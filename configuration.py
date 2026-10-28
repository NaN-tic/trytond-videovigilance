# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import logging
import time
from datetime import datetime, timedelta

from trytond.model import ModelSingleton, ModelSQL, ModelView, fields
from trytond.pool import Pool, PoolMeta
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction, without_check_access
from trytond.worker import run_task

from .camera import STATUSES

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
        "Alarm Armed",
        help="If disabled, events are recorded anyway but no notice is sent.")
    alarm_changed_at = fields.DateTime("Last Change", readonly=True)
    alarm_changed_by = fields.Many2One(
        'res.user', "Changed By", readonly=True, ondelete='SET NULL')

    camera_heartbeat_interval = fields.Integer(
        "Camera Heartbeat Interval (s)", required=True,
        domain=[('camera_heartbeat_interval', '>=', MIN_INTERVAL)],
        help="Every how many seconds the service checks that each camera responds and delivers "
             "video.")
    camera_heartbeat_failures = fields.Integer(
        "Failed Checks Before Alerting", required=True,
        domain=[('camera_heartbeat_failures', '>=', 1)],
        help="Consecutive failed checks before a camera is considered down and an alert is "
             "raised. Avoids false alerts caused by brief outages.")
    service_heartbeat_interval = fields.Integer(
        "Service Heartbeat Interval (s)", required=True,
        domain=[('service_heartbeat_interval', '>=', MIN_INTERVAL)],
        help="Every how many seconds the detection service communicates with Tryton.")
    service_heartbeat_misses = fields.Integer(
        "Missed Heartbeats Before Alerting", required=True,
        domain=[('service_heartbeat_misses', '>=', 1)],
        help="Missed heartbeats, in addition to the one that is due, before warning that the "
             "service does not respond: the service is considered down once the interval times "
             "(heartbeats + 1) has passed. It is checked at every interval.")
    service_status = fields.Selection(
        STATUSES, "Service Status", required=True, readonly=True)
    service_last_seen = fields.DateTime("Last Service Heartbeat", readonly=True)
    watchdog_checked_at = fields.DateTime("Last Review", readonly=True)
    watchdog_grace_from = fields.DateTime("Grace From", readonly=True)
    ptz_quiet_seconds = fields.Integer(
        "Notice Silence When the Camera Moves (s)", required=True,
        domain=[('ptz_quiet_seconds', '>=', 0)],
        help="Seconds during which no notices of events of a camera are sent after it has been "
             "moved from the web: when it moves, the whole scene shifts and the detector takes it "
             "for motion. Events are recorded anyway. 0 to never silence.")
    yeastar_pbx = fields.Many2One(
        'yeastar.pbx', "Yeastar PBX", ondelete='SET NULL',
        help="PBX that makes the calls to the managers. Without a PBX nobody is called.")
    yeastar_caller = fields.Char(
        "Caller Number",
        states={'required': Bool(Eval('yeastar_pbx'))},
        help="Number of the PBX the calls are made from: an extension, an IVR, a ring group or a "
             "queue. If it is an extension, that extension rings first.")
    call_cooldown = fields.Integer(
        "Wait Between Calls (s)", required=True,
        domain=[('call_cooldown', '>=', 0)],
        help="After calling the managers of a location they are not called again until this time "
             "has passed (failed calls do not count). 0 to call at every alarm.")
    call_voice = fields.Boolean(
        "Read the Alarm Aloud",
        help="The PBX calls the manager and reads the alarm (location, camera, time and persons), "
             "generated with Google Text-to-Speech. If the voice cannot be generated or played, "
             "the call is made anyway (it only rings).")
    call_voice_name = fields.Char(
        "Voice", states={'required': Bool(Eval('call_voice'))},
        help="Name of the Google Text-to-Speech voice, e.g. es-ES-Standard-A; the language comes "
             "from the name. GOOGLE_APPLICATION_CREDENTIALS is required in the trytond "
             "environment.")
    yeastar_dial_permission = fields.Char(
        "Extension with Dial Permission", states={'required': Bool(Eval('call_voice'))},
        help="Extension of the PBX that has permission for external calls: the PBX uses it to "
             "call the managers when the alarm is read.")

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
    def service_heartbeat(cls, cameras):
        """Detection service heartbeat.

        Records that the service is alive, applies the reported camera statuses
        (list of {'code', 'online', 'detail'}), and returns the current
        intervals for the service to adopt without restarting.
        """
        from .push import local_time
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
                "Watch the Video Surveillance Service Heartbeat"))
