# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Events: motion detections and camera movements in one timeline, newest first."""
from dominate.tags import a, div, h2, img, li, small, span, strong, ul
from trytond.model import fields
from trytond.pool import Pool
from trytond.protocols.wrappers import Response
from trytond.transaction import without_check_access

from ..push import local_time
from ..voyager import (
    Endpoint, component, current_user, json_response, nav, page, url, voyager)

PAGE_SIZE = 12
# filter name -> (label, event type or None for all)
FILTERS = {
    'all': ('Todos', None),
    'persons': ('Personas detectadas', 'person_detection'),
    'motion': ('Personas en movimiento', 'person_motion'),
    'movements': ('Cámara movida', 'camera_movement'),
    }
CARD = 'mb-3 rounded-xl border border-white/10 bg-gray-800/60 p-4'
LINK = 'text-teal-300 hover:text-teal-200'
IMAGE = 'block w-full rounded-lg border border-white/10'


def _page(kind, number):
    "(events of page `number`, total, ids of the events that raised an alarm)"
    pool = Pool()
    Event = pool.get('videovigilance.event')
    Alarm = pool.get('videovigilance.alarm')
    domain = [('type', '=', FILTERS[kind][1])] if FILTERS[kind][1] else []
    with without_check_access():
        total = Event.search_count(domain)
        events = Event.search(
            domain, order=[('started_at', 'DESC'), ('id', 'DESC')],
            offset=(number - 1) * PAGE_SIZE, limit=PAGE_SIZE)
        alarmed = {row['event'] for row in Alarm.search_read(
                [('event', 'in', [event.id for event in events])],
                fields_names=['event'])}
        rows = [(event, event.camera.name, event.id in alarmed) for event in events]
    return rows, total


def _detection(event, camera, alarmed):
    with li(cls='py-3') as root:
        strong('%d persona(s) %s' % (
                event.max_persons or 1,
                'en movimiento' if event.type == 'person_motion' else 'detectada(s)'),
            cls='block text-gray-100')
        span('%s · %s · confianza %d %%' % (
                camera, local_time(event.started_at),
                round((event.max_confidence or 0) * 100)), cls='block text-gray-300')
        if not alarmed:
            small('Sin aviso', cls='block text-gray-400',
                title='La alarma estaba desactivada, se había movido la cámara hacía '
                'poco o no había dispositivos suscritos.')
        if event.snapshot_id:
            img(src=url('detection_snapshot', event_id=event.id),
                alt='Captura de la detección', loading='lazy', cls='mt-2 ' + IMAGE)
    return root


def _frame(event, which, label, present):
    with div() as root:
        small(label, cls='mb-1 block text-gray-400')
        if present:
            img(src=url('movement_frame', movement_id=event.id, which=which),
                alt=label, loading='lazy', cls=IMAGE)
        else:
            div('Sin imagen', cls='flex aspect-video items-center justify-center '
                'rounded-lg border border-white/10 text-sm text-gray-400')
    return root


def _movement(event, camera):
    if event.ended_at is None:
        when = '%s · en curso' % local_time(event.started_at)
    else:
        seconds = max(0, round((event.ended_at - event.started_at).total_seconds()))
        when = '%s → %s · %d s' % (
            local_time(event.started_at), local_time(event.ended_at, '%H:%M:%S'), seconds)
    with li(cls='py-3') as root:
        strong('Cámara movida %s' % (event.directions or ''), cls='block text-gray-100')
        span('%s · %s' % (camera, when), cls='block text-gray-300')
        if event.user:
            small('Por %s' % (event.user.name or event.user.login),
                cls='block text-gray-400')
        with div(cls='mt-2 grid grid-cols-2 gap-2'):
            _frame(event, 'start', 'Al empezar', bool(event.start_frame_id))
            _frame(event, 'end', 'Al terminar', bool(event.end_frame_id))
    return root


class EventsPage(Endpoint):
    "Detections and camera movements, newest first"
    __name__ = 'videovigilance.web.events'
    __slots__ = ()
    _url = '/events'
    _method = ['GET']

    def render(self):
        if current_user() is None:
            return page(component('login_form'))
        # Voyager only fills fields from the route; the query string is read here.
        args = voyager().request.args
        kind = args.get('type', 'all')
        if kind not in FILTERS:
            kind = 'all'
        requested = max(1, args.get('page', 1, type=int))
        _, total = _page(kind, 1)
        last = max(1, -(-total // PAGE_SIZE))
        number = min(requested, last)  # a page past the end shows the last one
        rows, _ = _page(kind, number)

        with div(id='events') as root:
            component('session_bar')
            nav('events')
            with div(cls=CARD):
                h2('Eventos', cls='mb-2 text-sm font-semibold text-gray-400')
                with div(cls='mb-2 flex flex-wrap gap-1 text-sm'):
                    for name, (text, _type) in FILTERS.items():
                        a(text, href='%s?type=%s' % (url('events'), name),
                            cls='rounded-full px-3 py-1 ' + (
                                'bg-white/15 font-semibold text-gray-100'
                                if name == kind else LINK))
                if total:
                    small('%d en total · página %d de %d' % (total, number, last),
                        cls='mb-2 block text-gray-400')
                with ul(cls='divide-y divide-white/10'):
                    if not rows:
                        li('Todavía no hay eventos.', cls='py-3 text-gray-400')
                    for event, camera, alarmed in rows:
                        if event.type == 'camera_movement':
                            _movement(event, camera)
                        else:
                            _detection(event, camera, alarmed)
                with div(cls='mt-3 flex justify-between text-sm'):
                    if number > 1:
                        a('← Más recientes', cls=LINK, href='%s?type=%s&page=%d' % (
                                url('events'), kind, number - 1))
                    else:
                        span()
                    if number < last:
                        a('Más antiguos →', cls=LINK, href='%s?type=%s&page=%d' % (
                                url('events'), kind, number + 1))
        return page(root, title='Eventos')


def image_response(data):
    if not data:
        return json_response({'error': 'No existe'}, 404)
    return Response(bytes(data), 200, content_type='image/jpeg', headers={
            'Cache-Control': 'private, max-age=3600', 'X-Content-Type-Options': 'nosniff'})


class DetectionSnapshot(Endpoint):
    "Snapshot of one detection; needs a session, but not to be an administrator"
    __name__ = 'videovigilance.web.detection_snapshot'
    __slots__ = ()
    _url = '/detections/<int:event_id>/snapshot'
    _method = ['GET']

    event_id = fields.Integer('event_id')

    def render(self):
        if current_user() is None:
            return json_response({'error': 'Sesión no iniciada'}, 401)
        Event = Pool().get('videovigilance.event')
        with without_check_access():
            events = Event.search([('id', '=', self.event_id)])
            return image_response(events[0].snapshot if events else None)


class MovementFrame(Endpoint):
    "Picture from the start or the end of a camera movement; needs a session"
    __name__ = 'videovigilance.web.movement_frame'
    __slots__ = ()
    _url = '/movements/<int:movement_id>/<which>'
    _method = ['GET']

    movement_id = fields.Integer('movement_id')
    which = fields.Char('which')

    def render(self):
        if current_user() is None:
            return json_response({'error': 'Sesión no iniciada'}, 401)
        if self.which not in ('start', 'end'):
            return json_response({'error': 'No existe'}, 404)
        Event = Pool().get('videovigilance.event')
        with without_check_access():
            events = Event.search(
                [('id', '=', self.movement_id), ('type', '=', 'camera_movement')])
            return image_response(getattr(events[0], self.which + '_frame') if events else None)
