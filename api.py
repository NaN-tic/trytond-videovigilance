# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""HTTP API of the detection service, served by trytond under /<database>/videovigilance/.

The service is the only client. It does not log in to Tryton and does not call its JSON-RPC:
it sends a key of the `videovigilance` application of a user (`res.user.application`, what
Tryton has for this) as `Authorization: Bearer <key>`. The key says who the service is; what
it can do is what these routes do. The groups and access rules of that user do not take part,
and neither does the authentication configured for people (password, WebAuthn...).

* GET  config                   the active cameras with their credentials (ETag, 304)
* PUT  events/<uid>             a detection: multipart with `event` (JSON) and `snapshot`
                                (JPEG). 201 when stored, 200 when the uid was already stored.
* POST heartbeat                {"cameras": [{code, online, detail, device}]}; answers the
                                intervals the service has to use
* PUT  cameras/<id>/frame       the latest picture of a camera (image/jpeg), 204
* POST live/poll                which cameras are watched and the movement commands waiting
* POST live/<id>/frame          a live frame (image/jpeg); answers whether it is still watched

Errors are JSON ({"error": why}) with 400 (what was sent is not valid), 401 (no valid key),
404 (no such camera) or 413 (too big).
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from functools import wraps

from trytond.model.exceptions import SQLConstraintError
from trytond.pool import PoolMeta
from trytond.protocols.wrappers import (
    HTTPStatus, Response, abort, set_max_request_size, user_application, with_pool,
    with_transaction)
from trytond.transaction import without_check_access
from trytond.wsgi import app
from werkzeug.exceptions import RequestEntityTooLarge

from . import live
from .camera import MAX_HEARTBEAT_FRAME, is_frame
from .event import PERSON_TYPES

PREFIX = '/<database_name>/videovigilance'
SERVICE_APPLICATION = 'videovigilance'
JSON_MAX_BODY = 64 * 1024
EVENT_MAX_BODY = 4 * 1024 * 1024
EVENT_FIELDS = {
    'camera', 'type', 'started_at', 'ended_at', 'max_persons', 'max_confidence'}
UID = re.compile(r'[0-9A-Za-z_-]{1,64}')
# Credentials and frames must not be kept by anything on the way.
HEADERS = {'Cache-Control': 'no-store'}


class UserApplication(metaclass=PoolMeta):
    __name__ = 'res.user.application'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.application.selection.append(
            (SERVICE_APPLICATION, "Videovigilància: servei de detecció"))


service_application = user_application(SERVICE_APPLICATION, json=False)


def route(rule, methods, max_size, readonly=None):
    """A route of the detection service: it needs a valid key of its application, runs
    without checking the access rules of the key's user and has a limit for the request."""
    def decorator(func):
        @wraps(func)
        def unchecked(request, *args, **kwargs):
            try:
                with without_check_access():
                    return func(request, *args, **kwargs)
            except RequestEntityTooLarge:
                return refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'petición demasiado grande')
        handler = set_max_request_size(max_size)(with_pool(
                with_transaction(readonly=readonly)(service_application(unchecked))))
        return app.route(PREFIX + rule, methods=methods)(handler)
    return decorator


def reply(data, status=HTTPStatus.OK, **headers):
    return Response(json.dumps(data), status, content_type='application/json',
        headers={**HEADERS, **headers})


def refuse(status, why):
    return reply({'error': why}, status)


def utc(value):
    "A naive UTC datetime, what Tryton stores, from an ISO 8601 string."
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('fecha no válida: %r' % (value,)) from None
    return moment


