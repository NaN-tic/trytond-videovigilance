# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import hashlib
import hmac
import json
from datetime import timezone
from zoneinfo import ZoneInfo

from trytond.config import config
from trytond.model import fields
from trytond.pool import Pool, PoolMeta
from trytond.transaction import without_check_access

APPLICATION = 'videovigilance'


def local_time(value, fmt='%d/%m %H:%M:%S'):
    """Convert a naive UTC Tryton datetime to the configured local timezone.

    [videovigilance] timezone in trytond.conf; defaults to Europe/Madrid.
    """
    zone = ZoneInfo(config.get('videovigilance', 'timezone', default='Europe/Madrid'))
    return value.replace(tzinfo=timezone.utc).astimezone(zone).strftime(fmt)


def snapshot_token(message_id):
    """Sign the URL for a notice snapshot (derived from the secret key)."""
    key = config.get('videovigilance', 'secret_key')
    return hmac.new(key.encode(), f'snapshot:{message_id}'.encode(),
        hashlib.sha256).hexdigest()[:40]


def _group_ids(*names):
    ModelData = Pool().get('ir.model.data')
    return {ModelData.get_id('videovigilance', name) for name in names}


def is_web_account(user):
    "Whether `user` is a web.user (a web account that logs in with a password), not a Tryton user."
    return user.__name__ == 'web.user'


def has_access(user):
    """Whether the Tryton user can access the web app (admin or reader)."""
    with without_check_access():
        return bool(user.active and _group_ids('group_admin', 'group_reader')
            & {group.id for group in user.groups})


def is_admin(user):
    # A web account only reads: administering (the camera, the alarm) needs a Tryton session
    # of a user in the administration group.
    if is_web_account(user):
        return False
    with without_check_access():
        return bool(user.active and _group_ids('group_admin')
            & {group.id for group in user.groups})


def display_name(user):
    return user.email if is_web_account(user) else (user.name or user.login)


def authenticate_web_user(email, password):
    """The web user with this email and password, or None. May raise RateLimitException.

    web.user.authenticate fails with a TypeError on the accounts that have no password (the
    ones created only to receive notices) once it has applied its limits and delay: that is
    one more failed attempt.
    """
    pool = Pool()
    email = (email or '').strip().lower()
    with without_check_access():
        try:
            return pool.get('web.user').authenticate(email, password or '')
        except TypeError:
            pool.get('web.user.authenticate.attempt').add(email)


def web_user_for(user):
    """Web user (notice recipient) linked to a Tryton user.

    Created on first use from the Tryton user's email address. It has no
    password and exists only to receive notices.
    """
    if is_web_account(user):
        return user
    pool = Pool()
    User = pool.get('res.user')
    WebUser = pool.get('web.user')
    with without_check_access():
        if user.web_user:
            return user.web_user
        email = user.email.strip().lower()
        found = WebUser.search([('email', '=', email)], limit=1)
        web_user = found[0] if found else WebUser.create([{'email': email}])[0]
        User.write([user], {'web_user': web_user.id})
        return web_user


def notify_users(notices):
    """Publish notices to all subscribed devices and queue their delivery.

    notices: a list of {'type', 'title', 'body'} and optionally 'camera' and
    'event' (IDs of the camera and of the originating event). Each one is recorded as
    an alarm, even if no device is subscribed. Returns the number of queued deliveries.
    """
    pool = Pool()
    Application = pool.get('notification.web.application')
    Subscription = pool.get('notification.web.subscription')
    Message = pool.get('notification.web.message')
    Delivery = pool.get('notification.web.delivery')
    pool.get('videovigilance.alarm').raise_notices(notices)
    with without_check_access():
        applications = Application.search([('code', '=', APPLICATION)], limit=1)
        if not applications or not applications[0].push_enabled:
            return 0
        application = applications[0]
        users = {s.user for s in Subscription.search([
                    ('application', '=', application.id)])}
        deliveries = []
        for notice in notices:
            extra = {'event': notice['event']} if notice.get('event') else {}
            for user in users:
                message = Message.publish(
                    application, user, title=notice['title'],
                    body=notice['body'], **extra)
                if message:
                    deliveries += Delivery.search([('message', '=', message.id)])
        if deliveries:
            # Sent when the transaction is committed, without waiting for the cron job.
            Delivery.__queue__.send(deliveries)
        return len(deliveries)


class User(metaclass=PoolMeta):
    __name__ = 'res.user'

    web_user = fields.Many2One(
        'web.user', "Web User", readonly=True, ondelete='SET NULL',
        help="Recipient of this user's video surveillance notices.")


class Application(metaclass=PoolMeta):
    __name__ = 'notification.web.application'

    def eligible_user(self, user):
        eligible = super().eligible_user(user)
        if self.code != APPLICATION:
            return eligible
        # A notice reaches a Tryton user while they still have access, and a web account of
        # its own (not linked to a Tryton user) while it can still log in with a password.
        with without_check_access():
            owners = Pool().get('res.user').search([('web_user', '=', user.id)])
            has_password = bool(user.password_hash)
        if owners:
            return bool(eligible and any(has_access(owner) for owner in owners))
        return bool(eligible and has_password)


class Message(metaclass=PoolMeta):
    __name__ = 'notification.web.message'

    event = fields.Many2One(
        'videovigilance.event', "Event", readonly=True,
        ondelete='SET NULL')

    def snapshot_path(self):
        "Signed snapshot path relative to the application, or None if unavailable."
        if self.event is None or not self.event.snapshot_id:
            return None
        return f'snapshots/{self.id}/{snapshot_token(self.id)}.jpg'


class Delivery(metaclass=PoolMeta):
    __name__ = 'notification.web.delivery'

    @classmethod
    def send(cls, deliveries):
        # notification_web_push builds the push JSON inside send() with no
        # extension point, so the image is added by intercepting pywebpush.webpush
        # only for the duration of this call.
        import pywebpush
        images = {}
        for delivery in deliveries:
            message = delivery.message
            if message.application.code != APPLICATION:
                continue
            path = message.snapshot_path()
            if path:
                images[message.id] = message.application.url(path)
        if not images:
            return super().send(deliveries)
        original = pywebpush.webpush

        def webpush(*args, **kwargs):
            try:
                data = json.loads(kwargs['data'])
                message_id = int(data['tag'].rsplit('-', 1)[1])
                if message_id in images:
                    data['image'] = images[message_id]
                    kwargs['data'] = json.dumps(data)
            except (KeyError, ValueError, TypeError):
                pass
            return original(*args, **kwargs)
        pywebpush.webpush = webpush
        try:
            super().send(deliveries)
        finally:
            pywebpush.webpush = original
