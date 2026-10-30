# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Live video of a camera and the controls to move it. Administrators only.

Tryton does not reach the camera: the detection service sends the frames and carries out
the movement commands (see ..live). These endpoints only read the newest frame and queue
commands, so they answer at once and never wait for the camera.
"""
from urllib.parse import quote

from dominate.tags import a, button, canvas, div, h2, label, option, p, select
from trytond.model import fields
from trytond.pool import Pool
from trytond.protocols.wrappers import Response
from trytond.transaction import without_check_access

from .. import live
from ..push import is_admin
from ..voyager import (
    Endpoint, base, component, csrf, current_user, guard, json_response, nav, page, url,
    voyager)

# (symbol, dx, dy, accessible name): dx is to the right and dy is up, as seen on screen.
PAD = [
    ('', 0, 0, None), ('▲', 0, 1, 'Arriba'), ('', 0, 0, None),
    ('◄', -1, 0, 'Izquierda'), ('■', 0, 0, 'Parar'), ('►', 1, 0, 'Derecha'),
    ('', 0, 0, None), ('▼', 0, -1, 'Abajo'), ('', 0, 0, None),
    ]
SPEEDS = [('0.3', 'Lenta'), ('0.6', 'Normal'), ('1', 'Rápida')]
BUTTON = ('flex h-14 items-center justify-center rounded-lg border border-white/20 '
    'bg-gray-800 text-2xl text-gray-100 active:bg-teal-400 active:text-gray-900')


def _admin_camera(code):
    """(camera, None) for an administrator and an existing camera, or (None, error)."""
    user = current_user()
    if user is None:
        return None, json_response({'error': 'Sesión no iniciada'}, 401)
    if not is_admin(user):
        return None, json_response({'error': 'Solo un administrador puede usar la cámara'}, 403)
    camera = Pool().get('videovigilance.camera').for_web(code)
    if camera is None:
        return None, json_response({'error': 'La cámara no existe'}, 404)
    return camera, None


class LivePage(Endpoint):
    "Live video page with the movement pad"
    __name__ = 'videovigilance.web.live'
    __slots__ = ()
    _url = '/live'
    _method = ['GET']

    def render(self):
        user = current_user()
        if user is None:
            return page(div(p('Inicia sesión para ver la cámara.',
                        cls='text-gray-400'), a('Entrar', href=base(),
                        cls='text-teal-300 hover:text-teal-200')))
        if not is_admin(user):
            with div() as root:
                component('session_bar')
                nav('live')
                p('Solo un administrador puede ver la cámara en directo.',
                    cls='text-gray-400')
            return page(root, status=403, title='Cámara')
        Camera = Pool().get('videovigilance.camera')
        with without_check_access():
            cameras = Camera.search([('active', '=', True)])
        code = voyager().request.args.get('code')
        camera = next((c for c in cameras if c.code == code), None) or (
            cameras[0] if cameras else None)
        with div() as root:
            component('session_bar')
            nav('live')
            if camera is None:
                p('No hay ninguna cámara activa.', cls='text-gray-400')
                return page(root, title='Cámara')
            if len(cameras) > 1:
                with div(cls='mb-3 flex flex-wrap gap-2'):
                    for other in cameras:
                        current = other.id == camera.id
                        a(other.name, href=url('live', code=other.code), cls=(
                                'rounded-lg px-3 py-1.5 text-sm font-semibold '
                                + ('bg-teal-400 text-gray-900' if current else
                                    'border border-white/20 text-gray-100')))
            with div(id='live', cls='rounded-xl border border-white/10 bg-gray-800/60 p-4',
                    **{'data-csrf': csrf(),
                        'data-frame': url('live_frame', code=camera.code),
                        'data-move': url('live_move', code=camera.code),
                        'data-stop': url('live_stop', code=camera.code)}):
                h2(camera.name, cls='mb-2 text-sm font-semibold text-gray-400')
                with div(cls='relative mb-3 overflow-hidden rounded-lg bg-black'):
                    canvas(id='live-canvas', width='640', height='360',
                        cls='block h-auto w-full')
                    p('Conectando…', id='live-status', cls='absolute left-2 top-2 '
                        'rounded bg-black/60 px-2 py-1 text-xs text-gray-100')
                if camera.ptz_available:
                    with div(cls='mx-auto grid w-52 touch-none select-none grid-cols-3 gap-2'):
                        for symbol, dx, dy, name in PAD:
                            if name is None:
                                div()
                                continue
                            attributes = {'type': 'button', 'aria-label': name, 'title': name}
                            if name != 'Parar':
                                attributes.update({'data-dx': str(dx), 'data-dy': str(dy)})
                            else:
                                attributes['data-halt'] = '1'
                            button(symbol, cls=BUTTON, **attributes)
                    with div(cls='mt-3 flex items-center justify-center gap-2 text-sm '
                            'text-gray-400'):
                        label('Velocidad', **{'for': 'live-speed'})
                        with select(id='live-speed', cls='rounded-lg border-white/20 '
                                'bg-gray-900 py-1 text-sm text-gray-100'):
                            for value, name in SPEEDS:
                                option(name, value=value, selected=(value == '0.6'))
                    p('Mantén pulsada una flecha para mover la cámara; también sirven las '
                        'teclas de flecha. Se detiene al soltar.',
                        cls='mt-3 text-center text-xs text-gray-400')
                else:
                    p('Esta cámara no se puede mover desde aquí (necesita PTZ y ONVIF '
                        'activados, con usuario y contraseña).',
                        cls='text-center text-xs text-gray-400')
                p('', id='live-message', cls='mt-2 min-h-4 text-center text-xs text-red-400')
        return page(root, title='Cámara')



class LiveFrame(Endpoint):
    """The latest frame of a camera, if newer than the one the browser already has.

    It answers at once (200 with a new JPEG, 204 when nothing is new, 503 with the reason
    when there is no video) so no database connection stays busy waiting for the video.
    Asking for a frame is also how the browser tells the service that someone is watching.
    """
    __name__ = 'videovigilance.web.live_frame'
    __slots__ = ()
    _url = '/live/<code>/frame'
    _method = ['GET']

    code = fields.Char('code')

    def render(self):
        camera, error = _admin_camera(self.code)
        if error is not None:
            return error
        # Voyager only fills fields from the route; the query string is read here.
        after = voyager().request.args.get('after', 0, type=int)
        mailbox = live.mailbox()
        mailbox.want(camera.id)
        sequence, frame = mailbox.latest(camera.id)
        problem, message = mailbox.status(camera.id)
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Frame-Sequence': str(sequence)}
        if message:  # a movement the camera did not carry out, reported by the service
            headers['X-Live-Message'] = quote(message)
        if frame is None:
            if not mailbox.service_online():
                return json_response(
                    {'error': 'El servicio de detección no está conectado'}, 503)
            if problem:
                return json_response({'error': problem.capitalize()}, 503)
            return Response(status=204, headers=headers)
        if sequence == after:
            return Response(status=204, headers=headers)
        return Response(frame, 200, content_type='image/jpeg', headers=headers)


class LiveMove(Endpoint):
    "Start moving the camera; it stops by itself soon unless the command is renewed"
    __name__ = 'videovigilance.web.live_move'
    __slots__ = ()
    _url = '/live/<code>/move'
    _method = ['POST']

    code = fields.Char('code')
    dx = fields.Integer('dx')
    dy = fields.Integer('dy')
    speed = fields.Float('speed')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        camera, error = _admin_camera(self.code)
        if error is not None:
            return error
        if self.dx not in (-1, 0, 1) or self.dy not in (-1, 0, 1) or not (self.dx or self.dy):
            return json_response({'error': 'Dirección no válida'}, 400)
        if not camera.ptz_available:
            return json_response({'error': 'Esta cámara no se puede mover'}, 409)
        speed = min(1.0, max(0.1, self.speed or 0.6))
        try:
            camera.ptz_move(self.dx, self.dy, speed, user)
        except live.ServiceOffline as problem:
            return json_response({'error': 'La cámara no obedece: %s' % problem}, 424)
        return json_response({'moving': True})


class LiveStop(Endpoint):
    "Stop the camera"
    __name__ = 'videovigilance.web.live_stop'
    __slots__ = ()
    _url = '/live/<code>/stop'
    _method = ['POST']

    code = fields.Char('code')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        camera, error = _admin_camera(self.code)
        if error is not None:
            return error
        if not camera.ptz_available:
            return json_response({'error': 'Esta cámara no se puede mover'}, 409)
        try:
            camera.ptz_stop()
        except live.ServiceOffline as problem:
            return json_response({'error': 'La cámara no obedece: %s' % problem}, 424)
        return json_response({'moving': False})
