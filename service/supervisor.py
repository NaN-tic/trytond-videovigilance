import logging
import signal
import threading

from .camera import CameraRuntime
from .health import CameraMonitor, HealthSettings, HeartbeatSender
from .live import LiveRelay
from .outbox import Outbox
from .pipeline import InferencePool
from .sender import EventSender
from .tryton import RelayClient, RelayToken, TrytonClient, TrytonRejected, TrytonUnavailable

log = logging.getLogger(__name__)

STATUS_EVERY_S = 60


class Supervisor:
    def __init__(self, settings):
        self._settings = settings
        self._stop = threading.Event()
        self._outbox = Outbox(settings.outbox)
        self._pool = InferencePool(settings, self._stop)
        # One client per thread: requests.Session is not thread-safe.
        self._client = TrytonClient(settings.tryton_url, settings.tryton_user, settings.tryton_password)
        self._sender = EventSender(
            TrytonClient(settings.tryton_url, settings.tryton_user, settings.tryton_password),
            self._outbox, self._stop)
        self._runtimes = {}
        self._latest = {}  # the cameras as Tryton has them now (a running one keeps its old object)
        self._health = HealthSettings()
        self._monitor = CameraMonitor(self._video_state, self._health, self._stop)
        self._heartbeat = HeartbeatSender(
            TrytonClient(settings.tryton_url, settings.tryton_user, settings.tryton_password),
            self._monitor, self._health, self._stop)
        tokens = RelayToken(
            TrytonClient(settings.tryton_url, settings.tryton_user, settings.tryton_password))
        self._live = LiveRelay(
            lambda: RelayClient(settings.relay_url, tokens), settings.live_fps, self._stop)

    def run(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: self._stop.set())
        self._sender.start()
        self._pool.start()
        self._monitor.start()
        self._heartbeat.start()
        self._live.start()
        log.info('Servicio iniciado: %d worker(s), %.1f FPS por cámara',
            self._settings.workers, self._settings.fps)
        next_status = 0
        try:
            while not self._stop.is_set():
                self._refresh()
                if self._stop.wait(self._settings.refresh_s if self._runtimes else 5):
                    break
                next_status += self._settings.refresh_s if self._runtimes else 5
                if next_status >= STATUS_EVERY_S:
                    next_status = 0
                    self._log_status()
        finally:
            self._shutdown()

    def _refresh(self):
        try:
            cameras = self._client.cameras()
        except (TrytonUnavailable, TrytonRejected) as error:
            # Keep the current configuration: cameras continue running.
            log.warning('No se pudo leer la configuración de Tryton (%s); se mantiene la actual', error)
            return
        wanted = {}
        for camera in cameras:
            if camera.ready:
                wanted[camera.code] = camera
            else:
                log.warning('%s: tiene usuario pero no contraseña en Tryton; se omite', camera.code)
        self._latest = wanted
        self._live.set_cameras(wanted.values())
        for code in list(self._runtimes):
            if wanted.get(code) != self._runtimes[code].config:
                log.info('%s: retirada o modificada; se detiene', code)
                self._runtimes.pop(code).stop()
        for code, camera in wanted.items():
            if code not in self._runtimes:
                runtime = CameraRuntime(
                    camera, self._settings.motion, self._outbox, 1 / self._settings.fps)
                runtime.start()
                self._runtimes[code] = runtime
                log.info('%s: iniciada (%s, rotación %d°)', code, camera.host, camera.rotation)
        self._pool.set_cameras(self._runtimes)

    def _video_state(self):
        """Cameras, whether each is delivering video and how to get its latest frame, for the
        camera heartbeat. ONVIF and credentials come from the newest settings: a running camera
        is not restarted when only those change."""
        return [(self._latest.get(code, runtime.config), runtime.connected, runtime.latest_jpeg)
            for code, runtime in list(self._runtimes.items())]

    def _log_status(self):
        for code, runtime in self._runtimes.items():
            log.info('estado %s: %s, %d imágenes analizadas, %.0f ms/inferencia',
                code, 'conectada' if runtime.connected else 'SIN VÍDEO',
                runtime.analysed, runtime.model_ms)
        log.info('estado cola: %d pendiente(s), %d fallido(s)',
            self._outbox.count('pending'), self._outbox.count('failed'))
        log.info('estado latido a Tryton: %s',
            'FALLA, Tryton no responde' if self._heartbeat.failing else 'correcto')

    def _shutdown(self):
        log.info('Deteniendo servicio')
        self._stop.set()
        self._pool.join()
        for runtime in self._runtimes.values():
            runtime.stop()
        self._sender.join(timeout=10)
        self._monitor.join(timeout=5)
        self._heartbeat.join(timeout=5)
        self._live.join(timeout=5)
        self._live.close()
        self._outbox.close()
