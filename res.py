# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import PoolMeta

# The application of the key the detection service sends to the routes of routes.py.
SERVICE_APPLICATION = 'videovigilance'


class UserApplication(metaclass=PoolMeta):
    __name__ = 'res.user.application'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.application.selection.append(
            (SERVICE_APPLICATION, "Video Surveillance: Detection Service"))
