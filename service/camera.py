import logging
import threading
import time
import uuid
from datetime import datetime, timezone

import os

os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')


import cv2
from .motion import MotionDetector, Person, PresenceDetector

log = logging.getLogger(__name__)

ROTATIONS = {
    0: None,
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
MAX_BACKOFF_S = 30
SNAPSHOT_MAX_WIDTH = 1280
HEARTBEAT_MAX_WIDTH = 640
HEARTBEAT_QUALITY = 70


EVENT_LABELS = {'person_motion': 'persona en movimiento', 'person_detection': 'persona detectada'}


def _utc(timestamp):
    """Tryton stores DateTime values in UTC without a timezone."""
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None).isoformat()


class CameraRuntime:
    """Continuous RTSP capture, scheduled analysis by the pool, and the person events the
    camera is configured for: presence (a person is in view) and/or motion."""

    def __init__(self, config, motion_config, outbox, interval):
        self.config = config
        self.interval = interval
        self.next_due = 0.0
        self.busy = False  # managed by the pool under its lock
        self.connected = False
        self.analysed = 0
        self.model_ms = 0.0
        self._outbox = outbox
        self._detectors = {kind: tracker(motion_config) for kind, tracker, wanted in (
                ('person_motion', MotionDetector, config.detect_motion),
                ('person_detection', PresenceDetector, config.detect_persons)) if wanted}
        self._frame = None
        self._captured_at = 0.0
        self._sequence = 0
        self._analysed_sequence = 0
        self._frame_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._stopped = False
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._capture, name=f'capture-{config.code}', daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)
        with self._analysis_lock:
            self._stopped = True
            for tracker in self._detectors.values():
                tracker.close()

    def _open(self):
        return cv2.VideoCapture(self.config.url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000])

    def _capture(self):
        backoff = 1
        while not self._stop.is_set():
            capture = self._open()
            if not capture.isOpened():
                capture.release()
                log.warning('%s: no se puede abrir el RTSP; reintento en %ds', self.config.code, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue
            log.info('%s: conectada', self.config.code)
            backoff, self.connected = 1, True
            try:
                while not self._stop.is_set():
                    ok, frame = capture.read()
                    if not ok:
                        break
                    with self._frame_lock:
                        self._frame, self._captured_at = frame, time.time()
                        self._sequence += 1
            except cv2.error:
                log.exception('%s: error leyendo el vídeo', self.config.code)
            finally:
                capture.release()
                self.connected = False
                with self._frame_lock:
                    self._frame = None
            if not self._stop.is_set():
                log.warning('%s: sin vídeo; reconectando', self.config.code)
                self._stop.wait(backoff)

    def analyse(self, detector, now):
        with self._analysis_lock:
            if self._stopped:
                return
            with self._frame_lock:
                frame, sequence, captured_at = self._frame, self._sequence, self._captured_at
            if frame is None or sequence == self._analysed_sequence:
                for tracker in self._detectors.values():
                    tracker.tick(now)
                return
            self._analysed_sequence = sequence
            rotation = ROTATIONS[self.config.rotation]
            image = frame if rotation is None else cv2.rotate(frame, rotation)
            detections, model_ms = detector.detect(image)
            people = [Person(float(x), float(y), float(w), float(h), float(score))
                for (x, y, w, h), score in detections]
            self.analysed += 1
            self.model_ms = model_ms if self.analysed == 1 else 0.9 * self.model_ms + 0.1 * model_ms
            for kind, tracker in self._detectors.items():
                self._emit(kind, tracker.update(
                    captured_at, people, snapshot=self._snapshot(kind, image, people)))

    def _snapshot(self, kind, image, people):
        "The JPEG for an episode: boxes are red for people who move, green for those who are just there."
        def draw(counted):
            return self._jpeg(
                image, people, counted if kind == 'person_motion' else [False] * len(people))
        return draw

    def latest_jpeg(self):
        "The newest frame, upright, as a small JPEG for the heartbeat; None without video."
        with self._frame_lock:
            frame = self._frame
        if frame is None:
            return None
        try:
            rotation = ROTATIONS[self.config.rotation]
            image = frame if rotation is None else cv2.rotate(frame, rotation)
            return self._encode(image, HEARTBEAT_MAX_WIDTH, HEARTBEAT_QUALITY)
        except cv2.error:
            log.exception('%s: no se pudo codificar el fotograma del latido', self.config.code)
            return None

    def _jpeg(self, image, people, moving):
        try:
            canvas = image.copy()
            for person, is_moving in zip(people, moving):
                p1 = (max(0, int(person.x)), max(0, int(person.y)))
                p2 = (int(person.x + person.width), int(person.y + person.height))
                cv2.rectangle(canvas, p1, p2, (0, 0, 255) if is_moving else (0, 200, 0), 2)
            return self._encode(canvas, SNAPSHOT_MAX_WIDTH, 85)
        except cv2.error:
            log.exception('%s: no se pudo codificar la captura', self.config.code)
            return None

    @staticmethod
    def _encode(image, max_width, quality):
        if image.shape[1] > max_width:
            scale = max_width / image.shape[1]
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return jpeg.tobytes() if ok else None

    def _emit(self, kind, episodes):
        for episode in episodes:
            uid = uuid.uuid4().hex
            self._outbox.add(uid, {
                'uid': uid,
                'camera': self.config.id,
                'type': kind,
                'started_at': _utc(episode.started_at),
                'ended_at': _utc(episode.ended_at),
                'max_persons': episode.max_persons,
                'max_confidence': episode.max_confidence,
                }, episode.snapshot)
            log.info('%s: %s (%d persona(s), conf. %.2f); aviso enviado a la cola',
                self.config.code, EVENT_LABELS[kind], episode.max_persons,
                episode.max_confidence)
