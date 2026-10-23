# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Live video relay between the detection service and the browsers.

Tryton does not connect to the cameras (it may live on another network). The detection
service, which can reach them, polls Tryton: the answer says which cameras someone is
watching and which movement commands are waiting. For the watched cameras only, it then
pushes JPEG frames. The browsers ask Tryton for the newest frame.

This module is the mailbox between those two sides. It is a private directory of small
files instead of process memory, so it behaves the same with one trytond process or with
several workers on the same machine. Every write is atomic (a temporary file that is
renamed), so a reader sees a whole old file or a whole new one, never a mix.

Authentication of the service on the relay routes is a token signed with the module's
secret_key: checking it needs no database, which matters at several frames per second.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import struct
import tempfile
import time
from pathlib import Path

# A camera stays "wanted" this long after the last browser request for it.
IDLE_S = 20.0
# The service counts as connected if it contacted Tryton within this time.
SERVICE_S = 8.0
# A frame older than this is not live any more and is not shown.
FRESH_S = 5.0
# A movement command that waited longer than this is dropped: the camera stops by itself
# soon after it starts, so a late command could only surprise someone.
COMMAND_TTL_S = 3.0
# How long a movement error reported by the service is shown to the browser.
MESSAGE_S = 4.0
TOKEN_TTL_S = 3600
MAX_FRAME = 2_000_000
MAX_CAMERAS = 64

# Where the files live; None means a private directory in the temporary folder.
DIR = None


class ServiceOffline(Exception):
    "The detection service is not connected, so nobody can show or move the camera."


def _directory():
    path = Path(DIR) if DIR else Path(tempfile.gettempdir()) / f'videovigilance-live-{os.getuid()}'
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise RuntimeError(f'{path} no es un directorio privado del usuario actual')
    return path


def _safe(name):
    return re.sub(r'[^A-Za-z0-9_-]', '_', str(name))


def _write(path, data):
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
    os.replace(temporary, path)


def _read_json(path):
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}


def _touch(path):
    path.touch(mode=0o600)


def _age(path):
    "Seconds since the file was last written, or None if it does not exist."
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def _unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def issue_token(secret, database, now=None):
    "A token the service sends on the relay routes; valid for TOKEN_TTL_S."
    expires = int((time.time() if now is None else now) + TOKEN_TTL_S)
    return f'{expires}.{_signature(secret, database, expires)}'


def check_token(secret, database, token, now=None):
    try:
        expires, signature = token.split('.', 1)
        expires = int(expires)
    except (AttributeError, ValueError):
        return False
    if expires < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(signature, _signature(secret, database, expires))


def _signature(secret, database, expires):
    return hmac.new(secret.encode(), f'relay:{database}:{expires}'.encode(),
        hashlib.sha256).hexdigest()


def mailbox():
    "The mailbox of the database of the current transaction."
    from trytond.transaction import Transaction
    return Mailbox(Transaction().database.name)


