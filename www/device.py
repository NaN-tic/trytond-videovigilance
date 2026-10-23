# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import hashlib

from dominate.tags import button, div, h2, p
from trytond.exceptions import UserError
from trytond.model import fields
from trytond.modules.notification_web_push.notification import (
    validate_endpoint, validate_subscription_keys)
from trytond.pool import Pool
from trytond.transaction import Transaction, without_check_access

from ..assets import VERSION
from ..push import web_user_for
from ..voyager import (
    Component, Endpoint, application, base, csrf, guard, json_response, url, voyager)


class DeviceCard(Component):
    "Card for enabling notices on this device"
    __name__ = 'videovigilance.web.device_card'
    __slots__ = ()

    def render(self):
        app = application()
        with div(id='device', cls='mb-3 rounded-xl border border-white/10 '
                'bg-gray-800/60 p-4', **{
                    'data-csrf': csrf(),
                    'data-base': base(),
                    'data-subscribe': url('push_subscribe'),
                    'data-unsubscribe': url('push_unsubscribe'),
                    'data-test': url('push_test'),
                    'data-key': app.public_key if app else '',
                    'data-sw': f'{base()}sw.js?v={VERSION}',
                    'data-enabled': '1' if app and app.push_enabled else '0'}) as root:
            h2('Este dispositivo', cls='mb-2 text-sm font-semibold text-gray-400')
            p('Comprobando…', id='device-state', cls='mb-3 text-gray-100')
            with div(cls='flex gap-2'):
                button('Activar avisos', id='toggle', type='button', cls='rounded-lg '
                    'bg-teal-400 px-4 py-2.5 font-semibold text-gray-900 hover:bg-teal-300')
                button('Enviar prueba', id='test', type='button', hidden=True,
                    cls='rounded-lg border border-white/20 px-4 py-2.5 font-semibold '
                    'text-gray-100 hover:bg-white/5')
        return root


class PushSubscribe(Endpoint):
    "Subscribe a device to receive notices"
    __name__ = 'videovigilance.web.push_subscribe'
    __slots__ = ()
    _url = '/push/subscribe'
    _method = ['POST']

    endpoint = fields.Char('endpoint')
    p256dh = fields.Char('p256dh')
    auth = fields.Char('auth')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        app = application()
        if app is None or not app.active:
            return json_response({'error': 'Aplicación no configurada'}, 503)
        if not all((self.endpoint, self.p256dh, self.auth)):
            return json_response({'error': 'Suscripción incompleta'}, 400)
        try:
            validate_endpoint(self.endpoint)
            validate_subscription_keys(self.p256dh, self.auth)
        except (UserError, ValueError) as exception:
            return json_response(
                {'error': str(getattr(exception, 'message', exception))[:200]}, 400)
        Subscription = Pool().get('notification.web.subscription')
        web_user = web_user_for(user)
        endpoint_hash = hashlib.sha256(self.endpoint.encode()).hexdigest()
        values = {
            'application': app.id, 'user': web_user.id,
            'device': (voyager().request.headers.get('User-Agent', '')[:500]
                or 'desconocido'),
            'endpoint': self.endpoint, 'endpoint_hash': endpoint_hash,
            'p256dh': self.p256dh, 'auth': self.auth, 'active': True,
            }
        with without_check_access(), Transaction().set_context(active_test=False):
            existing = Subscription.search([('endpoint_hash', '=', endpoint_hash)])
            if existing:
                Subscription.write(existing, values)
            else:
                Subscription.create([values])
        return json_response({'subscribed': True})


class PushUnsubscribe(Endpoint):
    "Unsubscribe a device"
    __name__ = 'videovigilance.web.push_unsubscribe'
    __slots__ = ()
    _url = '/push/unsubscribe'
    _method = ['POST']

    endpoint = fields.Char('endpoint')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        app = application()
        if app is None or not self.endpoint:
            return json_response({'error': 'Falta el dispositivo'}, 400)
        Subscription = Pool().get('notification.web.subscription')
        web_user = web_user_for(user)
        with without_check_access():
            Subscription.delete(Subscription.search([
                        ('application', '=', app.id), ('user', '=', web_user.id),
                        ('endpoint_hash', '=',
                            hashlib.sha256(self.endpoint.encode()).hexdigest())]))
        return json_response({'subscribed': False})


class PushTest(Endpoint):
    "Send a test notice to the current user"
    __name__ = 'videovigilance.web.push_test'
    __slots__ = ()
    _url = '/push/test'
    _method = ['POST']

    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        app = application()
        if app is None or not app.active:
            return json_response({'error': 'Aplicación no configurada'}, 503)
        pool = Pool()
        Message = pool.get('notification.web.message')
        Delivery = pool.get('notification.web.delivery')
        web_user = web_user_for(user)
        with without_check_access():
            message = Message.publish(app, web_user, title='Aviso de prueba',
                body='Si lees esto, los avisos funcionan en este dispositivo.')
            deliveries = Delivery.search([('message', '=', message.id)]) if message else []
            if not deliveries:
                return json_response({'error': 'Este dispositivo no está suscrito'}, 409)
            # Sent when the transaction is committed, without waiting for the cron job.
            Delivery.__queue__.send(deliveries)
        return json_response({'queued': len(deliveries)})
