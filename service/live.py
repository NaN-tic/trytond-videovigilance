"""Live video and camera movement for Tryton's web page.

Tryton may live on another network, so it never connects to the cameras: this service does,
and it only ever calls Tryton (nothing connects to the service). It polls Tryton to learn
which cameras someone is watching and which movement commands are waiting, and it pushes
JPEG frames for the watched cameras. A camera nobody watches is neither captured nor sent.

Each watched camera has its own capture child process (live_capture.py): a decoder crash
cannot take the detection down, and the camera credentials reach the child on stdin,
never in its command line.
"""
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import onvif
from .health import Periodic
from .tryton import TrytonRejected, TrytonUnavailable

log = logging.getLogger(__name__)

CAPTURE = Path(__file__).with_name('live_capture.py')
POLL_IDLE_S = 1.0  # nobody watches: how often Tryton is asked whether somebody does
POLL_BUSY_S = 0.25  # somebody watches: how soon a movement command reaches the camera
RETRY_S = 4.0  # a capture that failed is not restarted sooner than this
OFFLINE_S = 15.0  # without an answer from Tryton for this long, every capture is stopped
WIDTH = 960
QUALITY = 70
REASONS = {
    2: 'no se puede abrir el vídeo de la cámara',
    3: 'se perdió el vídeo de la cámara',
    }
ONVIF_PORT = 2020


class Capture:
    "The capture child process of one camera, and the newest frame it has produced."

    def __init__(self, url, rotation, fps, script=CAPTURE):
        self.error = None
        self.finished_at = None
        self._sequence = 0
        self._frame = None
        self._condition = threading.Condition()
        self._process = subprocess.Popen(
            [sys.executable, str(script)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        settings = {'url': url, 'rotation': rotation, 'width': WIDTH, 'quality': QUALITY,
            'fps': fps}
        self._process.stdin.write((json.dumps(settings) + '\n').encode())
        self._process.stdin.close()
        threading.Thread(target=self._read, name='live-capture', daemon=True).start()

    @property
    def alive(self):
        return self.finished_at is None

    def _read_exact(self, count):
        data = b''
        while len(data) < count:
            chunk = self._process.stdout.read(count - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _read(self):
        try:
            while True:
                header = self._read_exact(4)
                if header is None:
                    break
                size = int.from_bytes(header, 'big')
                if not 0 < size < 20_000_000:
                    break
                data = self._read_exact(size)
                if data is None:
                    break
                with self._condition:
                    self._sequence += 1
                    self._frame = data
                    self._condition.notify_all()
        finally:
            code = self._process.wait()
            self._process.stdout.close()  # otherwise every stopped capture leaks a descriptor
            with self._condition:
                self.finished_at = time.monotonic()
                if code not in (0, -15):
                    self.error = REASONS.get(code, 'el vídeo se interrumpió')
                self._condition.notify_all()

    def next_frame(self, after, timeout):
        """(sequence, jpeg) of a frame newer than `after`, waiting up to `timeout` for it.

        None if there is none yet, or the capture has ended and everything was delivered.
        """
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence != after or self.finished_at is not None, timeout)
            if self._sequence == after:
                return None
            return self._sequence, self._frame

    def stop(self):
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


class LiveStream:
    "One camera's capture and the thread that pushes its newest frames to Tryton."

    def __init__(self, camera, relay, fps, script=CAPTURE):
        self.key = (camera.live_url, camera.rotation)
        self.capture = Capture(camera.live_url, camera.rotation, fps, script)
        self._camera_id = camera.id
        self._relay = relay  # a RelayClient used only by this stream's thread
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._push, name=f'live-{camera.code}', daemon=True)
        self._thread.start()

    @property
    def alive(self):
        return self.capture.alive

    @property
    def error(self):
        return self.capture.error

    def _push(self):
        sequence = 0
        while not self._stop.is_set():
            item = self.capture.next_frame(sequence, 1.0)
            if item is None:
                if not self.capture.alive:
                    break
                continue
            sequence, jpeg = item
            try:
                wanted = self._relay.push(self._camera_id, jpeg)
            except (TrytonUnavailable, TrytonRejected):
                continue  # this frame is lost; the next one is newer anyway
            if not wanted:
                self.capture.stop()
                break

    def stop(self):
        self._stop.set()
        self.capture.stop()
        self._thread.join(timeout=3)


class Ptz:
    """Carries out the movement commands of one camera, away from the polling thread.

    A camera can take seconds to answer. Only the newest command waits its turn: a move that
    is renewed every 0.7 s, or one already overtaken by a newer press, loses nothing by being
    skipped.
    """

    def __init__(self, camera, factory=onvif.OnvifCamera):
        self.camera = camera  # replaced when its configuration changes in Tryton
        self._factory = factory
        self._client = None
        self._key = None
        self._pending = None
        self._message = None
        self._closed = False
        self._condition = threading.Condition()
        self._thread = threading.Thread(
            target=self._run, name=f'ptz-{camera.code}', daemon=True)
        self._thread.start()

    def submit(self, command):
        with self._condition:
            self._pending = command
            self._condition.notify()

    def take_message(self):
        "The reason the last command failed, once; None if it did not."
        with self._condition:
            message, self._message = self._message, None
            return message

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify()
        self._thread.join(timeout=5)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending is not None or self._closed)
                if self._closed:
                    return
                command, self._pending = self._pending, None
            try:
                self._execute(command)
            except (onvif.OnvifError, ValueError, KeyError, TypeError) as error:
                log.warning('%s: orden de movimiento fallida (%s)', self.camera.code, error)
                with self._condition:
                    self._message = f'La cámara no obedece: {error}'

    def _execute(self, command):
        camera = self.camera
        if not camera.can_move:
            raise onvif.OnvifError('esta cámara no se puede mover')
        key = (camera.host, camera.onvif_port or ONVIF_PORT, camera.username, camera.password)
        if key != self._key:
            self._client, self._key = self._factory(*key, timeout=4), key
        if command['op'] == 'stop':
            self._client.stop()
            return
        if command['op'] != 'move':
            raise ValueError('orden desconocida')
        dx, dy = (max(-1, min(1, int(command[axis]))) for axis in ('dx', 'dy'))
        speed = max(0.1, min(1.0, float(command['speed'])))
        # The camera always gets a timeout: without one it would keep moving for minutes.
        seconds = max(1.0, min(5.0, float(command['seconds'])))
        pan, tilt = onvif.axes_for_rotation(dx, dy, camera.rotation)
        self._client.move(pan * speed, tilt * speed, seconds=seconds)


