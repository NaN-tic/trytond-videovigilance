# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool

from . import alarm, camera_alarms, device, events, home, live, login, messages


def register(module):
    Pool.register(
        login.LoginForm,
        login.Login,
        login.Logout,
        home.SessionBar,
        home.Dashboard,
        home.Index,
        home.NotificationPage,
        alarm.AlarmCard,
        alarm.AlarmSwitch,
        messages.MessageItem,
        messages.Messages,
        messages.MessageRead,
        device.DeviceCard,
        device.PushSubscribe,
        device.PushUnsubscribe,
        device.PushTest,
        live.LivePage,
        live.LiveFrame,
        live.LiveMove,
        live.LiveStop,
        events.EventsPage,
        events.DetectionSnapshot,
        events.MovementFrame,
        camera_alarms.CameraAlarmsPage,
        camera_alarms.AlarmSnapshot,
        module=module,
        type_='model'
    )
