import logging
import threading
import time

from .detector import Detector

log = logging.getLogger(__name__)

IDLE_S = 0.05


class InferencePool:
    """Shared inference threads: each analysis is handed to the next camera in turn."""

    def __init__(self, settings, stop):
        self._stop = stop
        self._lock = threading.Lock()
        self._cameras = {}
        # Load models here so a failure prevents the service from starting.
        self._detectors = [
            Detector(settings.model, threads=settings.threads,
                confidence=settings.confidence, nms=settings.nms)
            for _ in range(settings.workers)]
        self._threads = []

    def start(self):
        for index, detector in enumerate(self._detectors):
            thread = threading.Thread(
                target=self._work, args=(detector,), name=f'inference-{index}', daemon=True)
            thread.start()
            self._threads.append(thread)

    def join(self):
        for thread in self._threads:
            thread.join(timeout=10)

    def set_cameras(self, cameras):
        with self._lock:
            self._cameras = dict(cameras)

    def _claim(self, now):
        with self._lock:
            idle = [c for c in self._cameras.values() if not c.busy]
            due = [c for c in idle if c.next_due <= now]
            if not due:
                return None, min((c.next_due - now for c in idle), default=IDLE_S)
            camera = min(due, key=lambda c: c.next_due)
            camera.busy = True
            camera.next_due = now + camera.interval
            return camera, 0

    def _work(self, detector):
        while not self._stop.is_set():
            camera, wait = self._claim(time.time())
            if camera is None:
                self._stop.wait(min(max(wait, 0.005), IDLE_S))
                continue
            try:
                camera.analyse(detector, time.time())
            except Exception:
                log.exception('%s: error analizando una imagen', camera.config.code)
            finally:
                with self._lock:
                    camera.busy = False
