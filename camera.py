# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from datetime import datetime

from sql import Literal
from trytond.exceptions import UserError
from trytond.i18n import gettext
from trytond.model import Check, DeactivableMixin, ModelSQL, ModelView, Unique, fields
from trytond.pool import Pool
from trytond.pyson import Bool, Eval
from trytond.transaction import Transaction, without_check_access
from trytond.wizard import StateAction, Wizard

from . import secret

PASSWORD_PLACEHOLDER = 'x' * 10
STATUSES = [
    ('unknown', "Unknown"),
    ('online', "Online"),
    ('offline', "Offline"),
    ]

# A move command stops by itself after this long unless it is renewed, so a lost
# connection can never leave the camera running.
PTZ_HOLD_S = 2.0

# Largest heartbeat frame accepted (bytes). The service sends a small JPEG.
MAX_HEARTBEAT_FRAME = 1024 * 1024
DEVICE_TEXT_MAX = 100
# Camera field, what the camera calls it over ONVIF, and whether it only fills a blank.
DEVICE_FIELDS = (
    ('manufacturer', 'manufacturer', True), ('device_model', 'model', True),
    ('firmware_version', 'firmware', False), ('serial_number', 'serial', False),
    ('hardware_id', 'hardware', False))


def is_frame(data):
    "Whether `data` is a JPEG small enough to be kept as the latest picture of a camera."
    return (isinstance(data, bytes) and 0 < len(data) <= MAX_HEARTBEAT_FRAME
        and data.startswith(b'\xff\xd8'))


