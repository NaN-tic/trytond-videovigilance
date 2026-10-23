# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""The alarms of one camera, newest first. Opened from the camera's form in Tryton."""
from dominate.tags import a, div, h2, img, li, small, span, strong, ul
from trytond.model import fields
from trytond.pool import Pool
from trytond.transaction import without_check_access

from ..push import local_time
from ..voyager import (
    Endpoint, component, current_user, json_response, nav, page, url, voyager)
from .events import CARD, IMAGE, LINK, PAGE_SIZE, image_response


def _alarms(camera, number):
    "(alarms of page `number`, total)"
    Alarm = Pool().get('videovigilance.alarm')
    domain = [('camera', '=', camera.id)]
    with without_check_access():
        total = Alarm.search_count(domain)
        alarms = Alarm.search(
            domain, order=[('raised_at', 'DESC'), ('id', 'DESC')],
            offset=(number - 1) * PAGE_SIZE, limit=PAGE_SIZE)
        # Whether each alarm has a picture: its event's snapshot or the camera frame kept with it.
        rows = [(alarm, bool(alarm.event.snapshot_id if alarm.event else alarm.frame))
            for alarm in alarms]
    return rows, total


def _alarm(alarm, has_picture):
    with li(cls='py-3') as root:
        strong(alarm.title, cls='block text-gray-100')
        span(alarm.body or '', cls='block text-gray-300')
        small('%s · %s' % (local_time(alarm.raised_at),
                'Pendiente' if alarm.state == 'pending' else 'Vista'),
            cls='block text-gray-400')
        if has_picture:
            img(src=url('alarm_snapshot', alarm_id=alarm.id), alt='Imagen del aviso',
                loading='lazy', cls='mt-2 ' + IMAGE)
    return root


class CameraAlarmsPage(Endpoint):
    "Alarms of one camera"
    __name__ = 'videovigilance.web.camera_alarms'
    __slots__ = ()
    _url = '/camera/<int:camera_id>'
    _method = ['GET']

    camera_id = fields.Integer('camera_id')

    def render(self):
        if current_user() is None:
            return page(component('login_form'))
        Camera = Pool().get('videovigilance.camera')
        with without_check_access():
            cameras = Camera.search([('id', '=', self.camera_id)])
            camera = cameras[0] if cameras else None
        if camera is None:
            return page(div('La cámara no existe.', cls='py-16 text-center text-gray-400'),
                status=404, title='Cámara')
        # Voyager only fills fields from the route; the query string is read here.
        requested = max(1, voyager().request.args.get('page', 1, type=int))
        _, total = _alarms(camera, 1)
        last = max(1, -(-total // PAGE_SIZE))
        number = min(requested, last)  # a page past the end shows the last one
        rows, _ = _alarms(camera, number)
        here = url('camera_alarms', camera_id=camera.id)

        with div(id='camera-alarms') as root:
            component('session_bar')
            nav('events')
            with div(cls=CARD):
                h2('%s · %s' % (camera.name, camera.location.name if camera.location else '-'),
                    cls='mb-2 text-sm font-semibold text-gray-400')
                if total:
                    small('%d en total · página %d de %d' % (total, number, last),
                        cls='mb-2 block text-gray-400')
                with ul(cls='divide-y divide-white/10'):
                    if not rows:
                        li('Esta cámara todavía no tiene avisos.', cls='py-3 text-gray-400')
                    for alarm, has_picture in rows:
                        _alarm(alarm, has_picture)
                with div(cls='mt-3 flex justify-between text-sm'):
                    if number > 1:
                        a('← Más recientes', cls=LINK, href='%s?page=%d' % (here, number - 1))
                    else:
                        span()
                    if number < last:
                        a('Más antiguos →', cls=LINK, href='%s?page=%d' % (here, number + 1))
        return page(root, title='Avisos de la cámara')


class AlarmSnapshot(Endpoint):
    "Picture of an alarm; needs a session"
    __name__ = 'videovigilance.web.alarm_snapshot'
    __slots__ = ()
    _url = '/alarms/<int:alarm_id>/snapshot'
    _method = ['GET']

    alarm_id = fields.Integer('alarm_id')

    def render(self):
        if current_user() is None:
            return json_response({'error': 'Sesión no iniciada'}, 401)
        Alarm = Pool().get('videovigilance.alarm')
        with without_check_access():
            alarms = Alarm.search([('id', '=', self.alarm_id)])
            return image_response(alarms[0].snapshot if alarms else None)
