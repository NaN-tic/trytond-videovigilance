import base64
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import requests


class TrytonUnavailable(Exception):
    """Network, authentication, or server error: retrying is appropriate."""


class TrytonRejected(Exception):
    """Tryton rejected the request (validation or model permissions)."""


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


def _datetime(value):
    value = datetime.fromisoformat(value)
    return {'__class__': 'datetime', 'year': value.year, 'month': value.month,
        'day': value.day, 'hour': value.hour, 'minute': value.minute,
        'second': value.second, 'microsecond': value.microsecond}


def _bytes(data):
    "Binary data as Tryton's JSON-RPC wants it."
    return {'__class__': 'bytes', 'base64': base64.b64encode(data).decode()}


class TrytonClient:
    """JSON-RPC client. Not thread-safe: use one instance per thread."""

    CAMERA = 'model.videovigilance.camera'
    EVENT = 'model.videovigilance.event'
    CONFIGURATION = 'model.videovigilance.configuration'

    def __init__(self, url, user, password, timeout=15):
        self._url = url
        self._timeout = timeout
        self._session = requests.Session()
        self._session.auth = (user, password)

    def _call(self, method, params):
        try:
            response = self._session.post(self._url, timeout=self._timeout,
                json={'id': 1, 'method': method, 'params': params})
        except requests.RequestException as error:
            raise TrytonUnavailable(type(error).__name__) from None
        if response.status_code in (401, 403):
            raise TrytonUnavailable('Tryton rechaza las credenciales del servicio')
        if response.status_code >= 400:
            raise TrytonUnavailable(f'HTTP {response.status_code}')
        try:
            body = response.json()
        except ValueError:
            raise TrytonUnavailable('respuesta no JSON') from None
        if body.get('error'):
            raise TrytonRejected(f'{method}: {str(body["error"])[:300]}')
        return body['result']

    def cameras(self):
        """Active cameras with credentials decrypted by Tryton."""
        found = self._call(f'{self.CAMERA}.search_read', [
            [['active', '=', True]], 0, None, None,
            ['code', 'host', 'rtsp_port', 'stream_path', 'secondary_stream_path', 'username',
                'rotation', 'ptz', 'onvif_enabled', 'onvif_port', 'detect_persons',
                'detect_motion'], {}])
        passwords = {c['code']: c['password']
            for c in self._call(f'{self.CAMERA}.get_credentials', [None, {}])}
        return [CameraConfig(
                id=row['id'], code=row['code'], host=row['host'],
                rtsp_port=row['rtsp_port'], stream_path=row['stream_path'],
                rotation=int(row['rotation']), username=row['username'],
                password=passwords.get(row['code']),
                secondary_stream_path=row['secondary_stream_path'] or None,
                ptz=bool(row['ptz']), onvif_enabled=bool(row['onvif_enabled']),
                onvif_port=row['onvif_port'] or None,
                detect_persons=bool(row['detect_persons']),
                detect_motion=bool(row['detect_motion']))
            for row in found]

    def create_event(self, event, snapshot):
        values = {**event,
            'started_at': _datetime(event['started_at']),
            'ended_at': _datetime(event['ended_at'])}
        if snapshot:
            values['snapshot'] = _bytes(snapshot)
        return self._call(f'{self.EVENT}.create', [[values], {}])[0]

    def event_exists(self, uid):
        return self._call(f'{self.EVENT}.search_count', [[['uid', '=', uid]], {}]) > 0

    def heartbeat(self, cameras):
        """Tell Tryton the service is alive and report each camera's state, with its
        latest frame (JPEG bytes) when there is one.

        Returns the intervals configured in Tryton.
        """
        cameras = [{**camera, 'frame': _bytes(camera['frame'])} if camera.get('frame') else camera
            for camera in cameras]
        return self._call(f'{self.CONFIGURATION}.heartbeat', [cameras, {}])

    def relay_token(self):
        "Token for the live relay routes: {'token', 'expires_in'}."
        return self._call(f'{self.CONFIGURATION}.relay_token', [{}])


class RelayToken:
    """The live relay token, renewed before it expires. Shared by threads."""

    def __init__(self, client):
        self._client = client  # a TrytonClient, only used while holding the lock
        self._lock = threading.Lock()
        self._token = None
        self._renew_at = 0.0

    def get(self):
        with self._lock:
            if self._token is None or time.monotonic() >= self._renew_at:
                reply = self._client.relay_token()
                self._token = reply['token']
                self._renew_at = time.monotonic() + float(reply['expires_in']) / 2
            return self._token

    def reject(self, token):
        "Tryton did not accept this token: ask for a new one next time."
        with self._lock:
            if self._token == token:
                self._token = None


class RelayClient:
    """Client of Tryton's live relay routes. Not thread-safe: one instance per thread."""

    def __init__(self, url, tokens, timeout=5):
        self._url = url.rstrip('/')
        self._tokens = tokens
        self._timeout = timeout
        self._session = requests.Session()

    def _post(self, path, headers=None, **options):
        for attempt in (1, 2):
            token = self._tokens.get()
            sent = {**(headers or {}), 'Authorization': f'Bearer {token}'}
            try:
                response = self._session.post(f'{self._url}/{path}', headers=sent,
                    timeout=self._timeout, **options)
            except requests.RequestException as error:
                raise TrytonUnavailable(type(error).__name__) from None
            if response.status_code == 401 and attempt == 1:
                self._tokens.reject(token)  # it may just have expired: try once with a new one
                continue
            break
        if response.status_code >= 400:
            raise TrytonUnavailable(f'HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise TrytonUnavailable('respuesta no JSON') from None

    def poll(self, reports):
        """Tell Tryton what is happening on each camera ({id: {'error', 'message'}}).

        Returns {id: {'wanted': someone is watching, 'commands': [movement commands]}}.
        """
        try:
            return {int(camera_id): answer for camera_id, answer in
                self._post('poll', json={'cameras': reports})['cameras'].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TrytonUnavailable('respuesta inesperada') from None

    def push(self, camera_id, jpeg):
        "Send a frame. Returns whether someone is still watching."
        try:
            return bool(self._post(f'{camera_id}/frame', data=jpeg,
                    headers={'Content-Type': 'image/jpeg'})['wanted'])
        except (KeyError, TypeError):
            raise TrytonUnavailable('respuesta inesperada') from None
