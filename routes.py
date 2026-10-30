# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""HTTP routes of the module, served by trytond.

Under /<database>/avisos/, the notices web app: Voyager dispatches all dynamic content (see
voyager.py and www/); this file only mounts that entry point and serves content that needs
specific headers: static files, the service worker (scoped to the route), the manifest and
notice snapshots.

Under /<database>/videovigilance/, the API of the detection service (see doc/service_api.md).
"""

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from functools import wraps

from trytond.config import config
from trytond.model.exceptions import SQLConstraintError
from trytond.pool import Pool
from trytond.protocols.wrappers import (
    HTTPStatus, Response, abort, allow_null_origin, parse_authorization_header,
    set_max_request_size, with_pool, with_transaction)
from trytond.transaction import Transaction, without_check_access
from trytond.wsgi import app
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import send_from_directory

from . import live
from .assets import STATIC, VERSION
from .camera import MAX_HEARTBEAT_FRAME, is_frame
from .event import PERSON_TYPES
from .push import snapshot_token
from .res import SERVICE_APPLICATION
from .voyager import SITE_TYPE

PREFIX = '/<database_name>/avisos'
API_PREFIX = '/<database_name>/videovigilance'
JSON_MAX_BODY = 64 * 1024
EVENT_MAX_BODY = 4 * 1024 * 1024
EVENT_FIELDS = {
    'camera', 'type', 'started_at', 'ended_at', 'max_persons', 'max_confidence'}
UID = re.compile(r'[0-9A-Za-z_-]{1,64}')
# Credentials and frames must not be kept by anything on the way.
NO_STORE = {'Cache-Control': 'no-store'}


@app.route(PREFIX + '/static/<path:path>', methods=['GET', 'HEAD'])
@allow_null_origin
def avisos_static(request, database_name, path):
    response = send_from_directory(
        STATIC, path, request.environ, max_age=config.getint('web', 'cache_timeout'))
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.route(PREFIX + '/sw.js', methods=['GET'])
def avisos_service_worker(request, database_name):
    return Response((STATIC / 'sw.js').read_bytes(), HTTPStatus.OK,
        content_type='application/javascript',
        headers={'Cache-Control': 'no-cache',
            'Service-Worker-Allowed': f'/{database_name}/avisos/'})


@app.route(PREFIX + '/manifest.webmanifest', methods=['GET'])
def avisos_manifest(request, database_name):
    base = f'/{database_name}/avisos/'
    data = {
        'name': 'Videovigilància · alarma i avisos', 'short_name': 'Alarma',
        'start_url': base, 'scope': base, 'display': 'standalone',
        'background_color': '#111827', 'theme_color': '#111827',
        'icons': [
            {'src': f'{base}static/icon-192.png?v={VERSION}', 'sizes': '192x192', 'type': 'image/png'},
            {'src': f'{base}static/icon-512.png?v={VERSION}', 'sizes': '512x512', 'type': 'image/png'},
            ],
        }
    return Response(json.dumps(data), HTTPStatus.OK,
        content_type='application/manifest+json', headers={'Cache-Control': 'no-cache'})


@app.route(PREFIX + '/snapshots/<int:message_id>/<token>.jpg', methods=['GET'])
@with_pool
@with_transaction()
def avisos_snapshot(request, pool, message_id, token):
    """Notice snapshot. The URL is signed and expires with the notice (after 2 days):
    the browser requests it to display the notification, without session cookies."""
    not_found = Response('No existe', HTTPStatus.NOT_FOUND, content_type='text/plain')
    if not hmac.compare_digest(token, snapshot_token(message_id)):
        return not_found
    messages = pool.get('notification.web.message').search([('id', '=', message_id)])
    if not messages or messages[0].expires_at <= datetime.now():
        return not_found
    event = messages[0].event
    if event is None or not event.snapshot:
        return not_found
    return Response(bytes(event.snapshot), HTTPStatus.OK, content_type='image/jpeg',
        headers={'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer'})


@app.route(PREFIX + '/', methods=['GET', 'POST'], defaults={'path': ''})
@app.route(PREFIX + '/<path:path>', methods=['GET', 'POST'])
@with_pool
@with_transaction(readonly=False)
def avisos_site(request, pool, path):
    Site = pool.get('www.site')
    ModelData = pool.get('ir.model.data')
    # The user is the one of the Tryton session, so whatever Tryton asks to log in
    # (a password, WebAuthn...) applies here. Without a session Voyager still needs a user
    # to show the page that says so: this system user (no password or groups).
    user_id = request.user_id or ModelData.get_id('videovigilance', 'user_web')
    return Site.dispatch(SITE_TYPE, None, request, user_id,
        web_prefix=f'/{pool.database_name}/avisos')


# -- API of the detection service ------------------------------------------------------

def service_application(func):
    """Authenticate a service key and return its explicitly associated location."""
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        authorization = request.authorization or parse_authorization_header(
            request.headers.get('Authorization'))
        if authorization is None:
            abort(HTTPStatus.UNAUTHORIZED)
        if authorization.type == 'bearer':
            token = getattr(authorization, 'token', '')
        elif authorization.type == 'basic':
            token = authorization.get('password')
        else:
            abort(HTTPStatus.UNAUTHORIZED)
        Application = Pool().get('res.user.application')
        application = Application.check(token, SERVICE_APPLICATION)
        if not application:
            abort(HTTPStatus.UNAUTHORIZED)
        location = application.videovigilance_location
        if location is None or not location.active or application.user is None:
            abort(HTTPStatus.UNAUTHORIZED)
        with Transaction().set_user(application.user.id):
            with without_check_access():
                return func(request, *args, location=location, **kwargs)
    return wrapper


def service_route(rule, methods, max_size, readonly=None):
    """A route of the detection service with a valid, location-bound key."""
    def decorator(func):
        @wraps(func)
        def unchecked(request, *args, **kwargs):
            try:
                return func(request, *args, **kwargs)
            except RequestEntityTooLarge:
                return service_refuse(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'petición demasiado grande')
        handler = set_max_request_size(max_size)(with_pool(
                with_transaction(readonly=readonly)(
                    service_application(unchecked))))
        return app.route(API_PREFIX + rule, methods=methods)(handler)
    return decorator


def location_cameras(Camera, location, ids=None, codes=None):
    "Cameras matching the supplied identifiers and belonging to the authenticated location."
    domain = [('location', '=', location.id)]
    if ids is not None:
        domain.append(('id', 'in', ids))
    if codes is not None:
        domain.append(('code', 'in', codes))
    return Camera.search(domain)


def foreign_camera_ids(Camera, location, ids):
    "Whether any supplied id does not resolve to a camera of this location."
    return len(location_cameras(Camera, location, ids=ids)) != len(set(ids))




@service_route('/config', ['GET'], JSON_MAX_BODY)
def service_config(request, pool, location):
    body = json.dumps(
        {'cameras': pool.get('videovigilance.camera').service_config(location.id)},
        sort_keys=True)
    etag = '"%s"' % hashlib.sha256(body.encode()).hexdigest()
    headers = {**NO_STORE, 'ETag': etag}
    if request.headers.get('If-None-Match') == etag:
        return Response(status=HTTPStatus.NOT_MODIFIED, headers=headers)
    return Response(
        body, HTTPStatus.OK, content_type='application/json', headers=headers)


@service_route('/events/<uid>', ['PUT'], EVENT_MAX_BODY)
def service_event(request, pool, uid, location):
    try:
        values, snapshot = service_event_values(request, uid)
    except ValueError as problem:
        return service_refuse(HTTPStatus.BAD_REQUEST, str(problem))
    Camera = pool.get('videovigilance.camera')
    Camera.lock(Camera.browse([values['camera']]))
    if not location_cameras(Camera, location, ids=[values['camera']]):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    if snapshot:
        values['snapshot'] = snapshot
    try:
        event, created = pool.get('videovigilance.event').create_once(values)
    except SQLConstraintError:
        # Another request is storing this very uid at this moment; the service sends the
        # event again and then finds it stored.
        abort(HTTPStatus.CONFLICT)
    if not created:
        Camera.lock([event.camera] if event.camera else [])
        if event.camera is None or event.camera.location.id != location.id:
            return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    return service_reply({'uid': uid}, HTTPStatus.CREATED if created else HTTPStatus.OK)


@service_route('/heartbeat', ['POST'], JSON_MAX_BODY)
def service_heartbeat(request, pool, location):
    body = request.get_json(silent=True)
    cameras = body.get('cameras') if isinstance(body, dict) else None
    if not isinstance(cameras, list):
        return service_refuse(HTTPStatus.BAD_REQUEST, 'se esperaba {"cameras": [...]}')
    Camera = pool.get('videovigilance.camera')
    codes = [camera['code'] for camera in cameras
        if isinstance(camera, dict) and isinstance(camera.get('code'), str)]
    reported = Camera.search([('code', 'in', codes)])
    Camera.lock(reported)
    if any(camera.location.id != location.id for camera in reported):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    return service_reply(
        pool.get('videovigilance.configuration').service_heartbeat(cameras))


@service_route('/cameras/<int:camera_id>/frame', ['PUT'], MAX_HEARTBEAT_FRAME + 1024)
def service_camera_frame(request, pool, camera_id, location):
    Camera = pool.get('videovigilance.camera')
    Camera.lock(Camera.browse([camera_id]))
    if not location_cameras(Camera, location, ids=[camera_id]):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    frame = request.get_data()
    if not is_frame(frame):
        return service_refuse(
            HTTPStatus.BAD_REQUEST, 'no es un JPEG válido o es demasiado grande')
    if not Camera.store_frame(camera_id, frame):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    return Response(status=HTTPStatus.NO_CONTENT, headers=NO_STORE)


# Mailbox operations also lock camera ownership until the request completes.
@service_route('/live/poll', ['POST'], JSON_MAX_BODY)
def service_live_poll(request, pool, location):
    try:
        cameras = json.loads(request.get_data())['cameras']
        reports = {int(camera_id): report for camera_id, report in cameras.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return service_refuse(HTTPStatus.BAD_REQUEST, 'se esperaba {"cameras": {id: informe}}')
    Camera = pool.get('videovigilance.camera')
    Camera.lock(Camera.browse(list(reports)))
    if foreign_camera_ids(Camera, location, reports):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    reports = {str(camera_id): report for camera_id, report in reports.items()}
    return service_reply({'cameras': live.Mailbox(pool.database_name).exchange(reports)})


@service_route('/live/<int:camera_id>/frame', ['POST'], live.MAX_FRAME + 1024)
def service_live_frame(request, pool, camera_id, location):
    Camera = pool.get('videovigilance.camera')
    Camera.lock(Camera.browse([camera_id]))
    if not location_cameras(Camera, location, ids=[camera_id]):
        return service_refuse(HTTPStatus.NOT_FOUND, 'no existe la cámara')
    try:
        wanted = live.Mailbox(pool.database_name).accept_frame(
            camera_id, request.get_data())
    except ValueError as problem:
        return service_refuse(HTTPStatus.BAD_REQUEST, str(problem))
    return service_reply({'wanted': wanted})



def service_reply(data, status=HTTPStatus.OK, **headers):
    return Response(json.dumps(data), status, content_type='application/json',
        headers={**NO_STORE, **headers})


def service_refuse(status, why):
    return service_reply({'error': why}, status)


def utc_naive(value):
    "A naive UTC datetime, what Tryton stores, from an ISO 8601 string."
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('fecha no válida: %r' % (value,)) from None
    return moment


def service_event_values(request, uid):
    "(the values of the event, its snapshot or None) of a request; ValueError if not valid."
    # Transaction lock retries reuse the request, whose upload stream is already closed.
    cached = getattr(request, '_videovigilance_event_values', None)
    if cached is not None:
        values, snapshot = cached
        return dict(values), snapshot
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
        values[name] = utc_naive(values.get(name))
    upload = request.files.get('snapshot')
    snapshot = None
    if upload:
        snapshot = upload.read()
        upload.close()
    if snapshot is not None and not snapshot.startswith(b'\xff\xd8'):
        raise ValueError('«snapshot» no es un JPEG')
    values = {**values, 'uid': uid}
    request._videovigilance_event_values = (values, snapshot)
    return dict(values), snapshot


