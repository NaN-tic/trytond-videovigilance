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
from trytond.modules.xgettext import _

from .. import live
from ..push import is_admin
from ..voyager import (
    Endpoint, base, component, csrf, current_user, guard, json_response, nav,
    page, url, voyager)

# (symbol, dx, dy, accessible name): dx is to the right and dy is up, as seen on screen.
PAD = [
    ('', 0, 0, None), ('▲', 0, 1, lambda: _('Up')),
    ('', 0, 0, None), ('◄', -1, 0, lambda: _('Left')),
    ('■', 0, 0, lambda: _('Stop')),
    ('►', 1, 0, lambda: _('Right')),
    ('', 0, 0, None), ('▼', 0, -1, lambda: _('Down')),
    ('', 0, 0, None),
    ]
SPEEDS = [
    ('0.3', lambda: _('Slow')),
    ('0.6', lambda: _('Normal')),
    ('1', lambda: _('Fast'))]
BUTTON = ('flex h-14 items-center justify-center rounded-lg border border-white/20 '
    'bg-gray-800 text-2xl text-gray-100 active:bg-teal-400 active:text-gray-900')


def _admin_camera(code):
    """(camera, None) for an administrator and an existing camera, or (None, error)."""
    user = current_user()
    if user is None:
        return None, json_response({
            'error': _('No active session.')}, 401)
    if not is_admin(user):
        return None, json_response({
            'error': _('Only an administrator can use the camera.')}, 403)
    camera = Pool().get('videovigilance.camera').for_web(code)
    if camera is None:
        return None, json_response({
            'error': _('Camera not found.')}, 404)
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
            return page(div(p(_('Sign in to view the camera.'),
                        cls='text-gray-400'), a(
                        _('Sign in'), href=base(),
                        cls='text-teal-300 hover:text-teal-200')))
        if not is_admin(user):
            with div() as root:
                component('session_bar')
                nav('live')
                p(_('Only an administrator can view the live camera.'),
                    cls='text-gray-400')
            return page(root, status=403, title=_('Camera'))
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
                p(_('No active camera.'), cls='text-gray-400')
                return page(root, title=_('Camera'))
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
                    p(_('Connecting…'), id='live-status',
                        cls='absolute left-2 top-2 rounded bg-black/60 px-2 py-1 text-xs '
                        'text-gray-100')
                if camera.ptz_available:
                    with div(cls='mx-auto grid w-52 touch-none select-none grid-cols-3 gap-2'):
                        for symbol, dx, dy, name in PAD:
                            if name is None:
                                div()
                                continue
                            label_text = name()
                            attributes = {
                                'type': 'button', 'aria-label': label_text,
                                'title': label_text}
                            if dx or dy:
                                attributes.update({'data-dx': str(dx), 'data-dy': str(dy)})
                            else:
                                attributes['data-halt'] = '1'
                            button(symbol, cls=BUTTON, **attributes)
                    with div(cls='mt-3 flex items-center justify-center gap-2 text-sm '
                            'text-gray-400'):
                        label(_('Speed'),
                            **{'for': 'live-speed'})
                        with select(id='live-speed', cls='rounded-lg border-white/20 '
                                'bg-gray-900 py-1 text-sm text-gray-100'):
                            for value, message in SPEEDS:
                                option(message(), value=value,
                                    selected=(value == '0.6'))
                    p(_('Hold an arrow to move the camera; you can also use the arrow keys. It stops when you release.'),
                        cls='mt-3 text-center text-xs text-gray-400')
                else:
                    p(_('This camera cannot be moved from here (PTZ and ONVIF must be enabled, with a username and password).'),
                        cls='text-center text-xs text-gray-400')
                p('', id='live-message', cls='mt-2 min-h-4 text-center text-xs text-red-400')
        return page(root, title=_('Camera'))



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
                return json_response({
                    'error': _('The detection service is not connected.')}, 503)
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
            return json_response({
                'error': _('Invalid direction.')}, 400)
        if not camera.ptz_available:
            return json_response({
                'error': _('This camera cannot be moved from here.')}, 409)
        speed = min(1.0, max(0.1, self.speed or 0.6))
        try:
            camera.ptz_move(self.dx, self.dy, speed, user)
        except live.ServiceOffline as problem:
            return json_response({
                'error': _('The camera is not responding: %(error)s') % {
                    'error': problem}}, 424)
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
            return json_response({
                'error': _('This camera cannot be moved from here.')}, 409)
        try:
            camera.ptz_stop()
        except live.ServiceOffline as problem:
            return json_response({
                'error': _('The camera is not responding: %(error)s') % {
                    'error': problem}}, 424)
        return json_response({'moving': False})
