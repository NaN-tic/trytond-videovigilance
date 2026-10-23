"""The two heartbeats.

* Camera heartbeat: every `camera_interval` seconds each camera is probed (an RTSP
  OPTIONS request, which needs no login and no decoding) and must also be delivering
  video. A camera is declared offline only after `camera_failures` consecutive bad checks,
  and online again after one good check.
* Service heartbeat: every `service_interval` seconds the service tells Tryton it is
  alive and reports the state of each camera together with its latest frame (a small JPEG).
  Tryton answers with the current intervals, which are set in its configuration form, so
  they change without redeploying.

Only Tryton can raise the alarm for a silent service (a service that cannot reach Tryton
has no other way to reach the user): its watchdog does it when the heartbeats stop.
"""
import logging
import socket
import threading
import time

from .onvif import OnvifCamera
from .tryton import TrytonRejected, TrytonUnavailable

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 3.0
MIN_INTERVAL_S = 5.0
DEVICE_INFO_TTL_S = 6 * 3600.0  # brand, model and firmware are read again this often
DEVICE_INFO_RETRY_S = 600.0  # after a failed read


class HealthSettings:
    """Intervals and tolerance. Defaults apply until Tryton answers, then Tryton's values."""

    def __init__(self):
        self.camera_interval = 5.0
        self.camera_failures = 1
        self.service_interval = 5.0

    def update(self, reply):
        "Adopt the values in Tryton's answer; missing or invalid ones are ignored."
        for key, cast, minimum in (
                ('camera_interval', float, MIN_INTERVAL_S),
                ('camera_failures', int, 1),
                ('service_interval', float, MIN_INTERVAL_S)):
            try:
                value = cast(reply[key])
            except (KeyError, TypeError, ValueError):
                continue
            setattr(self, key, max(value, minimum))


def rtsp_probe(host, port, timeout=PROBE_TIMEOUT_S):
    """Ask the camera's RTSP server for its options. Returns (ok, detail).

    Any RTSP status line counts as alive, including 401 (login required).
    """
    url_host = f'[{host}]' if ':' in host else host
    request = (f'OPTIONS rtsp://{url_host}:{port}/ RTSP/1.0\r\n'
        'CSeq: 1\r\nUser-Agent: cameras-detection\r\n\r\n').encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(request)
            reply = connection.recv(64)
    except socket.timeout:
        return False, 'sin respuesta (tiempo agotado)'
    except OSError as error:
        return False, f'no se puede conectar ({error.strerror or type(error).__name__})'
    if not reply.startswith(b'RTSP/'):
        return False, 'respuesta que no es RTSP'
    return True, ''


class CameraHealth:
    """State of one camera: unknown until decided, then online/offline with hysteresis.
    It also holds what the monitor knows about the camera besides that."""

    def __init__(self):
        self.online = None
        self.detail = ''
        self._failures = 0
        self.frame = None  # gives the camera's latest JPEG (or None)
        self.device = None  # what the camera said about itself over ONVIF
        self.device_due = 0.0  # when to ask it again

    def record(self, ok, detail, failures_needed):
        if ok:
            self._failures, self.online, self.detail = 0, True, ''
            return
        self._failures += 1
        self.detail = detail
        if self._failures >= failures_needed:
            self.online = False


class Periodic(threading.Thread):
    "A thread that runs `step` now and then every `interval()` seconds until stopped."

    def __init__(self, name, stop):
        super().__init__(name=name, daemon=True)
        self._stop_event = stop

    def interval(self):
        raise NotImplementedError

    def step(self):
        raise NotImplementedError

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception:
                log.exception('%s: error inesperado', self.name)
            if self._stop_event.wait(self.interval()):
                break


def read_device_info(config):
    "What the camera reports over ONVIF about itself (see OnvifCamera.device_info)."
    return OnvifCamera(
        config.host, config.onvif_port, config.username, config.password).device_info()


class CameraMonitor(Periodic):
    """Camera heartbeat. `cameras()` returns [(camera config, video is flowing, frame)]
    where `frame()` gives the camera's latest JPEG (or None). A reachable camera with ONVIF
    enabled is also asked, now and then, for its brand and model."""

    def __init__(self, cameras, settings, stop, probe=rtsp_probe,
            device_info=read_device_info, clock=time.monotonic):
        super().__init__('camera-heartbeat', stop)
        self._cameras = cameras
        self._settings = settings
        self._probe = probe
        self._read_device = device_info
        self._clock = clock
        self._states = {}
        self._lock = threading.Lock()

    def interval(self):
        return self._settings.camera_interval

    def step(self):
        current = self._cameras()
        for config, video, frame in current:
            ok, detail = self._probe(config.host, config.rtsp_port)
            if ok and not video:
                ok, detail = False, 'responde pero no entrega vídeo'
            with self._lock:
                state = self._states.setdefault(config.code, CameraHealth())
                state.frame = frame
                before = state.online
                state.record(ok, detail, self._settings.camera_failures)
                after = state.online
            if before is not False and after is False:
                log.warning('%s: SIN CONEXIÓN (%s)', config.code, detail)
            elif before is False and after:
                log.info('%s: conexión recuperada', config.code)
            if ok:
                self._refresh_device(config, state)
        codes = {config.code for config, _, _ in current}
        with self._lock:
            for code in set(self._states) - codes:
                del self._states[code]

    def _refresh_device(self, config, state):
        "Read the camera's brand and model when it is due; a failure only postpones it."
        if not (getattr(config, 'onvif_enabled', False) and getattr(config, 'onvif_port', None)
                and config.username and config.password):
            return
        now = self._clock()
        if now < state.device_due:
            return
        try:
            device = self._read_device(config)
        except Exception as error:
            log.info('%s: no se pudo leer la marca y el modelo por ONVIF (%s)',
                config.code, error)
            state.device_due = now + DEVICE_INFO_RETRY_S
            return
        if device != state.device:
            log.info('%s: ONVIF informa de %s %s', config.code,
                device.get('manufacturer', '?'), device.get('model', '?'))
        state.device, state.device_due = device, now + DEVICE_INFO_TTL_S

    def reports(self):
        """Cameras whose state is already decided, as Tryton expects them. An online
        camera carries its latest frame, taken now so that it is as fresh as the beat;
        a camera that told its brand and model carries them as `device`."""
        with self._lock:
            decided = [(code, state.online, state.detail, state.frame, state.device)
                for code, state in self._states.items() if state.online is not None]
        reports = []
        for code, online, detail, frame, device in decided:
            report = {'code': code, 'online': online, 'detail': detail}
            jpeg = frame() if online else None
            if jpeg:
                report['frame'] = jpeg
            if device:
                report['device'] = dict(device)
            reports.append(report)
        return reports


class HeartbeatSender(Periodic):
    "Service heartbeat to Tryton, carrying the camera states."

    def __init__(self, client, monitor, settings, stop):
        super().__init__('tryton-heartbeat', stop)
        self._client = client
        self._monitor = monitor
        self._settings = settings
        self.failing = False

    def interval(self):
        return self._settings.service_interval

    def step(self):
        try:
            reply = self._client.heartbeat(self._monitor.reports())
        except (TrytonUnavailable, TrytonRejected) as error:
            if not self.failing:
                log.warning('Latido a Tryton fallido (%s); se reintenta cada %.0f s',
                    error, self._settings.service_interval)
            self.failing = True
            return
        if self.failing:
            log.info('Latido a Tryton restablecido')
        self.failing = False
        self._settings.update(reply)