class LiveRelay(Periodic):
    """Polls Tryton and does what it says: stream the watched cameras, move the ones told to.

    `make_client()` returns a new RelayClient (they are not thread-safe).
    """

    def __init__(self, make_client, fps, stop, script=CAPTURE, ptz_factory=onvif.OnvifCamera):
        super().__init__('live-relay', stop)
        self._make_client = make_client
        self._client = make_client()
        self._fps = fps
        self._script = script
        self._ptz_factory = ptz_factory
        self._lock = threading.Lock()
        self._cameras = {}
        self._streams = {}
        self._ptz = {}
        self._busy = False
        self._failures = 0
        self._last_ok = time.monotonic()

    def set_cameras(self, cameras):
        "The cameras Tryton has, as CameraConfig."
        with self._lock:
            self._cameras = {camera.id: camera for camera in cameras}

    def interval(self):
        if self._failures:
            return min(10.0, POLL_IDLE_S * 2 ** min(self._failures, 4))
        return POLL_BUSY_S if self._busy else POLL_IDLE_S

    def step(self):
        with self._lock:
            cameras = dict(self._cameras)
        try:
            answers = self._client.poll(self._reports(cameras))
        except (TrytonUnavailable, TrytonRejected) as error:
            if not self._failures:
                log.warning('Vídeo en directo: Tryton no responde (%s)', error)
            self._failures += 1
            self._busy = False
            if time.monotonic() - self._last_ok > OFFLINE_S:
                self._drop_streams(set(self._streams))
            return
        if self._failures:
            log.info('Vídeo en directo: Tryton responde de nuevo')
        self._failures = 0
        self._last_ok = time.monotonic()
        self._apply(cameras, answers)

    def _reports(self, cameras):
        reports = {}
        for camera_id in cameras:
            stream, ptz = self._streams.get(camera_id), self._ptz.get(camera_id)
            reports[camera_id] = {
                'error': stream.error if stream else None,
                'message': ptz.take_message() if ptz else None}
        return reports

    def _apply(self, cameras, answers):
        busy = False
        for camera_id, camera in cameras.items():
            answer = answers.get(camera_id) or {}
            commands = answer.get('commands') or []
            if commands:
                ptz = self._ptz.get(camera_id)
                if ptz is None:
                    ptz = self._ptz[camera_id] = Ptz(camera, self._ptz_factory)
                ptz.camera = camera
                for command in commands:
                    ptz.submit(command)
            if answer.get('wanted'):
                busy = True
                self._ensure_stream(camera)
            else:
                self._drop_streams({camera_id})
        self._drop_streams(set(self._streams) - set(cameras))
        for camera_id in set(self._ptz) - set(cameras):
            self._ptz.pop(camera_id).close()
        self._busy = busy

    def _ensure_stream(self, camera):
        key = (camera.live_url, camera.rotation)
        stream = self._streams.get(camera.id)
        if stream is not None:
            failed_just_now = (not stream.alive and stream.error
                and time.monotonic() - stream.capture.finished_at < RETRY_S)
            if stream.key == key and (stream.alive or failed_just_now):
                return  # running, or failed a moment ago: no restart storm
            stream.stop()
        self._streams[camera.id] = LiveStream(
            camera, self._make_client(), self._fps, self._script)
        log.info('%s: vídeo en directo iniciado', camera.code)

    def _drop_streams(self, camera_ids):
        for camera_id in camera_ids:
            stream = self._streams.pop(camera_id, None)
            if stream is not None:
                stream.stop()
                log.info('Vídeo en directo detenido (cámara %s)', camera_id)

    def close(self):
        "Stop every capture and movement thread."
        self._drop_streams(set(self._streams))
        for camera_id in list(self._ptz):
            self._ptz.pop(camera_id).close()
