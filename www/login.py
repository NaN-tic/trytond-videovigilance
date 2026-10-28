# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from dominate.tags import a, button, div, form, input_, label, p
from trytond.exceptions import RateLimitException
from trytond.model import fields
from trytond.transaction import Transaction
from werkzeug.utils import redirect

from ..push import authenticate_web_user
from ..voyager import (
    Component, Endpoint, access_problem, component, csrf_ok, hidden_csrf, page,
    rotate_session, tryton_user, url)


class LoginForm(Component):
    "Login form of a web account, with the way in for a Tryton user"
    __name__ = 'videovigilance.web.login_form'
    __slots__ = ()

    error = fields.Char('error')

    def render(self):
        problem = access_problem(tryton_user())
        with div(id='login') as root:
            if self.error:
                p(self.error, cls='mb-3 rounded-lg border border-red-400 '
                    'bg-red-950/40 px-3 py-2 text-red-100')
            if problem:
                p(problem[1], cls='mb-3 text-gray-100')
            with form(method='post', action=url('login'), cls='space-y-4 rounded-xl '
                    'border border-white/10 bg-gray-800/60 p-4'):
                hidden_csrf()
                with label(cls='block text-sm text-gray-400'):
                    div('Correo')
                    input_(type='email', name='email', autocomplete='username',
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
            # Sao takes the database from the first segment of the URL hash.
            a('Abrir Tryton', href='/#' + Transaction().database.name,
                cls='mt-3 inline-block text-sm font-semibold text-teal-300 '
                'hover:text-teal-200')
        return root


class Login(Endpoint):
    "Log in with the email and password of a web account"
    __name__ = 'videovigilance.web.login'
    __slots__ = ()
    _url = '/login'
    _method = ['POST']

    email = fields.Char('email')
    password = fields.Char('password')
    csrf = fields.Char('csrf')

    def _fail(self, message, status):
        return page(component('login_form', error=message), status)

    def render(self):
        if not csrf_ok(self.csrf):
            return self._fail('La sesión ha caducado; inténtalo de nuevo.', 403)
        try:
            account = authenticate_web_user(self.email, self.password)
        except RateLimitException:
            return self._fail('Demasiados intentos; espera unos minutos.', 429)
        if account is None:
            return self._fail('Correo o contraseña incorrectos.', 401)
        rotate_session(account)
        return redirect(url('index'), code=303)


class Logout(Endpoint):
    "Log out of a web account. A Tryton session is ended in Tryton."
    __name__ = 'videovigilance.web.logout'
    __slots__ = ()
    _url = '/logout'
    _method = ['POST']

    csrf = fields.Char('csrf')

    def render(self):
        if csrf_ok(self.csrf):
            rotate_session()
        return redirect(url('index'), code=303)
