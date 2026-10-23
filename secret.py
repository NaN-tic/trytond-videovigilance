# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from cryptography.fernet import Fernet, InvalidToken

from trytond.config import config
from trytond.i18n import gettext
from trytond.model.exceptions import ValidationError


class SecretKeyError(ValidationError):
    pass


def _fernet():
    key = config.get('videovigilance', 'secret_key')
    if not key:
        raise SecretKeyError(gettext('videovigilance.msg_secret_key_missing'))
    try:
        return Fernet(key.encode())
    except ValueError:
        raise SecretKeyError(
            gettext('videovigilance.msg_secret_key_invalid')) from None


def encrypt(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretKeyError(
            gettext('videovigilance.msg_secret_key_mismatch')) from None
