# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""The HTTP API of the detection service: a key is all it needs, and what it sends is checked.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_api
"""
import io
import json
import shutil
import tempfile
import uuid
from datetime import datetime

import trytond.config as config
from cryptography.fernet import Fernet
from trytond.modules.videovigilance import live
from trytond.modules.videovigilance.camera import MAX_HEARTBEAT_FRAME
from trytond.pool import Pool
from trytond.protocols.wrappers import HTTPStatus
from trytond.tests.test_tryton import RouteTestCase, with_transaction

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64


class ServiceAPITestCase(RouteTestCase):
    "Test the routes of the detection service"
    module = 'videovigilance'

    key = uuid.uuid4().hex
    cancelled_key = uuid.uuid4().hex
    other_application_key = uuid.uuid4().hex

    @classmethod
    def setUpClass(cls):
        cls.had_section = config.has_section('videovigilance')
        if not cls.had_section:
            config.add_section('videovigilance')
        cls.secret_key = config.get('videovigilance', 'secret_key', default='')
        config.set('videovigilance', 'secret_key', Fernet.generate_key().decode())
        cls.folder = tempfile.mkdtemp()
        live.DIR = cls.folder
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls.had_section:
            config.set('videovigilance', 'secret_key', cls.secret_key)
        else:
            config.remove_section('videovigilance')
        live.DIR = None
        shutil.rmtree(cls.folder, True)

    @classmethod
    def setUpDatabase(cls):
        pool = Pool()
        Application = pool.get('res.user.application')
        # A user without any group: the key is all the service has.
        user, = pool.get('res.user').create([{'name': 'Servei', 'login': 'servei'}])
        cls.user = user.id
        for key, application, state in [
                (cls.key, 'videovigilance', 'validated'),
                (cls.cancelled_key, 'videovigilance', 'cancelled'),
                (cls.other_application_key, 'rest', 'validated')]:
            Application(key=key, user=user.id, application=application, state=state).save()
        location, = pool.get('videovigilance.location').create([{'name': 'Casa'}])
        entrance, _ = pool.get('videovigilance.camera').create([
                {'name': 'Entrada', 'code': 'CAM1', 'host': '10.0.0.5', 'rtsp_port': 554,
                    'stream_path': 'stream1', 'rotation': '0', 'location': location.id,
                    'username': 'user', 'password': 'secreta'},
                {'name': 'Retirada', 'code': 'CAM2', 'host': '10.0.0.6', 'rtsp_port': 554,
                    'stream_path': 'stream1', 'rotation': '0', 'location': location.id,
                    'active': False}])
        cls.camera = entrance.id

    def headers(self, key=None):
        return {'Authorization': f'bearer {key or self.key}'}

    def url(self, path):
        return f'/{self.db_name}/videovigilance/{path}'

    def event(self, **values):
        data = {'camera': self.camera, 'type': 'person_detection',
            'started_at': '2026-10-27T10:00:00+00:00', 'ended_at': '2026-10-27T11:00:05+01:00',
            'max_persons': 2, 'max_confidence': 0.9, **values}
        return {'event': json.dumps(data), 'snapshot': (io.BytesIO(JPEG), 'snapshot.jpg')}

    def put_event(self, uid, data=None):
        return self.client().put(
            self.url(f'events/{uid}'), data=data or self.event(), headers=self.headers())

    @with_transaction()
    def events(self, uid_prefix):
        "(uid, type, persons, started, ended, snapshot, creator) of the stored events."
        return [(e.uid, e.type, e.max_persons, e.started_at, e.ended_at, e.snapshot_id,
                e.create_uid.id)
            for e in Pool().get('videovigilance.event').search(
                [('uid', 'like', uid_prefix + '%')])]

    # -- the key ----------------------------------------------------------------------

    def test_a_request_without_a_valid_key_is_refused(self):
        for headers in (
                {}, {'Authorization': 'bearer no-such-key'},
                self.headers(self.cancelled_key),
                self.headers(self.other_application_key),
                {'Cookie': 'tryton_session=admin:1:a-tryton-session'}):
            with self.subTest(headers=headers):
                response = self.client().get(self.url('config'), headers=headers)
                self.assertEqual(response.status_code, HTTPStatus.UNAUTHORIZED)

    def test_the_key_is_enough_for_a_user_without_groups(self):
        response = self.client().get(self.url('config'), headers=self.headers())
        self.assertEqual(response.status_code, HTTPStatus.OK)

    # -- config -----------------------------------------------------------------------

    def test_config_has_the_active_cameras_with_their_credentials(self):
        response = self.client().get(self.url('config'), headers=self.headers())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        camera, = response.get_json()['cameras']
        self.assertEqual(
            (camera['id'], camera['code'], camera['username'], camera['password'],
                camera['rotation'], camera['rtsp_port']),
            (self.camera, 'CAM1', 'user', 'secreta', 0, 554))

    def test_config_is_not_sent_again_when_the_service_has_it(self):
        first = self.client().get(self.url('config'), headers=self.headers())
        again = self.client().get(self.url('config'), headers={
                **self.headers(), 'If-None-Match': first.headers['ETag']})
        self.assertEqual(again.status_code, HTTPStatus.NOT_MODIFIED)
        self.assertEqual(again.data, b'')

    # -- events -----------------------------------------------------------------------

    def test_an_event_is_stored_once_however_many_times_it_is_sent(self):
        self.assertEqual(self.put_event('once').status_code, HTTPStatus.CREATED)
        self.assertEqual(self.put_event('once').status_code, HTTPStatus.OK)
        (uid, type_, persons, started, ended, snapshot, creator), = self.events('once')
        self.assertEqual((uid, type_, persons), ('once', 'person_detection', 2))
        # Times arrive with any offset and are stored as naive UTC.
        self.assertEqual((started, ended),
            (datetime(2026, 10, 27, 10, 0, 0), datetime(2026, 10, 27, 10, 0, 5)))
        self.assertTrue(snapshot)
        self.assertEqual(creator, self.user)

    def test_what_is_not_a_detection_of_a_camera_is_refused_and_not_stored(self):
        for case, uid, data, status in [
                ('a field it has no business with', 'bad1', self.event(foo=1), 400),
                ('a camera movement, which only people make', 'bad2',
                    self.event(type='camera_movement'), 400),
                ('a date that is not one', 'bad3', self.event(started_at='ayer'), 400),
                ('a camera that is not an id', 'bad4', self.event(camera='CAM1'), 400),
                ('a uid that is not the one of the route', 'bad5',
                    self.event(uid='other'), 400),
                ('no event part', 'bad6', {'snapshot': (io.BytesIO(JPEG), 's.jpg')}, 400),
                ('a snapshot that is not a JPEG', 'bad7',
                    {**self.event(), 'snapshot': (io.BytesIO(b'text'), 's.jpg')}, 400),
                ('a uid that is too long', 'bad' + 'x' * 70, self.event(), 400),
                ('a confidence out of range', 'bad8', self.event(max_confidence=2), 400),
                ('a camera that does not exist', 'bad9', self.event(camera=999999), 404),
                ]:
            with self.subTest(case):
                response = self.put_event(uid, data)
                self.assertEqual(response.status_code, status, response.data)
        self.assertEqual(self.events('bad'), [])

    # -- heartbeat --------------------------------------------------------------------

    def test_a_heartbeat_marks_the_service_and_the_cameras_and_answers_the_intervals(self):
        response = self.client().post(
            self.url('heartbeat'), headers=self.headers(), json={'cameras': [
                    {'code': 'CAM1', 'online': True, 'detail': 'ok'}]})
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(
            set(response.get_json()),
            {'camera_interval', 'camera_failures', 'service_interval'})

        @with_transaction()
        def check():
            pool = Pool()
            configuration = pool.get('videovigilance.configuration').get_singleton()
            camera, = pool.get('videovigilance.camera').search([('code', '=', 'CAM1')])
            self.assertEqual(configuration.service_status, 'online')
            self.assertEqual((camera.status, camera.status_detail), ('online', 'ok'))
        check()

    def test_a_heartbeat_that_is_not_a_list_of_cameras_is_refused(self):
        for body in ({'cameras': 'CAM1'}, {'other': []}, ['CAM1']):
            with self.subTest(body=body):
                response = self.client().post(
                    self.url('heartbeat'), headers=self.headers(), json=body)
                self.assertEqual(response.status_code, HTTPStatus.BAD_REQUEST)

    # -- the latest picture of a camera -----------------------------------------------

    def test_the_latest_picture_of_a_camera_is_kept(self):
        response = self.client().put(
            self.url(f'cameras/{self.camera}/frame'), data=JPEG,
            headers={**self.headers(), 'Content-Type': 'image/jpeg'})
        self.assertEqual(response.status_code, HTTPStatus.NO_CONTENT)

        @with_transaction()
        def check():
            camera = Pool().get('videovigilance.camera')(self.camera)
            self.assertEqual(bytes(camera.frame), JPEG)
            self.assertIsNotNone(camera.frame_at)
        check()

    def test_a_picture_that_is_not_one_or_is_too_big_or_has_no_camera_is_refused(self):
        def put(camera, data):
            return self.client().put(
                self.url(f'cameras/{camera}/frame'), data=data, headers=self.headers())
        self.assertEqual(put(self.camera, b'not a jpeg').status_code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(
            put(self.camera, JPEG + b'0' * (MAX_HEARTBEAT_FRAME + 2048)).status_code,
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(put(999999, JPEG).status_code, HTTPStatus.NOT_FOUND)

    # -- the live video ---------------------------------------------------------------

    def test_the_live_poll_answers_for_each_camera_and_refuses_what_is_not_a_poll(self):
        response = self.client().post(
            self.url('live/poll'), headers=self.headers(),
            json={'cameras': {str(self.camera): {'error': None, 'message': None}}})
        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.assertEqual(
            response.get_json(),
            {'cameras': {str(self.camera): {'wanted': False, 'commands': []}}})
        for body in ({'cameras': ['CAM1']}, {'other': {}}, {'cameras': {'CAM1': {}}}):
            with self.subTest(body=body):
                refused = self.client().post(
                    self.url('live/poll'), headers=self.headers(), json=body)
                self.assertEqual(refused.status_code, HTTPStatus.BAD_REQUEST)

    def test_a_live_frame_says_whether_somebody_is_still_watching(self):
        response = self.client().post(
            self.url(f'live/{self.camera}/frame'), data=JPEG, headers=self.headers())
        self.assertEqual(response.get_json(), {'wanted': False})
        refused = self.client().post(
            self.url(f'live/{self.camera}/frame'), data=b'not a jpeg', headers=self.headers())
        self.assertEqual(refused.status_code, HTTPStatus.BAD_REQUEST)


del RouteTestCase
