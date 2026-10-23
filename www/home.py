# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from dominate.tags import button, div, form, span, strong
from trytond.model import fields
from trytond.modules.voyager.voyager import render_component

from ..voyager import (
    Component, Endpoint, component, current_user, hidden_csrf, mark_read, nav, page, url)


class SessionBar(Component):
    "User with an active session and a logout button"
    __name__ = 'videovigilance.web.session_bar'
    __slots__ = ()

    def render(self):
        user = current_user()
        with div(cls='mb-4 flex items-center gap-2 text-sm text-gray-400') as root:
            span('Sesión:')
            strong(user.name or user.login, cls='text-gray-100')
            with form(method='post', action=url('logout'), cls='ml-auto'):
                hidden_csrf()
                button('Salir', type='submit', cls='text-teal-300 hover:text-teal-200')
        return root


class Dashboard(Component):
    "Main dashboard: session, alarm, device, and notices"
    __name__ = 'videovigilance.web.dashboard'
    __slots__ = ()

    highlight = fields.Integer('highlight')

    def render(self):
        with div(id='dashboard') as root:
            component('session_bar')
            nav('index')
            # Voyager endpoints loaded lazily by HTMX.
            render_component('videovigilance.web.alarm_card', lazy=True)
            component('device_card')
            if self.highlight:
                component('messages', highlight=self.highlight)
            else:
                render_component('videovigilance.web.messages', lazy=True)
        return root


class Index(Endpoint):
    "Home page"
    __name__ = 'videovigilance.web.index'
    __slots__ = ()
    _url = '/'
    _method = ['GET']

    def render(self):
        if current_user() is None:
            return page(component('login_form'))
        return page(component('dashboard'), title='Alarmas')


class NotificationPage(Endpoint):
    "Page opened when a notice is tapped"
    __name__ = 'videovigilance.web.notification'
    __slots__ = ()
    _url = '/notifications/<int:message_id>'
    _method = ['GET']

    message_id = fields.Integer('message_id')

    def render(self):
        user = current_user()
        if user is None:
            return page(component('login_form'))
        mark_read(user, self.message_id)
        return page(component('dashboard', highlight=self.message_id), title='Alarmas')