def event_values(request, uid):
    "(the values of the event, its snapshot or None) of a request; ValueError if not valid."
    if not UID.fullmatch(uid):
        raise ValueError('uid no válido')
    try:
        values = json.loads(request.form['event'])
    except (KeyError, ValueError):
        raise ValueError('falta la parte «event», que debe ser JSON') from None
    if not isinstance(values, dict) or values.pop('uid', uid) != uid:
        raise ValueError('«event» debe ser un objeto con el mismo uid que la ruta')
    unknown = values.keys() - EVENT_FIELDS
    if unknown:
        raise ValueError('campos no admitidos: %s' % ', '.join(sorted(unknown)))
    if values.get('type') not in PERSON_TYPES:
        raise ValueError('tipo de evento no admitido')
    if type(values.get('camera')) is not int:
        raise ValueError('«camera» debe ser el id de la cámara')
    for name, kinds in (('max_persons', (int,)), ('max_confidence', (int, float))):
        if name in values and type(values[name]) not in kinds:
            raise ValueError('«%s» no es un número válido' % name)
    for name in ('started_at', 'ended_at'):
        values[name] = utc(values.get(name))
    upload = request.files.get('snapshot')
    snapshot = None
    if upload:
        snapshot = upload.read()
        upload.close()
    if snapshot is not None and not snapshot.startswith(b'\xff\xd8'):
        raise ValueError('«snapshot» no es un JPEG')
    return {**values, 'uid': uid}, snapshot


@route('/config', ['GET'], JSON_MAX_BODY)
def service_config(request, pool):
    body = json.dumps(
        {'cameras': pool.get('videovigilance.camera').service_config()}, sort_keys=True)
    etag = '"%s"' % hashlib.sha256(body.encode()).hexdigest()
    headers = {**HEADERS, 'ETag': etag}
    if request.headers.get('If-None-Match') == etag:
        return Response(status=HTTPStatus.NOT_MODIFIED, headers=headers)
    return Response(
        body, HTTPStatus.OK, content_type='application/json', headers=headers)


@route('/events/<uid>', ['PUT'], EVENT_MAX_BODY)
def service_event(request, pool, uid):
    try:
        values, snapshot = event_values(request, uid)
    except ValueError as problem:
        return refuse(HTTPStatus.BAD_REQUEST, str(problem))
    if not pool.get('videovigilance.camera').search([('id', '=', values['camera'])]):
        return refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    if snapshot:
        values['snapshot'] = snapshot
    try:
        _, created = pool.get('videovigilance.event').create_once(values)
    except SQLConstraintError:
        # Another request is storing this very uid at this moment; the service sends the
        # event again and then finds it stored.
        abort(HTTPStatus.CONFLICT)
    return reply({'uid': uid}, HTTPStatus.CREATED if created else HTTPStatus.OK)


@route('/heartbeat', ['POST'], JSON_MAX_BODY)
def service_heartbeat(request, pool):
    body = request.get_json(silent=True)
    cameras = body.get('cameras') if isinstance(body, dict) else None
    if not isinstance(cameras, list):
        return refuse(HTTPStatus.BAD_REQUEST, 'se esperaba {"cameras": [...]}')
    return reply(pool.get('videovigilance.configuration').service_heartbeat(cameras))


@route('/cameras/<int:camera_id>/frame', ['PUT'], MAX_HEARTBEAT_FRAME + 1024)
def service_camera_frame(request, pool, camera_id):
    frame = request.get_data()
    if not is_frame(frame):
        return refuse(HTTPStatus.BAD_REQUEST, 'no es un JPEG válido o es demasiado grande')
    if not pool.get('videovigilance.camera').store_frame(camera_id, frame):
        return refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    return Response(status=HTTPStatus.NO_CONTENT, headers=HEADERS)


# The live routes only touch the files of the mailbox: they never write to the database.
@route('/live/poll', ['POST'], JSON_MAX_BODY, readonly=True)
def service_live_poll(request, pool):
    try:
        cameras = json.loads(request.get_data())['cameras']
        reports = {str(int(camera_id)): report for camera_id, report in cameras.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return refuse(HTTPStatus.BAD_REQUEST, 'se esperaba {"cameras": {id: informe}}')
    return reply({'cameras': live.Mailbox(pool.database_name).exchange(reports)})


@route('/live/<int:camera_id>/frame', ['POST'], live.MAX_FRAME + 1024, readonly=True)
def service_live_frame(request, pool, camera_id):
    try:
        wanted = live.Mailbox(pool.database_name).accept_frame(
            camera_id, request.get_data())
    except ValueError as problem:
        return refuse(HTTPStatus.BAD_REQUEST, str(problem))
    return reply({'wanted': wanted})
