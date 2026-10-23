# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import logging
import time
from datetime import datetime, timedelta

from trytond.config import config
from trytond.model import ModelSingleton, ModelSQL, ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.rpc import RPC
from trytond.transaction import Transaction, without_check_access
from trytond.worker import run_task

from .camera import STATUSES, require_service

logger = logging.getLogger(__name__)

MIN_INTERVAL = 5
# If the watchdog has not run for this long (or 3 intervals), Tryton was stopped: we
# cannot tell whether the service has gone silent, so it is given a grace period.
WATCHDOG_GAP = timedelta(seconds=30)
STARTUP_GRACE = timedelta(seconds=60)
# How long one run of the watchdog keeps checking (the cron starts a new run each minute).
WATCH_SPAN_S = 55


class Configuration(ModelSingleton, ModelSQL, ModelView):
    "Configuració de videovigilància"
    __name__ = 'videovigilance.configuration'

    alarm_armed = fields.Boolean(
        "Alarma activada",
        help="Si està desactivada, els esdeveniments es registren igualment "
        "però no s'envia cap avís.")
    alarm_changed_at = fields.DateTime("Darrer canvi", readonly=True)
    alarm_changed_by = fields.Many2One(
        'res.user', "Canviat per", readonly=True, ondelete='SET NULL')

    camera_heartbeat_interval = fields.Integer(
        "Interval del batec de càmeres (s)", required=True,
        domain=[('camera_heartbeat_interval', '>=', MIN_INTERVAL)],
        help="Cada quants segons el servei comprova que cada càmera respon i "
        "entrega vídeo.")
    camera_heartbeat_failures = fields.Integer(
        "Comprovacions fallides per avisar", required=True,
        domain=[('camera_heartbeat_failures', '>=', 1)],
        help="Comprovacions consecutives fallides abans de donar una càmera per "
        "caiguda i avisar. Evita falsos avisos per talls puntuals.")
    service_heartbeat_interval = fields.Integer(
        "Interval del batec del servei (s)", required=True,
        domain=[('service_heartbeat_interval', '>=', MIN_INTERVAL)],
        help="Cada quants segons el servei de detecció es comunica amb Tryton.")
    service_heartbeat_misses = fields.Integer(
        "Batecs perduts per avisar", required=True,
        domain=[('service_heartbeat_misses', '>=', 1)],
        help="Batecs perduts, a més del que ja toca, abans d'avisar que el "
        "servei no respon: es dona el servei per caigut passat l'interval per "
        "(batecs + 1). Es revisa a cada interval.")
    service_status = fields.Selection(
        STATUSES, "Estat del servei", required=True, readonly=True)
    service_last_seen = fields.DateTime("Darrer batec del servei", readonly=True)
    watchdog_checked_at = fields.DateTime("Darrera revisió", readonly=True)
    watchdog_grace_from = fields.DateTime("Marge des de", readonly=True)
    ptz_quiet_seconds = fields.Integer(
        "Silenci d'avisos en moure la càmera (s)", required=True,
        domain=[('ptz_quiet_seconds', '>=', 0)],
        help="Segons durant els quals no s'envien avisos d'esdeveniments d'una "
        "càmera després d'haver-la mogut des de la web: en moure's, tota "
        "l'escena es desplaça i el detector ho pren per moviment. Els "
        "esdeveniments es registren igualment. 0 per no silenciar mai.")
    yeastar_pbx = fields.Many2One(
        'yeastar.pbx', "Centraleta Yeastar", ondelete='SET NULL',
        help="Centraleta que fa les trucades als encarregats. Sense centraleta "
        "no es truca a ningú.")
    yeastar_caller = fields.Char(
        "Número d'origen",
        states={'required': Bool(Eval('yeastar_pbx'))},
        help="Número de la centraleta des del qual es truca: una extensió, una "
        "IVR, un grup de timbre o una cua. Si és una extensió, sonarà primer "
        "aquesta extensió.")
    call_cooldown = fields.Integer(
        "Espera entre trucades (s)", required=True,
        domain=[('call_cooldown', '>=', 0)],
        help="Després de trucar als encarregats d'una ubicació no es torna a "
        "trucar fins que passi aquest temps (les trucades fallides no compten). "
        "0 per trucar a cada alarma.")
    call_voice = fields.Boolean(
        "Llegir l'alarma per veu",
        help="La centraleta truca a l'encarregat i li llegeix l'alarma (ubicació, "
        "càmera, hora i persones), generada amb Google Text-to-Speech. Si la veu "
        "no es pot generar o reproduir, es truca igualment (només sona).")
    call_voice_name = fields.Char(
        "Veu", states={'required': Bool(Eval('call_voice'))},
        help="Nom de la veu de Google Text-to-Speech, p. ex. es-ES-Standard-A; "
        "l'idioma surt del nom. Cal GOOGLE_APPLICATION_CREDENTIALS a l'entorn "
        "de trytond.")
    yeastar_dial_permission = fields.Char(
        "Extensió amb permís de trucada", states={'required': Bool(Eval('call_voice'))},
        help="Extensió de la centraleta que té permís de trucades externes: la "
        "centraleta l'usa per trucar als encarregats quan es llegeix l'alarma.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        # Writes to the database: this cannot be a read-only call.
        cls.__rpc__['heartbeat'] = RPC(readonly=False)
        cls.__rpc__['relay_token'] = RPC()

    @staticmethod
    def default_alarm_armed():
        return True

    @staticmethod
    def default_camera_heartbeat_interval():
        return 5

    @staticmethod
    def default_camera_heartbeat_failures():
        return 1

    @staticmethod
    def default_service_heartbeat_interval():
        return 5

    @staticmethod
    def default_service_heartbeat_misses():
        return 1

    @staticmethod
    def default_service_status():
        return 'unknown'

    @staticmethod
    def default_ptz_quiet_seconds():
        return 10

    @staticmethod
    def default_call_cooldown():
        return 300

    @staticmethod
    def default_call_voice_name():
        return 'es-ES-Standard-A'

    @classmethod
    def is_armed(cls):
        with without_check_access():
            configuration = cls.get_singleton()
        if configuration is None:
            return cls.default_alarm_armed()
        return bool(configuration.alarm_armed)

    @staticmethod
    def _stamp(values):
        values['alarm_changed_at'] = datetime.now()
        values['alarm_changed_by'] = Transaction().user or None

    @classmethod
    def create(cls, vlist):
        vlist = [dict(values) for values in vlist]
        for values in vlist:
            if 'alarm_armed' in values:
                cls._stamp(values)
        return super().create(vlist)

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        args = []
        for configurations, values in zip(actions, actions):
            if 'alarm_armed' in values:
                values = dict(values)
                cls._stamp(values)
            args.extend((configurations, values))
        super().write(*args)

    @classmethod
    def set_alarm(cls, armed):
        "Arms or disarms the alarm; records who changed it and when."
        with without_check_access():
            configuration = cls.get_singleton()
            if configuration is None:
                cls.create([{'alarm_armed': bool(armed)}])
            elif bool(configuration.alarm_armed) != bool(armed):
                cls.write([configuration], {'alarm_armed': bool(armed)})

    @staticmethod
    def _send(notices):
        "A notification failure must never prevent recording the status."
        from .push import notify_users
        try:
            notify_users(notices)
        except Exception:
            logger.exception("No s'han pogut enviar els avisos de salut")

    @classmethod
    def heartbeat(cls, cameras):
        """Detection service heartbeat.

        Records that the service is alive, applies the reported camera statuses
        (list of {'code', 'online', 'detail'}), and returns the current
        intervals for the service to adopt without restarting.
        """
        from .push import local_time
        require_service('videovigilance.msg_heartbeat_forbidden')
        Camera = Pool().get('videovigilance.camera')
        now = datetime.now()
        with without_check_access():
            configuration = cls.get_singleton()
            if configuration is None:
                configuration, = cls.create([{}])
            recovered = configuration.service_status == 'offline'
            cls.write([configuration], {
                    'service_status': 'online', 'service_last_seen': now})
            notices = Camera.apply_heartbeat(
                cameras if isinstance(cameras, list) else [], now)
            if recovered:
                notices.append({
                        'title': 'Servicio de detección recuperado',
                        'type': 'service_up',
                        'body': 'Vuelve a comunicarse · %s' % local_time(now),
                        })
            reply = {
                'camera_interval': configuration.camera_heartbeat_interval,
                'camera_failures': configuration.camera_heartbeat_failures,
                'service_interval': configuration.service_heartbeat_interval,
                }
        cls._send(notices)
        return reply

    @classmethod
    def relay_token(cls):
        """Token for the live relay routes (frames and movement commands).

        The service asks for it over the authenticated RPC channel and then presents it
        on the relay routes, which check it without opening a database transaction.
        """
        from . import live
        require_service('videovigilance.msg_relay_forbidden')
        return {
            'token': live.issue_token(
                config.get('videovigilance', 'secret_key'), Transaction().database.name),
            'expires_in': live.TOKEN_TTL_S,
            }

    @classmethod
    def check_service(cls):
        """Service heartbeat watchdog (periodic task, started every minute).

        Only Tryton can detect that the service has gone silent: if the service
        cannot reach Tryton, it has no other channel to notify the user. A cron job
        cannot run more often than once a minute, so each run checks once per service
        interval until a few seconds before the next run: the silence is noticed in
        about one interval.
        """
        transaction = Transaction()
        deadline = time.monotonic() + WATCH_SPAN_S
        while True:
            interval = cls._check_service_once()
            # Each pass is kept (and its notice sent) without waiting for the end of the run.
            transaction.commit()
            while transaction.tasks:
                run_task(Pool(), transaction.tasks.pop())
            if time.monotonic() + interval >= deadline:
                return
            time.sleep(interval)

    @classmethod
    def _check_service_once(cls):
        "One check of the watchdog. Returns the seconds to wait before the next one."
        from .push import local_time
        now = datetime.now()
        with without_check_access():
            configuration = cls.get_singleton()
            if configuration is None:
                return cls.default_service_heartbeat_interval()
            interval = configuration.service_heartbeat_interval
            if configuration.service_last_seen is None:
                return interval
            values = {'watchdog_checked_at': now}
            checked = configuration.watchdog_checked_at
            if checked is None or now - checked > max(WATCHDOG_GAP, timedelta(seconds=3 * interval)):
                # The watchdog was stopped (Tryton stopped or restarted): the
                # service's silence cannot be counted from before then.
                values['watchdog_grace_from'] = now
                grace = now
            else:
                grace = configuration.watchdog_grace_from
            last_seen = configuration.service_last_seen
            reference = max(last_seen, grace) if grace else last_seen
            # A beat is late until it is a whole interval overdue, and `misses` beats must
            # be lost on top of that, so the normal delay of a beat never raises the alarm.
            limit = timedelta(seconds=interval * (configuration.service_heartbeat_misses + 1))
            if grace and last_seen < grace:
                # Nothing has arrived since Tryton came back: the service needs time to notice.
                limit = max(limit, STARTUP_GRACE)
            notices = []
            if (now - reference > limit
                    and configuration.service_status != 'offline'):
                values['service_status'] = 'offline'
                notices.append({
                        'title': 'Servicio de detección sin señal',
                        'type': 'service_down',
                        'body': 'Sin latidos desde %s. Las cámaras no se '
                        'están vigilando.' % local_time(last_seen),
                        })
            cls.write([configuration], values)
        cls._send(notices)
        return interval


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.append((
                'videovigilance.configuration|check_service',
                "Vigilar el batec del servei de videovigilància"))
