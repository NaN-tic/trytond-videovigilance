# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from dominate.tags import button, div, form, h2, input_, p, small
from trytond.model import fields
from trytond.pool import Pool
from trytond.transaction import without_check_access

from ..push import is_admin, local_time
from ..voyager import (
    Endpoint, component, current_user, guard, hidden_csrf, json_response, url)


class AlarmCard(Endpoint):
    "Alarm card"
    __name__ = 'videovigilance.web.alarm_card'
    __slots__ = ()
    _url = '/_alarm'
    _method = ['GET']

    def render(self):
        user = current_user()
        if user is None:
            return json_response({'error': 'Sesión no iniciada'}, 401)
        Configuration = Pool().get('videovigilance.configuration')
        armed = Configuration.is_armed()
        with without_check_access():
            configuration = Configuration.get_singleton()
        state = ('text-teal-300' if armed else 'text-red-400')
        with div(id='alarm', cls='mb-3 rounded-xl border border-white/10 '
                'bg-gray-800/60 p-4') as root:
            h2('Alarma', cls='mb-2 text-sm font-semibold text-gray-400')
            p('ACTIVADA' if armed else 'DESACTIVADA',
                cls=f'mb-1 text-3xl font-bold {state}')
            if configuration is not None and configuration.alarm_changed_at:
                who = configuration.alarm_changed_by
                small('Último cambio: %s%s' % (
                    local_time(configuration.alarm_changed_at),
                    ' · ' + (who.name or who.login) if who else ''),
                    cls='mb-3 block text-gray-400')
            if is_admin(user):
                attributes = {
                    'method': 'post', 'action': url('alarm_switch'),
                    'hx-post': url('alarm_switch'), 'hx-target': '#alarm',
                    'hx-swap': 'outerHTML'}
                if armed:
                    attributes['hx-confirm'] = '¿Desactivar la alarma? No se enviarán avisos.'
                with form(**attributes):
                    hidden_csrf()
                    input_(type='hidden', name='armed', value='0' if armed else '1')
                    if armed:
                        button('Desactivar alarma', type='submit', cls='rounded-lg '
                            'bg-red-400 px-4 py-2.5 font-semibold text-red-950 '
                            'hover:bg-red-300')
                    else:
                        button('Activar alarma', type='submit', cls='rounded-lg '
                            'bg-teal-400 px-4 py-2.5 font-semibold text-gray-900 '
                            'hover:bg-teal-300')
            else:
                small('Solo un administrador puede cambiarla.', cls='text-gray-400')
        return root


class AlarmSwitch(Endpoint):
    "Arm or disarm the alarm"
    __name__ = 'videovigilance.web.alarm_switch'
    __slots__ = ()
    _url = '/alarm'
    _method = ['POST']

    armed = fields.Char('armed')
    csrf = fields.Char('csrf')

    def render(self):
        user, error = guard(self.csrf)
        if error is not None:
            return error
        if not is_admin(user):
            return json_response({'error': 'Solo un administrador puede cambiar la alarma'}, 403)
        if self.armed not in ('0', '1'):
            return json_response({'error': 'Valor no válido'}, 400)
        Configuration = Pool().get('videovigilance.configuration')
        # Run as the session user so the change records who made it.
        Configuration.set_alarm(self.armed == '1')
        return component('alarm_card').tag()
