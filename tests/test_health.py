# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""The picture kept with a camera status event, and how soon a silent service is noticed.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_health
"""
import unittest
from datetime import datetime, timedelta

from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction

FRAME = b'\xff\xd8last-frame'


class HealthTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def camera(self):
        location, = Pool().get('videovigilance.location').create([{'name': 'Casa'}])
        camera, = Pool().get('videovigilance.camera').create([{
                    'name': 'N', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
                    'stream_path': 's', 'rotation': '0', 'status': 'online',
                    'location': location.id}])
        return camera

    def beat(self, camera, **report):
        Camera = Pool().get('videovigilance.camera')
        notices = Camera.apply_heartbeat(
            [{'code': camera.code, 'detail': 'sin respuesta', **report}], datetime.now())
        return notices, Camera(camera.id)

    def events(self, type_):
        return Pool().get('videovigilance.event').search([('type', '=', type_)])

    @with_transaction()
    def test_an_outage_is_an_event_not_an_alarm_and_keeps_the_last_frame_before_it(self):
        pool = Pool()
        camera = self.camera()
        _, camera = self.beat(camera, online=True, frame=FRAME)
        notices, camera = self.beat(camera, online=False)  # no frame while it is down
        from trytond.modules.videovigilance.push import notify_users
        notify_users(notices)
        self.assertEqual(pool.get('videovigilance.alarm').search([]), [])
        event, = self.events('camera_offline')
        self.assertEqual(event.camera, camera)
        self.assertEqual(event.snapshot, FRAME)
        self.assertEqual(event.message.split(' · ')[:2], ['N', 'sin respuesta'])
        # The camera is back and sends new pictures: the event keeps the old one.
        self.beat(camera, online=True, frame=b'\xff\xd8newer')
        self.assertEqual(pool.get('videovigilance.event')(event.id).snapshot, FRAME)

    @with_transaction()
    def test_a_detection_alarm_still_shows_the_snapshot_of_its_event(self):
        camera = self.camera()
        self.beat(camera, online=True, frame=b'\xff\xd8camera')
        event, = Pool().get('videovigilance.event').create([{
                    'uid': 'u', 'camera': camera.id, 'type': 'person_motion',
                    'started_at': datetime.now(), 'ended_at': datetime.now(),
                    'snapshot': b'\xff\xd8event'}])
        alarm, = Pool().get('videovigilance.alarm').search([('event', '=', event.id)])
        self.assertEqual(alarm.snapshot, b'\xff\xd8event')

    def service(self, silent_for, status='online', **values):
        Configuration = Pool().get('videovigilance.configuration')
        return Configuration.create([{
                    'alarm_armed': True, 'service_status': status,
                    'service_last_seen': datetime.now() - timedelta(seconds=silent_for),
                    **values}])[0]

    @with_transaction()
    def test_a_service_that_misses_one_beat_is_reported_after_two_intervals_not_before(self):
        Configuration = Pool().get('videovigilance.configuration')
        # 5 s interval, 1 missed beat allowed; the watchdog has been running
        configuration = self.service(
            4, watchdog_checked_at=datetime.now() - timedelta(seconds=5))
        self.assertEqual(Configuration._check_service_once(), 5)
        self.assertEqual(self.events('service_down'), [])
        Configuration.write([configuration], {
                'service_last_seen': datetime.now() - timedelta(seconds=11)})
        Configuration._check_service_once()
        self.assertEqual(len(self.events('service_down')), 1)
        self.assertEqual(Configuration(configuration.id).service_status, 'offline')
        Configuration._check_service_once()  # still silent: no second event
        self.assertEqual(len(self.events('service_down')), 1)

    @with_transaction()
    def test_after_tryton_was_stopped_the_service_gets_time_to_come_back(self):
        Configuration = Pool().get('videovigilance.configuration')
        # Never checked before: the silence cannot be counted from before this moment.
        self.service(300)
        Configuration._check_service_once()
        self.assertEqual(self.events('service_down'), [])
