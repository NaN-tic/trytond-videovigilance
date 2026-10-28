# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Camera, event, alarm, and surveillance notification web registry."""

from trytond.pool import Pool

from . import alarm, call, camera, configuration, event, movement, push, res, routes, voyager, www

__all__ = ['register', 'routes']


def register():
    Pool.register(
        camera.Location,
        camera.Camera,
        event.Event,
        movement.CameraMovement,
        alarm.Alarm,
        call.LocationManager,
        call.Call,
        call.YeastarPBX,
        configuration.Configuration,
        configuration.Cron,
        push.User,
        push.Application,
        push.Message,
        push.Delivery,
        res.UserApplication,
        voyager.Site,
        voyager.Layout,
        voyager.Error,
        module='videovigilance', type_='model')
    Pool.register(
        camera.OpenAlarms,
        module='videovigilance', type_='wizard')
    Pool.register(
        module='videovigilance', type_='report')

    www.register('videovigilance')
