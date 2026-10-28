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
from sql import Null
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import fields
from trytond.modules.voyager.voyager import (
    Component as VoyagerComponent, Endpoint as VoyagerEndpoint)
from trytond.pool import Pool, PoolMeta
from trytond.protocols.wrappers import Response
from trytond.transaction import Transaction, without_check_access
from trytond.wizard import StateAction, Wizard

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


class Session(metaclass=PoolMeta):
    __name__ = 'www.session'

    @classmethod
    def __register__(cls, module):
        super().__register__(module)
        if module != 'videovigilance':
            return
        # The identity of this web app is the user of the Tryton session, never one kept
        # here. Drop what the old login stored (its rows are the ones with a system_user):
        # Voyager prefers a session's user to the one of the request, so a session left
        # behind would keep acting as its old user. Web account sessions are not touched.
        site = Pool().get('www.site').__table__()
        table = cls.__table__()
        cursor = Transaction().connection.cursor()
        cursor.execute(*table.update(
                [table.system_user, table.user], [None, None],
                where=(table.system_user != Null) & table.site.in_(
                    site.select(site.id, where=site.type == SITE_TYPE))))


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
    """CSRF token. With a Tryton session in the request it is bound to it, so ending it
    (logout, expiry) invalidates it; otherwise it is bound to the session of this browser."""
    request = voyager().request
    tryton_session = request.session if request.user_id else None
    secret = (tryton_session.token if tryton_session is not None
        else session().session_id)
    return hmac.new(secret.encode(), b'videovigilance_web', hashlib.sha256).hexdigest()


def csrf_ok(token):
    return bool(token) and hmac.compare_digest(token, csrf())


def tryton_user():
    "The user of the Tryton session of this request (whatever way it logged in), or None."
    user_id = voyager().request.user_id
    return Pool().get('res.user')(user_id) if user_id else None


def web_account():
    "The web account (web.user) logged in with a password in this browser, or None."
    account = session().user
    if account is None:
        return None
    with without_check_access():
        return account if account.active and account.password_hash else None


def access_problem(user):
    "(status, text) of why `user` (see tryton_user) cannot use the web app; None if it can."
    if user is None:
        return 401, 'Inicia sesión para ver los avisos.'
    if not has_access(user):
        return 403, 'Tu usuario de Tryton no tiene acceso a esta web.'
    with without_check_access():
        has_email = bool((user.email or '').strip())
    if not has_email:
        return 403, ('Tu usuario de Tryton no tiene correo: añádelo en Preferencias '
            'para poder recibir avisos.')
    return None


def current_user():
    """Who is using the web app: the user of the Tryton session if it has access, else the
    web account logged in with a password, else None. A web account cannot administer
    (is_admin is false): it only reads and manages its own devices and notices."""
    user = tryton_user()
    if user is not None and access_problem(user) is None:
        return user
    return web_account()


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


def rotate_session(account=None):
    """Change the session ID on login or logout (preventing session fixation).

    `account` is the web account (web.user) that logs in; None logs out."""
    current = session()
    current.user = account
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


class OpenWeb(Wizard):
    "Open the web app from the Tryton menu"
    __name__ = 'videovigilance.open_web'
    start_state = 'open'
    open = StateAction('videovigilance.act_web_url')

    def do_open(self, action):
        web = application()
        if web is None:
            raise UserError(gettext('videovigilance.msg_web_not_configured'))
        action['url'] = web.url()
        return action, {}
