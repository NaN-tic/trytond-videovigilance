# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Alarms: everything that was raised to the devices, with a pending/seen state.

An alarm is created each time a notice is sent (a person detected while the alarm is
armed, a camera going offline or coming back, the detection service falling silent or
coming back). It exists even if no device was subscribed to receive it.
"""
import logging
from datetime import datetime

from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool
from trytond.pyson import Eval
from trytond.transaction import Transaction, without_check_access

logger = logging.getLogger(__name__)
TYPES = [
    ('person_motion', "Moviment de persona"),
    ('person_detection', "Detecció de persona"),
    ('camera_offline', "Càmera sense connexió"),
    ('camera_online', "Càmera recuperada"),
    ('service_down', "Servei de detecció sense senyal"),
    ('service_up', "Servei de detecció recuperat"),
    ]


class Alarm(ModelSQL, ModelView):
    "Alarma de videovigilància"
    __name__ = 'videovigilance.alarm'

    type = fields.Selection(TYPES, "Tipus", required=True, readonly=True)
    camera = fields.Many2One(
        'videovigilance.camera', "Càmera", readonly=True, ondelete='SET NULL',
        help="Buida en les alarmes que no són d'una càmera concreta.")
    event = fields.Many2One(
        'videovigilance.event', "Esdeveniment", readonly=True, ondelete='SET NULL',
        help="Detecció que ha provocat l'alarma.")
    raised_at = fields.DateTime("Data", required=True, readonly=True)
    title = fields.Char("Títol", required=True, readonly=True)
    body = fields.Char("Missatge", readonly=True)
    snapshot = fields.Function(
        fields.Binary("Captura", readonly=True), 'get_snapshot',
        )
    frame = fields.Binary(
        "Fotograma de la càmera", readonly=True,
        help="Darrera imatge de la càmera quan va saltar l'avís d'una caiguda o "
        "recuperació.")
    state = fields.Selection(
        [('pending', "Pendent"), ('seen', "Vista")], "Estat",
        required=True, readonly=True)
    seen_at = fields.DateTime("Vista el", readonly=True)
    seen_by = fields.Many2One(
        'res.user', "Vista per", readonly=True, ondelete='SET NULL')
    calls = fields.One2Many(
        'videovigilance.alarm.call', 'alarm', "Trucades", readonly=True)

    @classmethod
    def get_snapshot(cls, alarms, name):
        "The picture of the alarm: the event's snapshot or the camera's frame kept with it."
        size_only = Transaction().context.get('%s.%s' % (cls.__name__, name)) == 'size'
        result = {}
        for alarm in alarms:
            data = alarm.event.snapshot if alarm.event else alarm.frame
            result[alarm.id] = (len(data) if data else 0) if size_only else data
        return result

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('raised_at', 'DESC'), ('id', 'DESC')]
        cls._buttons.update(
            mark_seen={
                'invisible': Eval('state') == 'seen',
                'depends': ['state'],
                'icon': 'tryton-ok',
                },
            )

    @staticmethod
    def default_state():
        return 'pending'

    @classmethod
    @ModelView.button
    def mark_seen(cls, alarms):
        user = Transaction().user or None
        with without_check_access():
            cls.write([a for a in alarms if a.state == 'pending'], {
                    'state': 'seen', 'seen_at': datetime.now(), 'seen_by': user})

    @classmethod
    def raise_notices(cls, notices):
        """Record one alarm per notice, and request the phone calls it requires.

        notices: dicts with 'type' (one of TYPES), 'title', 'body' and optionally
        'camera' and 'event' (IDs). A notice about a camera without an event (it went
        offline, it came back) keeps the camera's latest frame: the last picture before
        it went dark, which the camera replaces once it is back.
        """
        now = datetime.now()
        with without_check_access():
            Camera = Pool().get('videovigilance.camera')
            cameras = {c.id: c for c in Camera.browse(
                    [n['camera'] for n in notices if n.get('camera') and not n.get('event')])}
            alarms = cls.create([{
                        'type': notice['type'], 'camera': notice.get('camera'),
                        'event': notice.get('event'), 'raised_at': now,
                        'title': notice['title'], 'body': notice['body'],
                        'frame': cameras[notice['camera']].frame
                        if notice.get('camera') in cameras else None,
                        } for notice in notices])
        try:
            Pool().get('videovigilance.alarm.call').request_for(alarms)
        except Exception:
            # Calling is a complement: it must never prevent the alarm from existing.
            logger.exception("No s'han pogut preparar les trucades de l'alarma")
        return alarms