class Location(DeactivableMixin, ModelSQL, ModelView):
    "Ubicació de videovigilància"
    __name__ = 'videovigilance.location'

    name = fields.Char("Name", required=True)
    address = fields.Text("Address")
    notes = fields.Text("Notes")
    cameras = fields.One2Many(
        'videovigilance.camera', 'location', "Cameras")
    managers = fields.One2Many(
        'videovigilance.location.manager', 'location', "Managers",
        help="People called when a person alarm goes off on a camera of this location. They are "
             "all called at once.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls._order = [('name', 'ASC'), ('id', 'ASC')]


class Camera(DeactivableMixin, ModelSQL, ModelView):
    "Càmera de videovigilància"
    __name__ = 'videovigilance.camera'

    name = fields.Char("Name", required=True)
    code = fields.Char(
        "Code", required=True,
        help="Unique and stable identifier of the camera.")
    location = fields.Many2One(
        'videovigilance.location', "Location", required=True, ondelete='RESTRICT',
        help="Where the camera is. The managers of the location are the ones called when a person "
             "alarm goes off.")
    manufacturer = fields.Char("Manufacturer")
    device_model = fields.Char("Model")
    firmware_version = fields.Char(
        "Firmware Version", readonly=True,
        help="Reported by the camera over ONVIF, through the detection service.")
    serial_number = fields.Char("Serial Number", readonly=True)
    hardware_id = fields.Char("Hardware Version", readonly=True)
    host = fields.Char(
        "Host", required=True,
        help="IP address or DNS name, without protocol, port or credentials.")
    rtsp_port = fields.Integer(
        "RTSP Port", required=True,
        domain=[('rtsp_port', '>=', 1), ('rtsp_port', '<=', 65535)])
    stream_path = fields.Char(
        "Main Stream", required=True,
        help="Relative RTSP path, for example stream1. Without credentials.")
    secondary_stream_path = fields.Char(
        "Secondary Stream",
        help="Optional relative RTSP path, for example stream2.")
    username = fields.Char("Camera User")
    password = fields.Function(
        fields.Char(
            "Password", strip=False,
            help="Password of the camera. It is stored encrypted and cannot be read back; leave "
                 "it as it is to keep it."),
        'get_password', setter='set_password')
    password_encrypted = fields.Char(
        "Encrypted Password", readonly=True, strip=False)
    onvif_enabled = fields.Boolean("ONVIF Enabled")
    onvif_port = fields.Integer(
        "ONVIF Port",
        states={
            'required': Bool(Eval('onvif_enabled')),
            'invisible': ~Bool(Eval('onvif_enabled')),
            },
        domain=['OR', ('onvif_port', '=', None),
            [('onvif_port', '>=', 1), ('onvif_port', '<=', 65535)]])
    ptz = fields.Boolean("PTZ Supported")
    detect_persons = fields.Boolean(
        "Person Detection",
        help="Creates an event when there is a person in the image, moving or not.")
    detect_motion = fields.Boolean(
        "Motion Detection",
        help="Creates an event when a person moves in the image. A person standing still does not "
             "create one.")
    rotation = fields.Selection(
        [('0', "0°"), ('90', "90°"), ('180', "180°"), ('270', "270°")],
        "Image Rotation", required=True,
        help="Rotation to apply to the image so that people appear upright (e.g. 180° if the "
             "camera is mounted upside down).")
    notes = fields.Text("Notes")
    status = fields.Selection(
        STATUSES, "Status", required=True, readonly=True,
        help="Status of the camera according to the detection service (camera heartbeat). It "
             "stays until the next heartbeat.")
    status_changed_at = fields.DateTime("Status Since", readonly=True)
    status_checked_at = fields.DateTime("Last Check", readonly=True)
    status_detail = fields.Char("Status Detail", readonly=True)
    frame = fields.Binary(
        "Last Frame", readonly=True,
        help="Last image the detection service sent with the heartbeat. It is kept even if the "
             "camera goes down. It is stored in the table and not in the file store: it changes "
             "at every heartbeat.")
    frame_at = fields.DateTime("Frame Time", readonly=True)
    ptz_moved_at = fields.DateTime(
        "Last Movement", readonly=True,
        help="When the last movement of the camera was ordered from the web. For a few seconds no "
             "notices of events of this camera are sent, because when it moves the whole scene "
             "shifts and looks like motion.")

    @classmethod
    def __setup__(cls):
        super().__setup__()
        table = cls.__table__()
        cls._sql_constraints += [
            ('code_unique', Unique(table, table.code),
                'videovigilance.msg_camera_code_unique'),
            ('detection_required', Check(
                    table, (table.detect_persons == Literal(True))
                    | (table.detect_motion == Literal(True))),
                'videovigilance.msg_camera_detection_required'),
            ]
        cls._order = [('name', 'ASC'), ('id', 'ASC')]
        cls._buttons.update(
            open_alarms={'invisible': Eval('id', -1) < 0, 'depends': ['id']})

    @classmethod
    @ModelView.button_action('videovigilance.wizard_camera_open_alarms')
    def open_alarms(cls, cameras):
        pass

    @classmethod
    def __register__(cls, module):
        super().__register__(module)
        table_h = cls.__table_handler__(module)
        table_h.drop_column('password_secret')

    @classmethod
    def get_password(cls, cameras, name):
        with without_check_access():
            return {
                c.id: PASSWORD_PLACEHOLDER if c.password_encrypted else None
                for c in cls.browse(cameras)}

    @classmethod
    def set_password(cls, cameras, name, value):
        if value == PASSWORD_PLACEHOLDER:
            return
        token = secret.encrypt(value) if value else None
        with without_check_access():
            cls.write(cameras, {'password_encrypted': token})

    @classmethod
    def service_config(cls):
        "What the detection service needs of the active cameras, with their credentials."
        with without_check_access():
            return [{
                    'id': c.id, 'code': c.code, 'host': c.host,
                    'rtsp_port': c.rtsp_port, 'stream_path': c.stream_path,
                    'secondary_stream_path': c.secondary_stream_path or None,
                    'username': c.username,
                    'password': (secret.decrypt(c.password_encrypted)
                        if c.password_encrypted else None),
                    'rotation': int(c.rotation), 'ptz': bool(c.ptz),
                    'onvif_enabled': bool(c.onvif_enabled),
                    'onvif_port': c.onvif_port or None,
                    'detect_persons': bool(c.detect_persons),
                    'detect_motion': bool(c.detect_motion),
                    } for c in cls.search([('active', '=', True)])]

    @classmethod
    def store_frame(cls, camera_id, frame):
        "Keep the JPEG `frame` as the latest picture of the camera; False if there is none."
        with without_check_access():
            cameras = cls.search([('id', '=', camera_id)])
            if not cameras:
                return False
            cls.write(cameras, {'frame': frame, 'frame_at': datetime.now()})
        return True

    @classmethod
    def for_web(cls, code):
        "The active camera with this code, or None."
        with without_check_access():
            cameras = cls.search([('code', '=', code), ('active', '=', True)], limit=1)
        return cameras[0] if cameras else None

    @property
    def ptz_available(self):
        return bool(self.ptz and self.onvif_enabled and self.username
            and self.password_encrypted)

    def ptz_move(self, dx, dy, speed, user=None):
        """Ask the service to start moving towards (dx, dy) as seen on screen; speed 0 to 1.

        Raises live.ServiceOffline if the detection service, which is the one that
        reaches the camera, is not connected.
        """
        from . import live
        mailbox = live.mailbox()
        # The picture from before the camera starts to move, if someone is watching.
        frame = mailbox.peek(self.id)
        mailbox.send(self.id, {
                'op': 'move', 'dx': dx, 'dy': dy, 'speed': speed, 'seconds': PTZ_HOLD_S})
        self._note_movement()
        Pool().get('videovigilance.event').record_command(self, user, dx, dy, frame)

    def ptz_stop(self):
        from . import live
        live.mailbox().send(self.id, {'op': 'stop'})
        self._note_movement()
        Pool().get('videovigilance.event').record_stop(self)

    def _note_movement(self):
        "Event notices are muted for a while after this (see Event.notify)."
        with without_check_access():
            type(self).write([self], {'ptz_moved_at': datetime.now()})

    @staticmethod
    def default_rtsp_port():
        return 554

    @staticmethod
    def default_status():
        return 'unknown'

    @staticmethod
    def _device_values(camera, device):
        """Values to write from what the camera reports about itself over ONVIF.

        Brand and model only fill the blanks: what someone typed is never overwritten.
        Firmware, serial and hardware are the camera's own and are always kept up to date.
        """
        if not isinstance(device, dict):
            return {}
        values = {}
        for field, key, only_if_blank in DEVICE_FIELDS:
            value = device.get(key)
            value = value.strip()[:DEVICE_TEXT_MAX] if isinstance(value, str) else ''
            current = getattr(camera, field)
            if value and value != current and not (only_if_blank and current):
                values[field] = value
        return values

    @classmethod
    def apply_heartbeat(cls, reports, now):
        """Records the status reported by the service for each camera.

        reports: [{'code', 'online', 'detail', 'frame', 'device'}]; 'frame' is an
        optional JPEG (bytes), the camera's latest picture, kept as is until
        the next one arrives; 'device' is what the camera says about itself over
        ONVIF ({'manufacturer', 'model', 'firmware', 'serial', 'hardware'}).
        Returns notices for status changes: outage and recovery (no notice when
        changing from "unknown" to "online").
        """
        from .push import local_time
        notices = []
        with without_check_access():
            reports = [r for r in reports
                if isinstance(r, dict) and isinstance(r.get('code'), str)]
            cameras = {c.code: c for c in cls.search(
                    [('code', 'in', [r['code'] for r in reports])])}
            for report in reports:
                camera = cameras.get(report['code'])
                if camera is None:
                    continue
                status = 'online' if report.get('online') else 'offline'
                detail = str(report.get('detail') or '').strip()[:200]
                values = {'status_checked_at': now, 'status_detail': detail}
                frame = report.get('frame')
                if is_frame(frame):
                    values.update(frame=frame, frame_at=now)
                values.update(cls._device_values(camera, report.get('device')))
                if status != camera.status:
                    values.update(status=status, status_changed_at=now)
                    if status == 'offline':
                        notices.append({
                                'title': 'Cámara sin conexión',
                                'type': 'camera_offline', 'camera': camera.id,
                                'body': '%s · %s · desde %s' % (
                                    camera.name, detail or 'sin respuesta',
                                    local_time(now)),
                                })
                    elif camera.status == 'offline':
                        notices.append({
                                'title': 'Cámara recuperada',
                                'type': 'camera_online', 'camera': camera.id,
                                'body': '%s · conectada de nuevo · %s' % (
                                    camera.name, local_time(now)),
                                })
                cls.write([camera], values)
        return notices

    @staticmethod
    def default_rotation():
        return '0'

    @staticmethod
    def default_stream_path():
        return 'stream1'

    @staticmethod
    def default_onvif_enabled():
        return False

    @staticmethod
    def default_onvif_port():
        return 2020

    @staticmethod
    def default_ptz():
        return False

    @staticmethod
    def default_detect_persons():
        return True

    @staticmethod
    def default_detect_motion():
        return True


class OpenAlarms(Wizard):
    "Open the camera's alarms in the web app"
    __name__ = 'videovigilance.camera.open_alarms'
    start_state = 'open'
    open = StateAction('videovigilance.act_camera_alarms_url')

    def do_open(self, action):
        from .voyager import application
        camera = Pool().get('videovigilance.camera')(Transaction().context['active_id'])
        web = application()
        if web is None:
            raise UserError(gettext('videovigilance.msg_web_not_configured'))
        action['url'] = web.url('camera/%d' % camera.id)
        return action, {}
