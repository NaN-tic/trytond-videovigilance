# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import json
from datetime import datetime

from dominate.tags import div, h2, img, li, small, span, strong, ul
from trytond.model import fields
from trytond.pool import Pool
from trytond.transaction import without_check_access

from ..push import local_time, web_user_for
from ..voyager import (
    Component, Endpoint, application, base, component, csrf, current_user, guard,
    json_response, mark_read, url)


class MessageItem(Component):
    "An item in the notice list, with its snapshot"
    __name__ = 'videovigilance.web.message_item'
    __slots__ = ()

    message = fields.Many2One('notification.web.message', 'message')
    highlight = fields.Boolean('highlight')

    def render(self):
        message = self.message
        classes = 'py-3'
        attributes = {}
        if message.read_at is None:
            classes += ' cursor-pointer'
            attributes = {
                'hx-post': url('message_read', message_id=message.id),
                'hx-vals': json.dumps({'csrf': csrf()}),
                'hx-target': '#messages', 'hx-swap': 'outerHTML'}
        if self.highlight:
            classes += ' -mx-2 rounded-lg bg-gray-700/60 px-2'
        with li(cls=classes, **attributes) as root:
            with strong(cls='block text-gray-100'):
                if message.read_at is None:
                    span(cls='mr-2 inline-block h-2 w-2 rounded-full bg-teal-300')
                span(message.title)
            span(message.body, cls='block text-gray-300')
            small(local_time(message.create_date), cls='block text-gray-400')
            path = message.snapshot_path() if message.expires_at > datetime.now() else None
            if path:
                img(src=base() + path, alt='Captura del movimiento', loading='lazy',
                    cls='mt-2 block w-full rounded-lg border border-white/10')
        return root


class Messages(Endpoint):
    "Notice list"
    __name__ = 'videovigilance.web.messages'
    __slots__ = ()
    _url = '/_messages'
    _method = ['GET']

    highlight = fields.Integer('highlight')

    def render(self):
        user = current_user()
        if user is None:
            return json_response({'error': 'Sesión no iniciada'}, 401)
        app = application()
        items = []
        if app is not None:
            web_user = web_user_for(user)
            Message = Pool().get('notification.web.message')
            with without_check_access():
                items = Message.search([
                        ('application', '=', app.id), ('user', '=', web_user.id)],
                    order=[('create_date', 'DESC'), ('id', 'DESC')], limit=30)
        with div(id='messages', cls='mb-3 rounded-xl border border-white/10 '
                'bg-gray-800/60 p-4', **{
                    'hx-get': url('messages'), 'hx-trigger': 'every 20s',
                    'hx-swap': 'outerHTML'}) as root:
            h2('Alarmas', cls='mb-2 text-sm font-semibold text-gray-400')
            with ul(cls='divide-y divide-white/10'):
                if not items:
                    li('Todavía no hay avisos.', cls='py-3 text-gray-400')
                for message in items:
                    component('message_item', message=message,
                        highlight=message.id == self.highlight)
        return root


class MessageRead(Endpoint):
    "Mark a notice as read"
    __name__ = 'videovigilance.web.message_read'
    __slots__ = ()
    _url = '/messages/<int:message_id>/read'
    _method = ['POST']

    message_id = fields.Integer('message_id')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        mark_read(user, self.message_id)
        return component('messages').tag()
