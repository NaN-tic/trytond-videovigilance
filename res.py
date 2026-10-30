# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import hashlib

from trytond.i18n import gettext
from trytond.model import Unique, fields
from trytond.model.exceptions import ValidationError
from trytond.pool import PoolMeta
from trytond.transaction import Transaction

from . import secret

# The application of the key the detection service sends to the routes of routes.py.
SERVICE_APPLICATION = 'videovigilance'


class UserApplication(metaclass=PoolMeta):
    __name__ = 'res.user.application'

    videovigilance_location = fields.Many2One(
        'videovigilance.location', "Video Surveillance Location", ondelete='CASCADE',
        help="Raspberry Pi whose cameras this detection service key can access.")
    videovigilance_key_encrypted = fields.Char(
        "Encrypted Service Token", readonly=True, strip=False)

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.application.selection.append(
            (SERVICE_APPLICATION, "Video Surveillance: Detection Service"))
        table = cls.__table__()
        cls._sql_constraints += [
            ('videovigilance_location_unique',
                Unique(table, table.videovigilance_location),
                'videovigilance.msg_location_service_unique'),
            ]

    @classmethod
    def validate_fields(cls, applications, field_names):
        super().validate_fields(applications, field_names)
        if field_names is None or {'application', 'videovigilance_location'} & set(field_names):
            for application in applications:
                if (application.videovigilance_location
                        and application.application != SERVICE_APPLICATION):
                    raise ValidationError(gettext('videovigilance.msg_location_service_only'))

    @staticmethod
    def service_key_hash(key):
        return hashlib.sha256(key.encode()).hexdigest()

    @classmethod
    def _service_key_values(cls, key):
        return {
            'key': cls.service_key_hash(key),
            'videovigilance_key_encrypted': secret.encrypt(key),
            }

    @classmethod
    def __register__(cls, module):
        super().__register__(module)
        cls._encrypt_service_keys()

    @classmethod
    def _encrypt_service_keys(cls):
        "Migrate plaintext service keys without changing the credentials on the devices."
        table = cls.__table__()
        cursor = Transaction().connection.cursor()
        cursor.execute(*table.select(table.id, table.key,
                where=(table.application == SERVICE_APPLICATION)
                & (table.videovigilance_key_encrypted == None)))
        for record_id, key in cursor.fetchall():
            values = cls._service_key_values(key)
            cursor.execute(*table.update(
                    [table.key, table.videovigilance_key_encrypted],
                    [values['key'], values['videovigilance_key_encrypted']],
                    where=table.id == record_id))

    @classmethod
    def create(cls, vlist):
        vlist = [dict(values) for values in vlist]
        for values in vlist:
            if values.get('application') == SERVICE_APPLICATION:
                key = values['key'] if 'key' in values else cls.default_key()
                if key:
                    values.update(cls._service_key_values(key))
        return super().create(vlist)

    @classmethod
    def write(cls, *args):
        actions = iter(args)
        args = []
        for records, values in zip(actions, actions):
            if not records or not ('key' in values or 'application' in values):
                args.extend((records, values))
                continue
            for record in records:
                record_values = dict(values)
                application = values.get('application', record.application)
                if application == SERVICE_APPLICATION:
                    if 'key' in values or record.application != SERVICE_APPLICATION:
                        key = values.get('key', record.key)
                        if key:
                            record_values.update(cls._service_key_values(key))
                elif record.application == SERVICE_APPLICATION:
                    if 'key' not in values:
                        record_values['key'] = secret.decrypt(
                            record.videovigilance_key_encrypted)
                    record_values['videovigilance_key_encrypted'] = None
                args.extend(([record], record_values))
        super().write(*args)

    @classmethod
    def check(cls, key, application):
        if application == SERVICE_APPLICATION:
            if not isinstance(key, str) or not key:
                return
            key = cls.service_key_hash(key)
        return super().check(key, application)
