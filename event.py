# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import logging
from datetime import datetime, timedelta

from trytond.model import Check, ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool
from trytond.pyson import Eval
from trytond.transaction import without_check_access

logger = logging.getLogger(__name__)

# Which fields apply to each kind of event.
PERSON_TYPES = ['person_motion', 'person_detection']
PERSON = {'invisible': ~Eval('type').in_(PERSON_TYPES)}
MOVEMENT = {'invisible': Eval('type') != 'camera_movement'}

NOTICES = {
    'person_motion': 'Movimiento de persona',
    'person_detection': 'Persona detectada',
    }


class Event(ModelSQL, ModelView):
    "Esdeveniment de videovigilància"
    __name__ = 'videovigilance.event'

    uid = fields.Char(
        "Identificador", required=True, readonly=True,
        help="Identificador únic generat pel servei de detecció; "
        "evita duplicats en els reintents.")
    camera = fields.Many2One(
        'videovigilance.camera', "Càmera", required=True, readonly=True,
        ondelete='RESTRICT')
    type = fields.Selection(
        [('person_motion', "Moviment de persona"),
            ('person_detection', "Detecció de persona"),
            ('camera_movement', "Moviment de càmera")],
        "Tipus", required=True, readonly=True)
    started_at = fields.DateTime("Inici", required=True, readonly=True)
    ended_at = fields.DateTime(
        "Final", readonly=True,
        help="Buit mentre la càmera encara es mou.")
    max_persons = fields.Integer(
        "Màxim de persones", readonly=True, states=PERSON,
        domain=[('max_persons', '>=', 0)])
    max_confidence = fields.Float(
        "Confiança màxima", readonly=True, states=PERSON,
        domain=[('max_confidence', '>=', 0), ('max_confidence', '<=', 1)])
    snapshot = fields.Binary(
        "Captura", readonly=True, states=PERSON,
        file_id='snapshot_id', store_prefix='videovigilance')
    snapshot_id = fields.Char("ID del fitxer", readonly=True)
    user = fields.Many2One(
        'res.user', "Usuari", readonly=True, ondelete='SET NULL', states=MOVEMENT,
        help="Qui ha mogut la càmera.")
    directions = fields.Char(
        "Direccions", readonly=True, states=MOVEMENT,
        help="Cap a on s'ha mogut la càmera, en l'ordre en què s'ha fet.")
    last_command_at = fields.DateTime("Darrera ordre", readonly=True)
    start_frame = fields.Binary(
        "Imatge d'inici", readonly=True, states=MOVEMENT,
        file_id='start_frame_id', store_prefix='videovigilance',
        help="Imatge just abans de començar a moure's (només si algú mirava "
        "el vídeo en directe).")
    start_frame_id = fields.Char("ID del fitxer d'inici", readonly=True)
    end_frame = fields.Binary(
        "Imatge final", readonly=True, states=MOVEMENT,
        file_id='end_frame_id', store_prefix='videovigilance')
    end_frame_id = fields.Char("ID del fitxer final", readonly=True)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('uid_unique', Unique(table, table.uid),
                'videovigilance.msg_event_uid_unique'),
            ('period_valid', Check(table, table.ended_at >= table.started_at),
                'videovigilance.msg_event_period_invalid'),
            ]
        cls._order = [('started_at', 'DESC'), ('id', 'DESC')]

    @classmethod
    def create(cls, vlist):
        events = super().create(vlist)
        try:
            cls.notify(events)
        except Exception:
            # A notification failure must never prevent the event from being recorded.
            logger.exception("No s'han pogut enviar els avisos dels esdeveniments")
        return events

    @classmethod
    def create_once(cls, values):
        "Create the event of `values` unless one with its uid exists: (event, created)."
        events = cls.search([('uid', '=', values['uid'])], limit=1)
        if events:
            return events[0], False
        event, = cls.create([values])
        return event, True

    @classmethod
    def notify(cls, events):
        from .push import local_time, notify_users
        # Only people raise alarms; camera movements are just recorded.
        events = [event for event in events if event.type in PERSON_TYPES]
        if not events:
            return
        Configuration = Pool().get('videovigilance.configuration')
        if not Configuration.is_armed():
            return
        with without_check_access():
            configuration = Configuration.get_singleton()
            quiet = timedelta(seconds=(
                    configuration.ptz_quiet_seconds if configuration is not None
                    else Configuration.default_ptz_quiet_seconds()))
            now = datetime.now()
            # Moving the camera shifts the whole scene, which looks like motion; a
            # person in view is still a person, so only motion is muted.
            events = [event for event in events
                if event.type == 'person_detection'
                or not (event.camera.ptz_moved_at
                    and now - event.camera.ptz_moved_at < quiet)]
            if not events:
                return
            notify_users([{
                        'title': NOTICES[event.type],
                        'body': '%s · %s · %d persona(s)' % (
                            event.camera.name, local_time(event.started_at),
                            event.max_persons or 1),
                        'event': event.id,
                        'camera': event.camera.id,
                        'type': event.type,
                        } for event in events])