class Mailbox:
    "What the service and the browsers exchange, for one database."

    def __init__(self, database):
        self.database = _safe(database)

    def _file(self, camera_id, kind):
        return _directory() / f'{self.database}-{int(camera_id)}.{kind}'

    # -- the service is connected -------------------------------------------------

    def _service_file(self):
        return _directory() / f'{self.database}.service'

    def service_online(self):
        age = _age(self._service_file())
        return age is not None and age <= SERVICE_S

    # -- viewers ------------------------------------------------------------------

    def want(self, camera_id):
        "A viewer (or a movement command) needs this camera now."
        _touch(self._file(camera_id, 'want'))

    def wanted(self, camera_id):
        age = _age(self._file(camera_id, 'want'))
        return age is not None and age <= IDLE_S

    # -- frames -------------------------------------------------------------------

    def put_frame(self, camera_id, jpeg):
        "Store a frame and return its sequence number (always greater than the last)."
        path = self._file(camera_id, 'frame')
        try:
            with open(path, 'rb') as handle:
                previous, = struct.unpack('>Q', handle.read(8))
        except (OSError, struct.error):
            previous = 0
        sequence = max(previous + 1, int(time.time() * 1000))
        _write(path, struct.pack('>Q', sequence) + jpeg)
        return sequence

    def latest(self, camera_id, max_age=FRESH_S):
        "(sequence, jpeg) of the newest frame, or (0, None) if there is none or it is old."
        try:
            with open(self._file(camera_id, 'frame'), 'rb') as handle:
                age = time.time() - os.fstat(handle.fileno()).st_mtime
                data = handle.read()
        except FileNotFoundError:
            return 0, None
        if age > max_age or len(data) <= 8:
            return 0, None
        return struct.unpack('>Q', data[:8])[0], data[8:]

    def peek(self, camera_id, max_age=3.0):
        "The picture the camera is showing, only if someone is already watching it."
        return self.latest(camera_id, max_age)[1]

    # -- what the service reports about a camera ------------------------------------

    def _set_status(self, camera_id, error, message):
        path = self._file(camera_id, 'status')
        state = _read_json(path)
        error = error or None
        if not message and state.get('error') == error:
            return  # nothing new: polls are frequent, so avoid rewriting the file
        if message:
            state['message'], state['message_at'] = message, time.time()
        state['error'] = error
        _write(path, json.dumps(state).encode())

    def status(self, camera_id):
        "(error, message): why there is no video, and a recent movement problem."
        state = _read_json(self._file(camera_id, 'status'))
        message = state.get('message')
        if message and time.time() - state.get('message_at', 0) > MESSAGE_S:
            message = None
        return state.get('error'), message

    # -- movement commands --------------------------------------------------------

    def _commands(self, camera_id):
        return sorted(_directory().glob(f'{self.database}-{int(camera_id)}.cmd.*'))

    def send(self, camera_id, command):
        "Queue a movement command for the service. Raises ServiceOffline if it is not there."
        if not self.service_online():
            raise ServiceOffline('el servicio de detección no está conectado')
        self.want(camera_id)
        name = f'{time.time_ns():020d}-{secrets.token_hex(3)}'
        _write(self._file(camera_id, f'cmd.{name}'),
            json.dumps({**command, 'at': time.time()}).encode())

    def take(self, camera_id):
        "The commands waiting for the service, oldest first; each is delivered once."
        commands = []
        for path in self._commands(camera_id):
            command = _read_json(path)
            try:
                path.unlink()
            except FileNotFoundError:
                continue  # another worker delivered it
            age = time.time() - command.pop('at', 0)
            if command and age <= COMMAND_TTL_S:
                commands.append(command)
        return commands

    # -- the exchange the service makes -------------------------------------------

    def exchange(self, reports):
        """One poll of the service.

        `reports` is {camera id: {'error': why there is no video or None, 'message': a
        movement problem or None}}. The answer is {camera id: {'wanted': whether someone is
        watching, 'commands': the movement commands waiting}}.
        """
        _touch(self._service_file())
        answer = {}
        for camera_id, report in list(reports.items())[:MAX_CAMERAS]:
            report = report if isinstance(report, dict) else {}
            self._set_status(camera_id, str(report.get('error') or '')[:200],
                str(report.get('message') or '')[:200])
            wanted = self.wanted(camera_id)
            if not wanted:
                _unlink(self._file(camera_id, 'frame'))  # the last picture is not kept around
            answer[camera_id] = {'wanted': wanted, 'commands': self.take(camera_id)}
        return answer

    def accept_frame(self, camera_id, jpeg):
        "A frame pushed by the service. Returns whether someone is still watching."
        if not 0 < len(jpeg) <= MAX_FRAME or not jpeg.startswith(b'\xff\xd8'):
            raise ValueError('no es un JPEG válido')
        _touch(self._service_file())
        wanted = self.wanted(camera_id)
        if wanted:
            self.put_frame(camera_id, jpeg)
        return wanted
