import json
import logging
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)


class TrytonUnavailable(Exception):
    """Network, authentication, or server error: retrying is appropriate."""


class TrytonRejected(Exception):
    """Tryton refused what was sent because it is not valid: sending it again will not help."""


@dataclass(frozen=True)
class CameraConfig:
    id: int
    code: str
    host: str
    rtsp_port: int
    stream_path: str
    rotation: int
    username: str | None
    password: str | None = field(default=None, repr=False)
    # Only the live view and the movement controls use these. Changing them must not
    # restart the camera's detection, so they are left out of the comparison.
    secondary_stream_path: str | None = field(default=None, compare=False)
    ptz: bool = field(default=False, compare=False)
    onvif_enabled: bool = field(default=False, compare=False)
    onvif_port: int | None = field(default=None, compare=False)
    # Which person events this camera raises. Changing them restarts the camera's detection.
    detect_persons: bool = True
    detect_motion: bool = True

    @property
    def ready(self):
        return not self.username or bool(self.password)

    def _rtsp(self, path):
        host = f'[{self.host}]' if ':' in self.host else self.host
        auth = ''
        if self.username:
            auth = quote(self.username, safe='') + ':' + quote(self.password or '', safe='') + '@'
        return f"rtsp://{auth}{host}:{self.rtsp_port}/{path.lstrip('/')}"

    @property
    def url(self):
        return self._rtsp(self.stream_path)

    @property
    def live_url(self):
        "The lighter secondary stream, so the live view does not compete with the detection."
        return self._rtsp(self.secondary_stream_path or self.stream_path)

    @property
    def can_move(self):
        return bool(self.ptz and self.onvif_enabled and self.username and self.password)


def _why(response):
    """What Tryton says is wrong with a request, or None. Without that explanation it is not
    a refusal of the request (a Tryton without this API answers 404 with a web page)."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    why = body.get('error') or body.get('description') or body.get('message')
    return str(why)[:300] if why else None


class TrytonClient:
    """Client of the HTTP API of the videovigilance module, with the key of the service.
    Not thread-safe: use one instance per thread."""

    JPEG = {'Content-Type': 'image/jpeg'}

    def __init__(self, url, token, timeout=15):
        self._url = url.rstrip('/')
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers['Authorization'] = f'Bearer {token}'
        self._config = None  # (ETag, cameras) of the last answer

    def _request(self, method, path, **options):
        "The response to a request that Tryton accepted."
        try:
            response = self._session.request(
                method, f'{self._url}/{path}', timeout=self._timeout, **options)
        except requests.RequestException as error:
            raise TrytonUnavailable(type(error).__name__) from None
        if response.status_code in (401, 403):
            raise TrytonUnavailable('Tryton rechaza la clave del servicio')
        if response.status_code >= 400:
            why = _why(response)
            if why and response.status_code in (400, 404, 413):
                raise TrytonRejected(f'{method} {path}: {why}')
            raise TrytonUnavailable(f'HTTP {response.status_code}')
        return response

    def _json(self, method, path, **options):
        try:
            return self._request(method, path, **options).json()
        except ValueError:
            raise TrytonUnavailable('respuesta no JSON') from None

    def cameras(self):
        "Active cameras with their credentials. Tryton says when nothing changed."
        etag, cameras = self._config or (None, None)
        response = self._request(
            'GET', 'config', headers={'If-None-Match': etag} if etag else {})
        if response.status_code == 304 and cameras is not None:
            return cameras
        try:
            cameras = [CameraConfig(
                    id=row['id'], code=row['code'], host=row['host'],
                    rtsp_port=row['rtsp_port'], stream_path=row['stream_path'],
                    rotation=row['rotation'], username=row['username'],
                    password=row['password'],
                    secondary_stream_path=row['secondary_stream_path'],
                    ptz=row['ptz'], onvif_enabled=row['onvif_enabled'],
                    onvif_port=row['onvif_port'],
                    detect_persons=row['detect_persons'],
                    detect_motion=row['detect_motion'])
                for row in response.json()['cameras']]
        except (ValueError, KeyError, TypeError):
            raise TrytonUnavailable('respuesta inesperada') from None
        self._config = (response.headers.get('ETag'), cameras)
        return cameras

    def create_event(self, event, snapshot):
        """Store an event. Tryton keeps the first one of each uid, so sending an event again
        (for instance when the answer was lost) is harmless."""
        files = {'event': (None, json.dumps(event), 'application/json')}
        if snapshot:
            files['snapshot'] = ('snapshot.jpg', snapshot, 'image/jpeg')
        self._request('PUT', f"events/{quote(event['uid'], safe='')}", files=files)

    def heartbeat(self, cameras):
        """Tell Tryton the service is alive and report each camera's state ({id, code, online,
        detail, device}), then send the latest frame (JPEG bytes) of those that have one.

        Returns the intervals configured in Tryton.
        """
        reports = [{key: value for key, value in camera.items() if key not in ('id', 'frame')}
            for camera in cameras]
        reply = self._json('POST', 'heartbeat', json={'cameras': reports})
        for camera in cameras:
            if camera.get('frame'):
                try:
                    self._request('PUT', f"cameras/{camera['id']}/frame",
                        data=camera['frame'], headers=self.JPEG)
                except (TrytonUnavailable, TrytonRejected) as error:
                    log.warning('%s: no se pudo enviar la imagen (%s)', camera['code'], error)
        return reply

    def poll(self, reports):
        """Tell Tryton what is happening on each camera ({id: {'error', 'message'}}).

        Returns {id: {'wanted': someone is watching, 'commands': [movement commands]}}.
        """
        try:
            return {int(camera_id): answer for camera_id, answer in
                self._json('POST', 'live/poll', json={'cameras': reports})['cameras'].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TrytonUnavailable('respuesta inesperada') from None

    def push(self, camera_id, jpeg):
        "Send a frame. Returns whether someone is still watching."
        try:
            return bool(self._json(
                    'POST', f'live/{camera_id}/frame', data=jpeg, headers=self.JPEG)['wanted'])
        except (KeyError, TypeError):
            raise TrytonUnavailable('respuesta inesperada') from None
