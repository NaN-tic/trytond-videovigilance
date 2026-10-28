# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""HTTP routes for the notices web app, served by trytond under /<database>/avisos/.

Voyager dispatches all dynamic content (see voyager.py and www/). This file only
mounts that entry point and serves content that needs specific headers: static
files, the service worker (scoped to the route), the manifest, notice snapshots, and the
live relay the detection service uses to send video and receive movement commands.
"""

import hmac
import json
from datetime import datetime

from trytond.config import config
from trytond.protocols.wrappers import (
    HTTPStatus, Response, allow_null_origin, set_max_request_size, with_pool,
    with_transaction)
from trytond.wsgi import app
from werkzeug.utils import send_from_directory

from . import live
from .assets import STATIC, VERSION
from .push import snapshot_token
from .voyager import SITE_TYPE

PREFIX = '/<database_name>/avisos'
RELAY_MAX_BODY = 64 * 1024


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


def _relay_mailbox(request, pool):
    """The mailbox, if the request carries the service's token; None otherwise.

    Checking the token needs no database: frames arrive several times a second.
    """
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not live.check_token(
            config.get('videovigilance', 'secret_key'), pool.database_name, token.strip()):
        return None
    return live.Mailbox(pool.database_name)


def _relay_reply(data, status=HTTPStatus.OK):
    return Response(json.dumps(data), status, content_type='application/json',
        headers={'Cache-Control': 'no-store'})


@app.route(PREFIX + '/relay/poll', methods=['POST'])
@set_max_request_size(RELAY_MAX_BODY)
@with_pool
def relay_poll(request, pool):
    """The service's poll: what it sees on each camera in, who is watching and the
    movement commands waiting out. See live.Mailbox.exchange."""
    mailbox = _relay_mailbox(request, pool)
    if mailbox is None:
        return _relay_reply({'error': 'Testimonio no válido'}, HTTPStatus.UNAUTHORIZED)
    try:
        cameras = json.loads(request.get_data())['cameras']
        reports = {str(int(camera_id)): report for camera_id, report in cameras.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return _relay_reply({'error': 'Petición no válida'}, HTTPStatus.BAD_REQUEST)
    return _relay_reply({'cameras': mailbox.exchange(reports)})


@app.route(PREFIX + '/relay/<int:camera_id>/frame', methods=['POST'])
@set_max_request_size(live.MAX_FRAME + 1024)
@with_pool
def relay_frame(request, pool, camera_id):
    "A JPEG frame from the service. The answer tells it whether someone is still watching."
    mailbox = _relay_mailbox(request, pool)
    if mailbox is None:
        return _relay_reply({'error': 'Testimonio no válido'}, HTTPStatus.UNAUTHORIZED)
    try:
        wanted = mailbox.accept_frame(camera_id, request.get_data())
    except ValueError as error:
        return _relay_reply({'error': str(error)}, HTTPStatus.BAD_REQUEST)
    return _relay_reply({'wanted': wanted})


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
