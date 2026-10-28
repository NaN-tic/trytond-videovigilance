# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Base for the alarm and notices web app, following NaN-tic Voyager site structure.

  voyager.py   Site (www.site), base Component/Endpoint classes, Layout and Error,
               plus shared session and security utilities.
  www/         Pages: one file per screen, registered with www.register().
  voyager/     Static assets (htmx, Tailwind CSS, device JS) and their configuration.
  routes.py    Voyager dispatch and static asset serving.

Voyager runs outside RPC, without Tryton permission checks, so authorization
is handled explicitly here (session, group, and CSRF token).
"""

import hashlib
import hmac
import json
import secrets
from datetime import datetime

from dominate import document
from dominate.tags import a, div, h1, input_, link, meta, p, script
from trytond.model import fields
from trytond.modules.voyager.voyager import (
    Component as VoyagerComponent, Endpoint as VoyagerEndpoint)
from trytond.pool import Pool, PoolMeta
from trytond.protocols.wrappers import Response
from trytond.transaction import Transaction, without_check_access

from .assets import VERSION
from .push import APPLICATION, has_access, is_admin, web_user_for

SITE_TYPE = 'videovigilance_alarm'

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'")
# same-origin (not no-referrer): with no-referrer, browsers send
# «Origin: null» with forms, and Tryton rejects that origin.
SECURITY_HEADERS = {
    'Content-Security-Policy': CSP,
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin',
    }


class Site(metaclass=PoolMeta):
    __name__ = 'www.site'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.type.selection.append((SITE_TYPE, "Video Surveillance: Alarm and Notices"))

    @classmethod
    def set_session_cookie(cls, response, session):
        """Voyager's final step before responding: set the session cookie and headers.

        Voyager returns all HTML without a charset or security headers and with
        a cookie lacking HttpOnly/Secure; correct that for this site.
        """
        if session.site.type != SITE_TYPE:
            return super().set_session_cookie(response, session)
        context = Transaction().context.get('voyager_context')
        prefix = (getattr(context, 'web_prefix', None) or '') + '/'
        # Secure only when the request came over https: served locally over plain http
        # (127.0.0.1) a Secure cookie depends on each browser's loopback exception.
        secure = bool(getattr(getattr(context, 'request', None), 'is_secure', False))
        response.set_cookie(
            'session_id', session.session_id, max_age=session.site.session_lifetime,
            path=prefix, secure=secure, httponly=True, samesite='Strict')
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if response.mimetype == 'text/html':
            response.headers['Content-Type'] = 'text/html; charset=utf-8'


class Component(VoyagerComponent):
    __slots__ = ()
    _cached = False


class Endpoint(Component, VoyagerEndpoint):
    __slots__ = ()
    _type = SITE_TYPE

    def lazy_content(self):
        # Voyager's indicator uses inline styles, which the CSP does not allow.
        return div('Cargando…', cls='py-3 text-sm text-gray-400')


def component(name, **values):
    """Instantiate a component from this web app.

    Always use the Pool: Tryton only completes configuration (fields, _record)
    on the registered model class, not on the module's class.
    """
    return Pool().get('videovigilance.web.' + name)(**values)


def page(content, status=None, title=None):
    """Full page using the Layout; with `status`, a response with that status code.

    Voyager only supports a status code when returning the entire response.
    `content` can be a tag or a component.
    """
    if isinstance(content, VoyagerComponent):
        content = content.tag()
    layout = component('layout', title=title)
    layout.main.add(content)
    tag = layout.tag()
    if status is None:
        return tag
    return Response(tag.render().replace('hx_', 'hx-'), status,
        content_type='text/html; charset=utf-8')


# --------------------------------------------------------------------------
# Session and security
# --------------------------------------------------------------------------


def voyager():
    return Transaction().context['voyager_context']


def base():
    return (voyager().web_prefix or '') + '/'


def session():
    return voyager().session


def url(name, **values):
    "URL for an endpoint in this web app, built by Voyager (including the prefix)."
    return Pool().get('videovigilance.web.' + name).url(**values)


def csrf():
    return hmac.new(session().session_id.encode(), b'videovigilance_web',
        hashlib.sha256).hexdigest()


def csrf_ok(token):
    return bool(token) and hmac.compare_digest(token, csrf())


def current_user():
    """Tryton user with an active session and web access, or None."""
    user = session().system_user
    return user if user is not None and has_access(user) else None


def application():
    Application = Pool().get('notification.web.application')
    with without_check_access():
        applications = Application.search([('code', '=', APPLICATION)], limit=1)
    return applications[0] if applications else None


def json_response(data, status=200):
    return Response(json.dumps(data), status, content_type='application/json')


def guard(token):
    """Return (user, None) or (None, error response)."""
    user = current_user()
    if user is None:
        return None, json_response({'error': 'Sesión no iniciada'}, 401)
    if not csrf_ok(token):
        return None, json_response({'error': 'Token CSRF no válido'}, 403)
    return user, None


def hidden_csrf():
    input_(type='hidden', name='csrf', value=csrf())


def nav(active):
    """Links between the pages of the web app, with the current one highlighted.

    The camera page (live video and movement) is only offered to administrators.
    Create it inside the parent's «with» block: it attaches itself there.
    """
    user = current_user()
    pages = [('index', 'Alarmas'), ('events', 'Eventos')]
    if user is not None and is_admin(user):
        pages.append(('live', 'Cámara'))
    with div(cls='mb-4 flex gap-2 text-sm') as root:
        for name, text in pages:
            a(text, href=url(name), cls='rounded-lg px-3 py-1.5 font-semibold ' + (
                    'bg-teal-400 text-gray-900' if name == active else
                    'border border-white/20 text-gray-100 hover:bg-white/5'))
    return root


def rotate_session(user=None, web_user=None):
    """Change the session ID on login or logout (preventing session fixation)."""
    current = session()
    current.system_user = user
    current.user = web_user
    current.session_id = secrets.token_urlsafe()
    current.save()


def mark_read(user, message_id):
    app = application()
    if app is None:
        return
    Message = Pool().get('notification.web.message')
    web_user = web_user_for(user)
    with without_check_access():
        unread = Message.search([
                ('id', '=', message_id), ('application', '=', app.id),
                ('user', '=', web_user.id), ('read_at', '=', None)])
        if unread:
            Message.write(unread, {'read_at': datetime.now()})


# --------------------------------------------------------------------------
# Layout and errors
# --------------------------------------------------------------------------

class Layout(Component):
    'Layout'
    __name__ = 'videovigilance.web.layout'
    __slots__ = ['main']

    title = fields.Char('Title')

    def __init__(self, *args, **kwargs):
        self.main = div(cls='mx-auto w-full max-w-xl px-4 pb-6 pt-4')
        super().__init__(*args, **kwargs)

    def render(self):
        site = self.site
        web_prefix = (self.web_prefix() or '').rstrip('/')
        static_path = f'{web_prefix}/static'
        title = self.title or site.title or 'Videovigilància'

        doc = document(title=title)
        doc.set_attribute('lang', 'es')
        doc.set_attribute('class', 'bg-gray-900')
        with doc.head:
            meta(charset='utf-8')
            meta(name='viewport', content='width=device-width, initial-scale=1')
            meta(name='theme-color', content='#111827')
            if site.metadescription:
                meta(name='description', content=site.metadescription)
            meta(name='htmx-config', content='{"includeIndicatorStyles": false}')
            link(rel='manifest', href=f'{web_prefix}/manifest.webmanifest?v={VERSION}')
            link(rel='apple-touch-icon', href=f'{static_path}/apple-touch-icon.png?v={VERSION}')
            link(rel='stylesheet', href=f'{static_path}/output.css?v={VERSION}')
            script(src=f'{static_path}/htmx.min.js?v={VERSION}', defer=True)
            script(src=f'{static_path}/app.js?v={VERSION}', defer=True)
            script(src=f'{static_path}/live.js?v={VERSION}', defer=True)
        # Add explicitly and in order: dominate attaches tags created inside a
        # «with» to the parent when exiting it, which means after this point.
        doc.body.set_attribute('class', 'min-h-screen bg-gray-900 text-gray-100')
        doc.body.add(h1(title, cls='mx-auto w-full max-w-xl px-4 pt-6 text-xl '
            'font-semibold text-gray-100'))
        doc.body.add(self.main)
        return doc


class Error(Endpoint):
    'Error'
    __name__ = 'videovigilance.web.error'
    __slots__ = ()
    _url = '/error'
    _status = [400, 403, 404, 405, 500]

    status = fields.Integer('Status')

    TITLES = {
        400: 'Petición no válida',
        403: 'Acceso denegado',
        404: 'Página no encontrada',
        405: 'Método no permitido',
        500: 'Error del servidor',
        }

    def get_status(self):
        return self.status if self.status in self.TITLES else 404

    def render(self):
        status = self.get_status()
        title = self.TITLES[status]
        with div(cls='py-16 text-center') as content:
            p(str(status), cls='text-6xl font-semibold tracking-tight text-gray-100')
            p(title, cls='mt-4 text-xl text-gray-300')
            a('Volver al inicio →', href=url('index'),
                cls='mt-8 inline-block font-semibold text-teal-300 hover:text-teal-200')
        return page(content, title=f'{status} {title}')
