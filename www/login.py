# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from dominate.tags import button, div, form, input_, label, p
from trytond.exceptions import RateLimitException, UserError
from trytond.model import fields
from trytond.pool import Pool
from werkzeug.utils import redirect

from ..push import has_access, web_user_for
from ..voyager import (
    Component, Endpoint, component, csrf_ok, hidden_csrf, page, rotate_session, url)


class LoginForm(Component):
    "Login form using a Tryton user"
    __name__ = 'videovigilance.web.login_form'
    __slots__ = ()

    error = fields.Char('error')

    def render(self):
        with div(id='login') as root:
            if self.error:
                p(self.error, cls='mb-3 rounded-lg border border-red-400 '
                    'bg-red-950/40 px-3 py-2 text-red-100')
            with form(method='post', action=url('login'), cls='space-y-4 rounded-xl '
                    'border border-white/10 bg-gray-800/60 p-4'):
                hidden_csrf()
                with label(cls='block text-sm text-gray-400'):
                    div('Usuario de Tryton')
                    input_(type='text', name='login', autocomplete='username',
                        required=True, autofocus=True,
                        cls='mt-1 block w-full rounded-lg border-white/20 bg-gray-900 '
                        'text-gray-100 focus:border-teal-400 focus:ring-teal-400')
                with label(cls='block text-sm text-gray-400'):
                    div('Contraseña')
                    input_(type='password', name='password',
                        autocomplete='current-password', required=True,
                        cls='mt-1 block w-full rounded-lg border-white/20 bg-gray-900 '
                        'text-gray-100 focus:border-teal-400 focus:ring-teal-400')
                button('Entrar', type='submit', cls='w-full rounded-lg bg-teal-400 px-4 '
                    'py-2.5 font-semibold text-gray-900 hover:bg-teal-300')
        return root


class Login(Endpoint):
    "Log in with a Tryton user"
    __name__ = 'videovigilance.web.login'
    __slots__ = ()
    _url = '/login'
    _method = ['POST']

    login = fields.Char('login')
    password = fields.Char('password')
    csrf = fields.Char('csrf')

    def _fail(self, message, status):
        return page(component('login_form', error=message), status)

    def render(self):
        if not csrf_ok(self.csrf):
            return self._fail('La sesión ha caducado; inténtalo de nuevo.', 403)
        User = Pool().get('res.user')
        try:
            user_id = User.get_login(self.login or '', {'password': self.password or ''})
        except RateLimitException:
            return self._fail('Demasiados intentos; espera unos minutos.', 429)
        if not user_id:
            return self._fail('Usuario o contraseña incorrectos.', 401)
        user = User(user_id)
        if not has_access(user):
            return self._fail('Tu usuario no tiene acceso a esta web.', 403)
        if not user.email:
            return self._fail('Tu usuario de Tryton no tiene correo: añádelo en '
                'Preferencias para poder recibir avisos.', 403)
        try:
            web_user = web_user_for(user)
        except UserError as error:
            return self._fail(str(error.message)[:200], 403)
        rotate_session(user, web_user)
        return redirect(url('index'), code=303)


class Logout(Endpoint):
    "Log out"
    __name__ = 'videovigilance.web.logout'
    __slots__ = ()
    _url = '/logout'
    _method = ['POST']

    csrf = fields.Char('csrf')

    def render(self):
        if csrf_ok(self.csrf):
            rotate_session()
        return redirect(url('index'), code=303)
