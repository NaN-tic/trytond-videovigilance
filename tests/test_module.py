# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import trytond.config as config
from cryptography.fernet import Fernet
from dominate.tags import div
from trytond.exceptions import UserError
from trytond.model.exceptions import (
    AccessButtonError, AccessError, RequiredValidationError, ValidationError)
from trytond.modules.company.tests import create_company, set_company
from trytond.modules.videovigilance import live
from trytond.modules.videovigilance.camera import MAX_HEARTBEAT_FRAME
from trytond.modules.videovigilance.push import (
    authenticate_web_user, display_name, is_admin)
from trytond.modules.videovigilance.voyager import (
    SITE_TYPE, access_problem, csrf, csrf_ok, current_user, rotate_session, tryton_user)
from trytond.modules.voyager.voyager import VoyagerContext
from trytond.pool import Pool
from trytond.protocols.wrappers import HTTPStatus
from trytond.tests.test_tryton import (
    ModuleTestCase, RouteTestCase, activate_module, drop_db, with_transaction)
from trytond.transaction import Transaction
from werkzeug.datastructures import MultiDict


class Pbx(BaseHTTPRequestHandler):
    "The calls of the Yeastar API that the module uses."
    dials = []
    plays = []
    uploads = []
    tokens = 0
    fail_dial = fail_upload = fail_play = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        cls = type(self)
        url = urlsplit(self.path)
        raw = self.rfile.read(int(self.headers['Content-Length']))
        assert parse_qs(url.query).get('access_token', ['TOKEN']) == ['TOKEN'], url.query
        if url.path == '/openapi/v1.0/custom_prompt/upload':
            assert self.headers['Content-Type'].startswith('multipart/form-data'), self.headers
            cls.uploads.append(raw)
            reply = ({'errcode': 20004, 'errmsg': 'FAILURE'} if cls.fail_upload
                else {'errcode': 0, 'errmsg': 'SUCCESS', 'exist_type': ''})
            return self.answer(reply)
        body = json.loads(raw or b'{}')
        if url.path == '/openapi/v1.0/get_token':
            cls.tokens += 1
            assert body == {'username': 'client', 'password': 'secret'}, body
            reply = {'errcode': 0, 'errmsg': 'SUCCESS', 'access_token': 'TOKEN',
                'access_token_expire_time': 1800, 'refresh_token': 'REFRESH',
                'refresh_token_expire_time': 86400}
        elif url.path == '/openapi/v1.0/call/dial':
            cls.dials.append(body)
            if cls.fail_dial:
                reply = {'errcode': 10004, 'errmsg': 'FAILURE'}
            else:
                reply = {'errcode': 0, 'errmsg': 'SUCCESS', 'call_id': '1650.%d' % len(cls.dials)}
        elif url.path == '/openapi/v1.0/call/play_prompt':
            cls.plays.append(body)
            if cls.fail_play:
                reply = {'errcode': 10005, 'errmsg': 'FAILURE'}
            else:
                reply = {'errcode': 0, 'errmsg': 'SUCCESS', 'call_id': '1720.%d' % len(cls.plays)}
        else:
            self.send_error(404)
            return
        self.answer(reply)

    def answer(self, reply):
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CallTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        drop_db()
        activate_module('videovigilance', lang='es')
        cls.server = HTTPServer(('127.0.0.1', 0), Pbx)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def setUp(self):
        Pbx.dials, Pbx.plays, Pbx.uploads, Pbx.tokens = [], [], [], 0
        Pbx.fail_dial = Pbx.fail_upload = Pbx.fail_play = False
        # The module commits (the PBX token, each result); the test must stay isolated.
        patcher = patch.object(Transaction, 'commit')
        patcher.start()
        self.addCleanup(patcher.stop)

    def scenario(self, numbers=('+34 600 111 222',), cooldown=300, voice=False):
        pool = Pool()
        company = create_company()
        with set_company(company):
            pbx, = pool.get('yeastar.pbx').create([{
                        'name': 'PBX', 'company': company.id,
                        'base_url': 'http://127.0.0.1:%d' % self.server.server_port,
                        'api_path': 'openapi/v1.0', 'username': 'client',
                        'password': 'secret', 'time_format': '%Y/%m/%d %H:%M:%S',
                        'endpoints': [('create', [
                                    {'action': 'get_token', 'endpoint': 'get_token'},
                                    {'action': 'refresh_token', 'endpoint': 'refresh_token'},
                                    {'action': 'revoke_token', 'endpoint': 'revoke_token'},
                                    {'action': 'dial', 'endpoint': 'call/dial'},
                                    ])],
                        }])
        Configuration = pool.get('videovigilance.configuration')
        Configuration.create([{
                    'alarm_armed': True, 'yeastar_pbx': pbx.id, 'yeastar_caller': '700',
                    'call_cooldown': cooldown, 'call_voice': voice,
                    'call_voice_name': 'es-ES-Standard-A',
                    'yeastar_dial_permission': '701' if voice else None}])
        managers = []
        for index, number in enumerate(numbers):
            party, = pool.get('party.party').create([{
                        'name': 'Encarregat %d' % index,
                        'contact_mechanisms': [('create', [{'type': 'mobile', 'value': number}])],
                        }])
            managers.append({'party': party.id, 'phone': party.contact_mechanisms[0].id,
                    'sequence': index})
        location, = pool.get('videovigilance.location').create([{
                    'name': 'Casa', 'managers': [('create', managers)]}])
        return location, self.camera(location, 'CAM1')

    def camera(self, location, code):
        camera, = Pool().get('videovigilance.camera').create([{
                    'name': code, 'code': code, 'host': 'h', 'location': location.id,
                    'rtsp_port': 554, 'stream_path': 's', 'rotation': '0'}])
        return camera

    def event(self, camera, kind='person_motion', uid='u'):
        event, = Pool().get('videovigilance.event').create([{
                    'uid': uid, 'camera': camera.id, 'type': kind,
                    'started_at': datetime.now(), 'ended_at': datetime.now(),
                    'max_persons': 1, 'max_confidence': 0.9}])
        return event

    def run_queue(self):
        "What trytond does once the request has been committed."
        transaction = Transaction()
        while transaction.tasks:
            Pool().get('ir.queue')(transaction.tasks.pop()).run()

    def calls(self):
        return Pool().get('videovigilance.alarm.call').search([], order=[('id', 'ASC')])

    @with_transaction()
    def test_every_manager_is_called_from_the_configured_number(self):
        _, camera = self.scenario(('+34 600 111 222', '+34 972 000 000'))
        self.event(camera)
        self.assertEqual(Pbx.dials, [])  # nothing is dialled before the commit
        self.run_queue()
        self.assertEqual(Pbx.dials, [
                {'caller': '700', 'callee': '0034600111222'},
                {'caller': '700', 'callee': '0034972000000'}])
        self.assertEqual(Pbx.tokens, 1)
        calls = self.calls()
        self.assertEqual([c.state for c in calls], ['placed', 'placed'])
        self.assertEqual([c.call_id for c in calls], ['1650.1', '1650.2'])

    @with_transaction()
    def test_a_person_who_is_detected_and_moving_makes_one_round_of_calls(self):
        _, camera = self.scenario()
        self.event(camera, 'person_detection', 'a')
        self.event(camera, 'person_motion', 'b')
        self.run_queue()
        self.assertEqual(len(Pbx.dials), 1)

    @with_transaction()
    def test_failed_calls_do_not_block_the_next_alarm_but_placed_ones_do(self):
        _, camera = self.scenario()
        Pbx.fail_dial = True
        self.event(camera, uid='a')
        self.run_queue()
        call, = self.calls()
        self.assertEqual(call.state, 'failed')
        self.assertIn('FAILURE', call.error)
        Pbx.fail_dial = False
        self.event(camera, uid='b')
        self.run_queue()
        self.assertEqual([c.state for c in self.calls()], ['failed', 'placed'])
        self.event(camera, uid='c')
        self.run_queue()
        self.assertEqual(len(self.calls()), 2)

    @with_transaction()
    def test_no_calls_when_the_location_has_no_managers(self):
        location, camera = self.scenario()
        Pool().get('videovigilance.location.manager').delete(location.managers)
        self.event(camera, uid='a')
        self.run_queue()
        self.assertEqual(Pbx.dials, [])
        self.assertEqual(self.calls(), [])

    @with_transaction()
    def test_cooldown_zero_calls_on_every_alarm(self):
        _, camera = self.scenario(cooldown=0)
        self.event(camera, uid='a')
        self.run_queue()
        self.event(camera, uid='b')
        self.run_queue()
        self.assertEqual(len(Pbx.dials), 2)

    @with_transaction()
    def test_health_notices_never_call_and_are_events_not_alarms(self):
        from trytond.modules.videovigilance.push import notify_users
        pool = Pool()
        _, camera = self.scenario()
        notify_users([{
                    'type': 'camera_offline', 'camera': camera.id,
                    'title': 'x', 'body': 'y'}])
        self.run_queue()
        self.assertEqual(Pbx.dials, [])
        self.assertEqual(pool.get('videovigilance.alarm').search([]), [])
        self.assertEqual(
            [e.type for e in pool.get('videovigilance.event').search([])],
            ['camera_offline'])

    @with_transaction()
    def test_a_number_that_cannot_be_dialled_is_refused_when_saved(self):
        from trytond.model.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.scenario(('ext. 12',))

    def voice_scenario(self):
        location, camera = self.scenario(('+34 600 111 222', '+34 972 000 000'), voice=True)
        self.event(camera)
        return location

    @with_transaction()
    def test_with_voice_the_pbx_reads_the_alarm_instead_of_just_ringing(self):
        with Transaction().set_context(language='es'), \
                patch('trytond.modules.videovigilance.call.synthesize',
                    return_value=b'RIFFfake-wav') as synthesize:
            location = self.voice_scenario()
            self.run_queue()
        text, voice = synthesize.call_args.args
        self.assertEqual(voice, 'es-ES-Standard-A')
        for part in ('Movimiento de persona', 'CAM1', 'Casa', '1 persona.'):
            self.assertIn(part, text)
        self.assertEqual(synthesize.call_count, 1)  # one audio for the whole round
        upload, = Pbx.uploads
        self.assertIn(b'filename="videovigilancia_%d.wav"' % location.id, upload)
        self.assertIn(b'RIFFfake-wav', upload)
        self.assertIn(b'force', upload)
        name = 'videovigilancia_%d' % location.id
        self.assertEqual(Pbx.plays, [
                {'number': '0034600111222', 'prompts': [name], 'count': 2,
                    'dial_permission': '701'},
                {'number': '0034972000000', 'prompts': [name], 'count': 2,
                    'dial_permission': '701'}])
        self.assertEqual(Pbx.dials, [])
        calls = self.calls()
        self.assertEqual([(c.state, c.voice, c.voice_error) for c in calls],
            [('placed', True, None)] * 2)
        self.assertEqual([c.call_id for c in calls], ['1720.1', '1720.2'])

    @with_transaction()
    def test_if_the_voice_cannot_be_generated_the_managers_are_called_anyway(self):
        with patch('trytond.modules.videovigilance.call.synthesize',
                side_effect=RuntimeError('sin credenciales de Google')):
            self.voice_scenario()
            self.run_queue()
        self.assertEqual(Pbx.uploads, [])
        self.assertEqual(len(Pbx.dials), 2)
        self.assertEqual(Pbx.plays, [])
        for call in self.calls():
            self.assertEqual((call.state, call.voice), ('placed', False))
            self.assertIn('sin credenciales de Google', call.voice_error)

    @with_transaction()
    def test_if_the_audio_cannot_be_uploaded_the_managers_are_called_anyway(self):
        Pbx.fail_upload = True
        with patch('trytond.modules.videovigilance.call.synthesize', return_value=b'RIFF'):
            self.voice_scenario()
            self.run_queue()
        self.assertEqual(Pbx.plays, [])
        self.assertEqual(len(Pbx.dials), 2)
        for call in self.calls():
            self.assertEqual((call.state, call.voice), ('placed', False))
            self.assertIn('FAILURE', call.voice_error)

    @with_transaction()
    def test_if_the_pbx_cannot_play_it_that_manager_is_rung_instead(self):
        Pbx.fail_play = True
        with patch('trytond.modules.videovigilance.call.synthesize', return_value=b'RIFF'):
            self.voice_scenario()
            self.run_queue()
        self.assertEqual(len(Pbx.plays), 2)
        self.assertEqual(len(Pbx.dials), 2)
        for call in self.calls():
            self.assertEqual((call.state, call.voice), ('placed', False))
            self.assertIn('FAILURE', call.voice_error)

    @with_transaction()
    def test_voice_is_off_unless_configured(self):
        _, camera = self.scenario()
        with patch('trytond.modules.videovigilance.call.synthesize') as synthesize:
            self.event(camera)
            self.run_queue()
        synthesize.assert_not_called()
        self.assertEqual(Pbx.uploads, [])
        self.assertEqual(len(Pbx.dials), 1)


CAMERA = {'name': 'N', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
    'stream_path': 's', 'rotation': '0'}


class CameraTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.had_section = config.has_section('videovigilance')
        if not cls.had_section:
            config.add_section('videovigilance')
        cls.secret_key = config.get('videovigilance', 'secret_key', default='')
        if cls.had_section:
            cls.addClassCleanup(config.set, 'videovigilance', 'secret_key', cls.secret_key)
        else:
            cls.addClassCleanup(config.remove_section, 'videovigilance')
        cls.fernet_key = Fernet.generate_key()
        config.set('videovigilance', 'secret_key', cls.fernet_key.decode())
        activate_module('videovigilance')

    @with_transaction()
    def test_a_camera_cannot_be_saved_without_a_location(self):
        with self.assertRaises(RequiredValidationError):
            Pool().get('videovigilance.camera').create([dict(CAMERA)])

    @with_transaction()
    def test_a_camera_cannot_lose_its_location(self):
        pool = Pool()
        location, = pool.get('videovigilance.location').create([{'name': 'Casa'}])
        camera, = pool.get('videovigilance.camera').create([{**CAMERA, 'location': location.id}])
        with self.assertRaises(RequiredValidationError):
            pool.get('videovigilance.camera').write([camera], {'location': None})

    @with_transaction()
    def test_service_token_rotation_is_location_specific(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        locations = Location.create([{'name': 'One'}, {'name': 'Two'}])
        first, second = locations
        creator = Transaction().user
        Location.generate_service_token(locations)
        first_token, second_token = [
            row['service_token'] for row in Location.read(
                [first.id, second.id], ['service_token'])]
        self.assertTrue(first_token)
        self.assertTrue(second_token)
        old_first_application = Application.check(first_token, 'videovigilance')
        self.assertIsNotNone(old_first_application)
        self.assertEqual(old_first_application.user.id, creator)
        self.assertIsNotNone(Application.check(second_token, 'videovigilance'))
        self.assertNotEqual(old_first_application.key, first_token)
        self.assertEqual(
            Fernet(self.fernet_key).decrypt(
                old_first_application.videovigilance_key_encrypted.encode()).decode(),
            first_token)
        self.assertIsNone(Application.check(old_first_application.key, 'videovigilance'))
        self.assertIsNone(Application.check(
            old_first_application.videovigilance_key_encrypted, 'videovigilance'))

        Location.generate_service_token([first])
        rotated_first_token, = [
            row['service_token'] for row in Location.read([first.id], ['service_token'])]
        self.assertTrue(rotated_first_token)
        self.assertNotEqual(rotated_first_token, first_token)
        self.assertIsNone(Application.check(first_token, 'videovigilance'))
        self.assertIsNotNone(Application.check(second_token, 'videovigilance'))
        self.assertEqual(
            Application.check(rotated_first_token, 'videovigilance').id,
            old_first_application.id)
        rotated_application = Application.check(rotated_first_token, 'videovigilance')
        self.assertEqual(
            Fernet(self.fernet_key).decrypt(
                rotated_application.videovigilance_key_encrypted.encode()).decode(),
            rotated_first_token)

    @with_transaction()
    def test_revoking_a_service_token_cancels_its_application(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        location, = Location.create([{'name': 'One'}])
        Location.generate_service_token([location])
        token, = [row['service_token'] for row in Location.read(
            [location.id], ['service_token'])]
        self.assertIsNotNone(Application.check(token, 'videovigilance'))

        Location.revoke_service_token([location])
        self.assertIsNone(Location.read([location.id], ['service_token'])[0]['service_token'])
        self.assertIsNone(Application.check(token, 'videovigilance'))

    @with_transaction()
    def test_reader_cannot_read_or_manage_service_tokens_but_admin_can(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        reader, = User.create([{
            'name': 'Reader', 'login': 'reader',
            'groups': [('add', [ModelData.get_id('videovigilance', 'group_reader')])]}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id('videovigilance', 'group_admin')])]})
        with Transaction().set_user(reader.id), Transaction().set_context(_check_access=True):
            with self.assertRaises(AccessError):
                Location.read([location.id], ['service_token'])
            with self.assertRaises(AccessButtonError):
                Location.generate_service_token([location])
            with self.assertRaises(AccessButtonError):
                Location.revoke_service_token([location])
        with Transaction().set_user(admin), Transaction().set_context(_check_access=True):
            Location.generate_service_token([location])
            self.assertTrue(Location.read([location.id], ['service_token'])[0]['service_token'])
            Location.revoke_service_token([location])

    @with_transaction()
    def test_only_an_admin_can_associate_a_service_application_with_a_location(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        reader, = User.create([{
                'name': 'Reader', 'login': 'reader-application',
                'groups': [('add', [ModelData.get_id(
                    'videovigilance', 'group_reader')])]}])
        rest_application, = Application.create([{
                'user': reader.id, 'application': 'rest', 'key': 'reader-rest-key'}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id(
                    'videovigilance', 'group_admin')])]})

        with Transaction().set_user(reader.id), Transaction().set_context(
                _check_access=True):
            with self.assertRaises(AccessError):
                Application.create([{
                        'user': reader.id, 'application': 'videovigilance',
                        'key': 'reader-service-key',
                        'videovigilance_location': location.id}])
            with self.assertRaises(AccessError):
                Application.write(
                    [rest_application], {'videovigilance_location': location.id})
            self.assertEqual(
                Application.check('reader-rest-key', 'rest').id,
                rest_application.id)

        with Transaction().set_user(admin), Transaction().set_context(
                _check_access=True):
            Location.generate_service_token([location])
            token = Location.read([location.id], ['service_token'])[0]['service_token']
            service_application = Application.check(token, 'videovigilance')
            self.assertEqual(
                service_application.videovigilance_location.id, location.id)
            self.assertEqual(
                Application.check('reader-rest-key', 'rest').id,
                rest_application.id)

    @with_transaction()
    def test_a_rest_application_cannot_be_assigned_to_a_location(self):
        pool = Pool()
        location, = pool.get('videovigilance.location').create([{'name': 'One'}])
        Application = pool.get('res.user.application')
        application, = Application.create([{
                'user': Transaction().user, 'application': 'rest', 'key': 'rest-key'}])
        with self.assertRaises(ValidationError):
            Application.write([application], {'videovigilance_location': location.id})

    @with_transaction()
    def test_show_token_dialog_is_restricted_and_never_saves_its_secret(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Wizard = pool.get('videovigilance.location.show_service_token', type='wizard')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        Location.generate_service_token([location])
        token = Location.read([location.id], ['service_token'])[0]['service_token']
        reader, = User.create([{
                    'name': 'Reader', 'login': 'reader-token',
                    'groups': [('add', [ModelData.get_id('videovigilance', 'group_reader')])]}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id('videovigilance', 'group_admin')])]})
        context = {'active_model': Location.__name__, 'active_id': location.id,
            'active_ids': [location.id], '_check_access': True}
        with Transaction().set_user(reader.id), Transaction().set_context(context):
            with self.assertRaises(AccessError):
                Wizard.create()
        with Transaction().set_user(admin), Transaction().set_context(context):
            session_id, _, _ = Wizard.create()
            view = Wizard.execute(session_id, {}, 'start')['view']
            self.assertEqual(view['defaults']['token'], token)
            Wizard.execute(session_id, {'start': {'token': token}}, 'end')
            session = pool.get('ir.session.wizard')(session_id)
            self.assertNotIn(token, session.data)
            Wizard.delete(session_id)

    @with_transaction()
    def test_plaintext_service_keys_are_migrated_without_changing_device_credentials(self):
        Application = Pool().get('res.user.application')
        token = 'existing-device-credential'
        application, = Application.create([{
                    'user': Transaction().user, 'application': 'videovigilance', 'key': token}])
        table = Application.__table__()
        cursor = Transaction().connection.cursor()
        # Simulate the row left by the version which stored the key in plaintext.
        cursor.execute(*table.update(
                [table.key, table.videovigilance_key_encrypted], [token, None],
                where=table.id == application.id))
        Application._encrypt_service_keys()
        cursor.execute(*table.select(table.key, table.videovigilance_key_encrypted,
                where=table.id == application.id))
        fingerprint, encrypted = cursor.fetchone()
        self.assertNotEqual(fingerprint, token)
        self.assertEqual(Fernet(self.fernet_key).decrypt(encrypted.encode()).decode(), token)
        self.assertEqual(Application.check(token, 'videovigilance').id, application.id)
        Application._encrypt_service_keys()
        cursor.execute(*table.select(table.videovigilance_key_encrypted,
                where=table.id == application.id))
        self.assertEqual(cursor.fetchone()[0], encrypted)

    @with_transaction()
    def test_other_application_keys_keep_their_normal_authentication(self):
        Application = Pool().get('res.user.application')
        application, = Application.create([{
                    'user': Transaction().user, 'application': 'rest', 'key': 'rest-key'}])
        self.assertEqual(application.key, 'rest-key')
        self.assertIsNone(application.videovigilance_key_encrypted)
        self.assertEqual(Application.check('rest-key', 'rest').id, application.id)
        Application.write([application], {'key': 'replacement-rest-key'})
        self.assertIsNone(Application.check('rest-key', 'rest'))
        self.assertEqual(Application.check('replacement-rest-key', 'rest').id, application.id)


DEVICE = {'manufacturer': 'tp-link', 'model': 'Tapo TC43', 'firmware': '1.4.1',
    'serial': '746163c2', 'hardware': '2.0'}


class DeviceInfoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def camera(self, **values):
        location, = Pool().get('videovigilance.location').create([{'name': 'Casa'}])
        camera, = Pool().get('videovigilance.camera').create([{
                    'name': 'N', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
                    'stream_path': 's', 'rotation': '0', 'location': location.id, **values}])
        return camera

    def beat(self, camera, device):
        Camera = Pool().get('videovigilance.camera')
        Camera.apply_heartbeat(
            [{'code': camera.code, 'online': True, 'detail': '', 'device': device}],
            datetime.now())
        return Camera(camera.id)

    @with_transaction()
    def test_a_blank_camera_gets_brand_model_and_the_cameras_own_data(self):
        camera = self.beat(self.camera(), DEVICE)
        self.assertEqual(
            (camera.manufacturer, camera.device_model, camera.firmware_version,
                camera.serial_number, camera.hardware_id),
            ('tp-link', 'Tapo TC43', '1.4.1', '746163c2', '2.0'))

    @with_transaction()
    def test_what_someone_typed_is_never_overwritten_but_firmware_follows_the_camera(self):
        camera = self.camera(manufacturer='TP-Link Tapo', device_model='C530WS',
            firmware_version='1.0.0')
        camera = self.beat(camera, DEVICE)
        self.assertEqual((camera.manufacturer, camera.device_model),
            ('TP-Link Tapo', 'C530WS'))
        self.assertEqual(camera.firmware_version, '1.4.1')

    @with_transaction()
    def test_a_heartbeat_without_or_with_garbage_device_data_changes_nothing(self):
        camera = self.camera(manufacturer='Acme')
        for device in (None, 'x', [], {'model': 5, 'firmware': None, 'serial': '  '}):
            camera = self.beat(camera, device)
        self.assertEqual((camera.manufacturer, camera.device_model, camera.firmware_version),
            ('Acme', None, None))

    @with_transaction()
    def test_long_values_are_cut(self):
        camera = self.beat(self.camera(), {'model': 'M' * 500})
        self.assertEqual(len(camera.device_model), 100)


class EventsPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        drop_db()
        activate_module('videovigilance', lang='es')

    def camera(self):
        pool = Pool()
        location, = pool.get('videovigilance.location').create([{'name': 'Casa'}])
        camera, = pool.get('videovigilance.camera').create([{
                    'name': 'Entrada', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
                    'stream_path': 's', 'rotation': '0', 'location': location.id}])
        return camera

    def event(self, uid, type_, camera=None, **values):
        return Pool().get('videovigilance.event').create([{
                    'uid': uid, 'type': type_, 'camera': camera.id if camera else None,
                    'started_at': datetime(2026, 10, 30, 9), **values}])[0]

    def render(self, query=(), language=None):
        request = SimpleNamespace(args=MultiDict(query), user_id=None)
        context = VoyagerContext(request=request,
            session=SimpleNamespace(session_id='events-page-test'))
        transaction_context = {'voyager_context': context}
        if language:
            transaction_context['language'] = language
        prefix = 'trytond.modules.videovigilance.www.events.'
        with Transaction().set_context(**transaction_context):
            with patch(prefix + 'current_user', return_value=object()), \
                    patch(prefix + 'component', side_effect=lambda *args: div()), \
                    patch(prefix + 'nav', side_effect=lambda *args: div()), \
                    patch(prefix + 'page', side_effect=lambda content, **values: content), \
                    patch(prefix + 'url', side_effect=lambda name, **values: '/' + name):
                return Pool().get('videovigilance.web.events')().render().render()

    @with_transaction()
    def test_the_list_has_movements_and_status_changes_but_no_people(self):
        camera = self.camera()
        self.event('p1', 'person_motion', camera, max_persons=3)
        self.event('p2', 'person_detection', camera, max_persons=4)
        self.event('m1', 'camera_movement', camera, directions='←')
        self.event('s1', 'camera_offline', camera, message='Entrada · sin respuesta')
        self.event('s2', 'service_down', message='Sin latidos')
        html = self.render()
        self.assertEqual(html.count('<li class="py-3">'), 3)

    @with_transaction()
    def test_filters_split_movements_from_status_and_a_person_filter_does_not_exist(self):
        camera = self.camera()
        self.event('m1', 'camera_movement', camera)
        self.event('s1', 'camera_online', camera, message='conectada de nuevo')
        movements = self.render([('type', 'movements')])
        self.assertEqual(movements.count('<li class="py-3">'), 1)
        status = self.render([('type', 'status')])
        self.assertEqual(status.count('<li class="py-3">'), 1)
        self.assertEqual(
            self.render([('type', 'persons')]).count('<li class="py-3">'), 2)
        self.assertEqual(self.render().count('<li class="py-3">'), 2)
        self.assertEqual(self.render([('type', 'persons')]), self.render())

    @with_transaction()
    def test_page_text_uses_the_selected_language(self):
        self.assertIn('Eventos', self.render(language='es'))

    @with_transaction()
    def test_translations_survive_source_extraction(self):
        from trytond.modules.xgettext.i18n import TranslationSet

        camera = self.camera()
        self.event('offline', 'camera_offline', camera)
        TranslationSet.set_xgettext(None)
        html = self.render(language='es')
        self.assertIn('Cámara movida', html)
        self.assertIn('Cámara desconectada', html)

    @with_transaction()
    def test_only_service_status_events_may_have_no_camera(self):
        camera = self.camera()
        self.event('ok', 'service_up')
        for type_ in ('camera_offline', 'camera_online', 'camera_movement', 'person_motion'):
            with self.subTest(type=type_), self.assertRaises(RequiredValidationError):
                self.event('no-camera-' + type_, type_)
        self.event('with-camera', 'camera_offline', camera)


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


HERE = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f'vv_{name}', HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mailbox_live = load('live')
MAILBOX_JPEG = b'\xff\xd8\xff\xe0fake jpeg'


class MailboxTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(setattr, mailbox_live, 'DIR', mailbox_live.DIR)
        mailbox_live.DIR = self.folder
        self.box = mailbox_live.Mailbox('cameras')

    def age(self, path_part, seconds):
        "Make the files of the mailbox that contain `path_part` look `seconds` old."
        for name in os.listdir(self.folder):
            if path_part in name:
                moment = time.time() - seconds
                os.utime(Path(self.folder) / name, (moment, moment))


class FrameTest(MailboxTestCase):
    def test_every_frame_has_a_higher_sequence_than_the_last(self):
        first = self.box.put_frame(1, MAILBOX_JPEG)
        second = self.box.put_frame(1, MAILBOX_JPEG)
        third = self.box.put_frame(1, MAILBOX_JPEG)
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertEqual(self.box.latest(1), (third, MAILBOX_JPEG))

    def test_a_service_restart_does_not_make_sequences_go_back(self):
        before = self.box.put_frame(1, MAILBOX_JPEG)
        # a new mailbox object, as in another worker or after Tryton restarts
        after = mailbox_live.Mailbox('cameras').put_frame(1, MAILBOX_JPEG)
        self.assertGreater(after, before)

    def test_an_old_frame_is_not_live_and_is_not_served(self):
        self.box.put_frame(1, MAILBOX_JPEG)
        self.age('.frame', mailbox_live.FRESH_S + 1)
        self.assertEqual(self.box.latest(1), (0, None))
        self.assertIsNone(self.box.peek(1))

    def test_peek_only_returns_a_picture_taken_a_moment_ago(self):
        self.box.put_frame(1, MAILBOX_JPEG)
        self.assertEqual(self.box.peek(1), MAILBOX_JPEG)
        self.age('.frame', 3.5)
        self.assertIsNone(self.box.peek(1))
        self.assertEqual(self.box.latest(1)[1], MAILBOX_JPEG)  # still shown to a viewer

    def test_cameras_and_databases_do_not_see_each_others_frames(self):
        self.box.put_frame(1, MAILBOX_JPEG)
        self.assertIsNone(self.box.latest(2)[1])
        self.assertIsNone(mailbox_live.Mailbox('other').latest(1)[1])

    def test_a_frame_that_is_not_a_jpeg_is_refused(self):
        self.box.want(1)
        for bad in (b'', b'GIF89a', b'\xff\xd8' + b'x' * mailbox_live.MAX_FRAME):
            with self.assertRaises(ValueError):
                self.box.accept_frame(1, bad)
        self.assertIsNone(self.box.latest(1)[1])


class DemandTest(MailboxTestCase):
    def test_a_camera_is_wanted_only_while_someone_asked_recently(self):
        self.assertFalse(self.box.wanted(1))
        self.box.want(1)
        self.assertTrue(self.box.wanted(1))
        self.age('.want', mailbox_live.IDLE_S + 1)
        self.assertFalse(self.box.wanted(1))

    def test_a_frame_nobody_asked_for_is_not_kept(self):
        self.assertFalse(self.box.accept_frame(1, MAILBOX_JPEG))
        self.assertIsNone(self.box.latest(1)[1])
        self.box.want(1)
        self.assertTrue(self.box.accept_frame(1, MAILBOX_JPEG))
        self.assertEqual(self.box.latest(1)[1], MAILBOX_JPEG)

    def test_the_last_picture_is_removed_when_the_service_polls_and_nobody_watches(self):
        self.box.want(1)
        self.box.accept_frame(1, MAILBOX_JPEG)
        self.age('.want', mailbox_live.IDLE_S + 1)
        self.box.exchange({'1': {}})
        self.assertFalse(os.path.exists(Path(self.folder) / 'cameras-1.frame'))


class ExchangeTest(MailboxTestCase):
    def test_the_service_is_connected_only_while_it_keeps_polling(self):
        self.assertFalse(self.box.service_online())
        self.box.exchange({})
        self.assertTrue(self.box.service_online())
        self.age('.service', mailbox_live.SERVICE_S + 1)
        self.assertFalse(self.box.service_online())

    def test_the_answer_says_which_cameras_are_wanted(self):
        self.box.want(2)
        answer = self.box.exchange({'1': {}, '2': {}})
        self.assertFalse(answer['1']['wanted'])
        self.assertTrue(answer['2']['wanted'])

    def test_what_the_service_reports_is_kept_and_a_clean_report_clears_it(self):
        self.box.exchange({'1': {'error': 'no se puede abrir el vídeo', 'message': None}})
        self.assertEqual(self.box.status(1), ('no se puede abrir el vídeo', None))
        self.box.exchange({'1': {'error': None, 'message': None}})
        self.assertEqual(self.box.status(1), (None, None))

    def test_a_movement_problem_is_shown_for_a_few_seconds_only(self):
        self.box.exchange({'1': {'message': 'La cámara no obedece: sin respuesta'}})
        self.assertEqual(self.box.status(1)[1], 'La cámara no obedece: sin respuesta')
        mailbox_live.MESSAGE_S, original = 0.05, mailbox_live.MESSAGE_S
        self.addCleanup(setattr, mailbox_live, 'MESSAGE_S', original)
        time.sleep(0.1)
        self.assertIsNone(self.box.status(1)[1])

    def test_a_report_without_a_message_does_not_erase_the_message_just_received(self):
        self.box.exchange({'1': {'message': 'fallo'}})
        self.box.exchange({'1': {'error': None, 'message': None}})
        self.assertEqual(self.box.status(1)[1], 'fallo')

    def test_garbage_reports_are_harmless(self):
        answer = self.box.exchange({'1': 'x', '2': None, '3': {'error': 5, 'message': ['a']}})
        self.assertEqual(set(answer), {'1', '2', '3'})


class CommandTest(MailboxTestCase):
    def online(self):
        self.box.exchange({})

    def test_nothing_can_be_sent_while_the_service_is_not_connected(self):
        with self.assertRaises(mailbox_live.ServiceOffline):
            self.box.send(1, {'op': 'stop'})
        self.assertEqual(self.box.take(1), [])

    def test_commands_reach_the_service_once_and_in_order(self):
        self.online()
        self.box.send(1, {'op': 'move', 'dx': 1, 'dy': 0})
        self.box.send(1, {'op': 'stop'})
        answer = self.box.exchange({'1': {}})
        self.assertEqual([c['op'] for c in answer['1']['commands']], ['move', 'stop'])
        self.assertEqual(self.box.exchange({'1': {}})['1']['commands'], [])

    def test_commands_of_another_camera_are_not_delivered(self):
        self.online()
        self.box.send(2, {'op': 'stop'})
        self.assertEqual(self.box.exchange({'1': {}})['1']['commands'], [])
        self.assertEqual(len(self.box.exchange({'2': {}})['2']['commands']), 1)

    def test_a_command_that_waited_too_long_is_dropped(self):
        self.online()
        self.box.send(1, {'op': 'move', 'dx': 1, 'dy': 0})
        mailbox_live.COMMAND_TTL_S, original = 0.05, mailbox_live.COMMAND_TTL_S
        self.addCleanup(setattr, mailbox_live, 'COMMAND_TTL_S', original)
        time.sleep(0.1)
        self.assertEqual(self.box.take(1), [])

    def test_sending_a_command_also_marks_the_camera_as_wanted(self):
        self.online()
        self.box.send(1, {'op': 'stop'})
        self.assertTrue(self.box.wanted(1))

    def test_two_workers_never_deliver_the_same_command_twice(self):
        self.online()
        self.box.send(1, {'op': 'stop'})
        first, second = mailbox_live.Mailbox('cameras'), mailbox_live.Mailbox('cameras')
        self.assertEqual(len(first.take(1)) + len(second.take(1)), 1)


class DirectoryTest(unittest.TestCase):
    def test_a_directory_others_can_read_is_refused(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        os.chmod(folder, 0o755)
        self.addCleanup(setattr, mailbox_live, 'DIR', mailbox_live.DIR)
        mailbox_live.DIR = folder
        with self.assertRaisesRegex(RuntimeError, 'privado'):
            mailbox_live.Mailbox('cameras').want(1)

    def test_the_default_directory_is_private(self):
        with tempfile.TemporaryDirectory() as parent:
            with patch.object(mailbox_live, 'DIR', None), \
                    patch.object(mailbox_live.tempfile, 'gettempdir', return_value=parent):
                folder = mailbox_live._directory()
                self.assertEqual(oct(folder.stat().st_mode & 0o777), '0o700')


class LivePageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def camera(self, name, code, active=True):
        pool = Pool()
        location, = pool.get('videovigilance.location').create([{'name': name}])
        camera, = pool.get('videovigilance.camera').create([{
                    'name': name, 'code': code, 'host': 'h', 'rtsp_port': 554,
                    'stream_path': 's', 'rotation': '0', 'location': location.id,
                    'active': active}])
        return camera

    def render(self, query):
        request = SimpleNamespace(args=MultiDict(query), user_id=None)
        context = VoyagerContext(request=request,
            session=SimpleNamespace(session_id='live-page-test'))
        with Transaction().set_context(voyager_context=context):
            with patch('trytond.modules.videovigilance.www.live.current_user',
                    return_value=object()), \
                    patch('trytond.modules.videovigilance.www.live.is_admin',
                        return_value=True), \
                    patch('trytond.modules.videovigilance.www.live.component',
                        side_effect=lambda *args: div()), \
                    patch('trytond.modules.videovigilance.www.live.nav',
                        side_effect=lambda *args: div()), \
                    patch('trytond.modules.videovigilance.www.live.page',
                        side_effect=lambda content, **values: content), \
                    patch('trytond.modules.videovigilance.www.live.url',
                        side_effect=lambda name, **values: '/%s/%s' % (
                            name, values.get('code', ''))):
                return Pool().get('videovigilance.web.live')().render().render()

    @with_transaction()
    def test_query_selects_an_active_camera_and_invalid_or_missing_codes_fall_back(self):
        self.camera('First', 'FIRST')
        self.camera('Selected', 'SELECTED')
        for query, expected in [
                ([('code', 'SELECTED')], 'SELECTED'),
                ([('code', 'UNKNOWN')], 'FIRST'),
                ([], 'FIRST')]:
            with self.subTest(query=query):
                html = self.render(query)
                self.assertIn('live_frame/%s' % expected, html)
                self.assertIn('live_move/%s' % expected, html)
                self.assertIn('live_stop/%s' % expected, html)

    @with_transaction()
    def test_an_inactive_camera_falls_back_and_no_controls_render_without_active_camera(
            self):
        first = self.camera('First', 'FIRST')
        inactive = self.camera('Inactive', 'INACTIVE', active=False)
        html = self.render([('code', inactive.code)])
        self.assertIn('live_frame/FIRST', html)
        self.assertNotIn('live_frame/INACTIVE', html)

        Pool().get('videovigilance.camera').write([first], {'active': False})
        html = self.render([('code', 'INACTIVE')])
        self.assertNotIn('id="live"', html)


class VideovigilanceTestCase(ModuleTestCase):
    'Test Videovigilance module'
    module = 'videovigilance'

    @with_transaction(user=0)
    def test_wizards(self):
        # The shared framework check must also inspect group-restricted wizards.
        return super().test_wizards.__wrapped__(self)


class WebLanguageTestCase(RouteTestCase):
    module = 'videovigilance'
    language = 'es'

    @classmethod
    def setUpDatabase(cls):
        Lang = Pool().get('ir.lang')
        Lang.write(Lang.search([('code', '=', 'en')]), {'translatable': True})
        Pool().get('www.site').create([{
                    'name': 'Avisos', 'type': 'videovigilance_alarm',
                    'url': 'http://localhost'}])

    def test_browser_language_selects_login_translation(self):
        url = f'/{self.db_name}/avisos/'
        spanish = self.client().get(url, headers={'Accept-Language': 'es-ES,es;q=0.9'})
        english = self.client().get(url, headers={'Accept-Language': 'en'})
        self.assertEqual(spanish.status_code, 200)
        self.assertIn('Correo electrónico', spanish.get_data(as_text=True))
        self.assertIn('Email', english.get_data(as_text=True))

    @with_transaction()
    def test_disarming_warns_that_notices_stop(self):
        Configuration = Pool().get('videovigilance.configuration')
        prefix = 'trytond.modules.videovigilance.www.alarm.'
        with Transaction().set_context(language='es'), \
                patch(prefix + 'current_user', return_value=object()), \
                patch(prefix + 'is_admin', return_value=True), \
                patch(prefix + 'hidden_csrf'), \
                patch(prefix + 'url', return_value='/alarm'), \
                patch.object(Configuration, 'is_armed', return_value=True), \
                patch.object(Configuration, 'get_singleton', return_value=None):
            html = Pool().get('videovigilance.web.alarm_card')().render().render()
        self.assertIn('No se enviarán avisos.', unescape(html))


class OpenWebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def open(self):
        Wizard = Pool().get('videovigilance.open_web', type='wizard')
        session_id, _, _ = Wizard.create()
        return Wizard.execute(session_id, {}, 'open')

    @with_transaction()
    def test_without_a_web_app_it_says_so_instead_of_opening_a_blank_page(self):
        with self.assertRaises(UserError):
            self.open()

    @with_transaction()
    def test_it_opens_the_address_of_the_web_app(self):
        company = create_company()
        with set_company(company):
            Pool().get('notification.web.application').create([{
                        'name': 'Avisos', 'code': 'videovigilance', 'company': company.id,
                        'origin': 'https://tryton.example', 'base_path': '/cameras/avisos/'}])
        action, = self.open()['actions']
        self.assertEqual(action[0]['url'], 'https://tryton.example/cameras/avisos/')


JPEG = b'\xff\xd8\xff\xe0' + b'0' * 64


class ServiceRoutesTestCase(RouteTestCase):
    "Test the routes of the detection service"
    module = 'videovigilance'
    language = 'es'

    key = uuid.uuid4().hex
    second_key = uuid.uuid4().hex
    inactive_key = uuid.uuid4().hex
    cancelled_key = uuid.uuid4().hex
    other_application_key = uuid.uuid4().hex
    unscoped_key = uuid.uuid4().hex

    @classmethod
    def setUpClass(cls):
        cls.had_section = config.has_section('videovigilance')
        if not cls.had_section:
            config.add_section('videovigilance')
        cls.secret_key = config.get('videovigilance', 'secret_key', default='')
        if cls.had_section:
            cls.addClassCleanup(config.set, 'videovigilance', 'secret_key', cls.secret_key)
        else:
            cls.addClassCleanup(config.remove_section, 'videovigilance')
        config.set('videovigilance', 'secret_key', Fernet.generate_key().decode())
        cls.folder = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.folder, True)
        cls.addClassCleanup(setattr, live, 'DIR', live.DIR)
        live.DIR = cls.folder
        super().setUpClass()

    @classmethod
    def setUpDatabase(cls):
        pool = Pool()
        Application = pool.get('res.user.application')
        # A user without any group: a scoped key is all the service has.
        spanish, = pool.get('ir.lang').search([('code', '=', 'es')])
        user, = pool.get('res.user').create([{
                    'name': 'Servei', 'login': 'servei', 'language': spanish.id}])
        cls.user = user.id
        Location = pool.get('videovigilance.location')
        (cls.location, cls.second_location, cls.inactive_location,
            cls.cancelled_location) = Location.create([
                {'name': 'Casa'}, {'name': 'Oficina'}, {'name': 'Magatzem', 'active': False},
                {'name': 'Cancel·lada'}])
        applications = [
            (cls.key, 'videovigilance', 'validated', cls.location.id),
            (cls.second_key, 'videovigilance', 'validated', cls.second_location.id),
            (cls.inactive_key, 'videovigilance', 'validated', cls.inactive_location.id),
            (cls.cancelled_key, 'videovigilance', 'cancelled', cls.cancelled_location.id),
            (cls.other_application_key, 'rest', 'validated', None),
            (cls.unscoped_key, 'videovigilance', 'validated', None)]
        for key, application, state, location in applications:
            Application(
                key=key, user=user.id, application=application, state=state,
                videovigilance_location=location).save()
        entrance, _ = pool.get('videovigilance.camera').create([
                {'name': 'Entrada', 'code': 'CAM1', 'host': '10.0.0.5', 'rtsp_port': 554,
                    'stream_path': 'stream1', 'rotation': '0', 'location': cls.location.id,
                    'username': 'user', 'password': 'secreta'},
                {'name': 'Retirada', 'code': 'CAM2', 'host': '10.0.0.6', 'rtsp_port': 554,
                    'stream_path': 'stream1', 'rotation': '0', 'location': cls.location.id,
                    'active': False}])
        cls.camera = entrance.id
        second_camera, = pool.get('videovigilance.camera').create([{
                'name': 'Garagem', 'code': 'CAM3', 'host': '10.0.1.5', 'rtsp_port': 554,
                'stream_path': 'stream1', 'rotation': '0', 'location': cls.second_location.id}])
        cls.second_camera = second_camera.id

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
                self.headers(self.unscoped_key),
                self.headers(self.inactive_key),
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

    def test_each_key_receives_only_its_locations_active_camera(self):
        response = self.client().get(
            self.url('config'), headers=self.headers(self.second_key))
        camera, = response.get_json()['cameras']
        self.assertEqual((camera['id'], camera['code']), (self.second_camera, 'CAM3'))

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

    def test_event_alarm_uses_service_user_language(self):
        self.assertEqual(self.put_event('language').status_code, HTTPStatus.CREATED)
        with Transaction().start(self.db_name, 0):
            Alarm = Pool().get('videovigilance.alarm')
            alarm, = Alarm.search([('event.uid', '=', 'language')])
            self.assertEqual(alarm.title, 'Persona detectada')
            self.assertIn('Personas: 2', alarm.body)

    def test_an_event_for_another_location_is_not_stored(self):
        response = self.put_event('foreign-event', self.event(camera=self.second_camera))
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        self.assertEqual(self.events('foreign-event'), [])

    def test_a_uid_collision_with_another_location_is_not_acknowledged(self):
        created = self.client().put(
            self.url('events/foreign-collision'),
            data=self.event(camera=self.second_camera), headers=self.headers(self.second_key))
        self.assertEqual(created.status_code, HTTPStatus.CREATED)
        response = self.put_event('foreign-collision')
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        events = self.events('foreign-collision')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], 'foreign-collision')

    def test_an_event_returned_by_a_concurrent_request_is_checked_for_its_location(self):
        uid = 'concurrent-foreign-event'
        Event = Pool(self.db_name).get('videovigilance.event')
        create_once = Event.create_once

        def concurrent_insert(values):
            Event.create([{
                        'uid': uid, 'camera': self.second_camera, 'type': 'person_detection',
                        'started_at': datetime(2026, 10, 27, 10),
                        'ended_at': datetime(2026, 10, 27, 11),
                        }])
            return create_once(values)

        # A competing location's event becomes visible before the idempotency lookup.
        with patch.object(Event, 'create_once', side_effect=concurrent_insert):
            response = self.put_event(uid)

        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        events = self.events(uid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], uid)

    def test_an_event_that_another_request_is_storing_is_asked_again_not_duplicated(self):
        self.assertEqual(self.put_event('race').status_code, HTTPStatus.CREATED)
        Event = Pool(self.db_name).get('videovigilance.event')
        # Two requests missed each other's event: only the unique uid stops the second one.
        with patch.object(Event, 'search', return_value=[]):
            response = self.put_event('race')
        self.assertEqual(response.status_code, HTTPStatus.CONFLICT)
        self.assertEqual(len(self.events('race')), 1)

    def test_an_event_too_big_is_refused_with_a_reason(self):
        data = {**self.event(), 'snapshot': (io.BytesIO(JPEG + b'0' * 5_000_000), 's.jpg')}
        response = self.put_event('big', data)
        self.assertEqual(response.status_code, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertIn('error', response.get_json())

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
                ('a count that is not a number', 'bad10', self.event(max_persons='many'), 400),
                ('a boolean taken for a number', 'bad11', self.event(max_confidence=True), 400),
                ('a time that cannot be put in UTC', 'bad12',
                    self.event(started_at='9999-12-31T23:59:59-05:00'), 400),
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

    def test_a_heartbeat_mixed_with_another_location_is_rejected_before_updates(self):
        @with_transaction()
        def state():
            pool = Pool()
            Camera = pool.get('videovigilance.camera')
            cameras = Camera.browse([self.camera, self.second_camera])
            configuration = pool.get('videovigilance.configuration').get_singleton()
            return ([(c.status, c.status_detail, c.status_checked_at) for c in cameras],
                configuration.service_last_seen if configuration else None)
        before = state()
        response = self.client().post(
            self.url('heartbeat'), headers=self.headers(),
            json={'cameras': [
                {'code': 'CAM1', 'online': True, 'detail': 'should not stick'},
                {'code': 'CAM3', 'online': True, 'detail': 'foreign'}]})
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

        self.assertEqual(state(), before)

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


    def test_a_frame_for_another_location_is_not_stored(self):
        response = self.client().put(
            self.url(f'cameras/{self.second_camera}/frame'), data=JPEG,
            headers={**self.headers(), 'Content-Type': 'image/jpeg'})
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)

        @with_transaction()
        def check():
            camera = Pool().get('videovigilance.camera')(self.second_camera)
            self.assertFalse(camera.frame)
        check()

    def test_a_picture_that_is_not_one_or_is_too_big_or_has_no_camera_is_refused(self):
        def put(camera, data):
            return self.client().put(
                self.url(f'cameras/{camera}/frame'), data=data, headers=self.headers())
        self.assertEqual(put(self.camera, b'not a jpeg').status_code, HTTPStatus.BAD_REQUEST)
        too_big = put(self.camera, JPEG + b'0' * (MAX_HEARTBEAT_FRAME + 2048))
        self.assertEqual(too_big.status_code, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertIn('error', too_big.get_json())
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

    def test_a_live_poll_with_a_foreign_camera_is_rejected_before_mailbox_access(self):
        with patch.object(live.Mailbox, 'exchange') as exchange:
            response = self.client().post(
                self.url('live/poll'), headers=self.headers(),
                json={'cameras': {str(self.second_camera): {
                    'error': None, 'message': None}}})
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        exchange.assert_not_called()

    def test_a_live_frame_for_another_location_is_not_accepted(self):
        with patch.object(live.Mailbox, 'accept_frame') as accept_frame:
            response = self.client().post(
                self.url(f'live/{self.second_camera}/frame'), data=JPEG,
                headers=self.headers())
        self.assertEqual(response.status_code, HTTPStatus.NOT_FOUND)
        accept_frame.assert_not_called()

    def test_a_live_frame_says_whether_somebody_is_still_watching(self):
        response = self.client().post(
            self.url(f'live/{self.camera}/frame'), data=JPEG, headers=self.headers())
        self.assertEqual(response.get_json(), {'wanted': False})
        refused = self.client().post(
            self.url(f'live/{self.camera}/frame'), data=b'not a jpeg', headers=self.headers())
        self.assertEqual(refused.status_code, HTTPStatus.BAD_REQUEST)


PASSWORD = 'Clave-de-prueba-9'


class SessionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def user(self, group='group_reader', **values):
        pool = Pool()
        groups = [pool.get('ir.model.data').get_id('videovigilance', group)] if group else []
        user, = pool.get('res.user').create([{
                    'name': 'Ana', 'login': 'ana', 'email': 'ana@gmail.com',
                    'groups': [('add', groups)], **values}])
        return user

    def account(self, email='cuenta@gmail.com', password=PASSWORD, **values):
        return Pool().get('web.user').create([{
                    'email': email, 'password': password, **values}])[0]

    def browser(self, account=None, session_id='browser-session'):
        "The Voyager session of the browser, with the web account logged in if any."
        pool = Pool()
        Site, Session = pool.get('www.site'), pool.get('www.session')
        sites = Site.search([('type', '=', SITE_TYPE)], limit=1)
        site = sites[0] if sites else Site.create([
                {'name': 'avisos', 'type': SITE_TYPE, 'url': 'http://x'}])[0]
        session, = Session.create([{
                    'site': site.id, 'session_id': session_id,
                    'user': account.id if account else None,
                    'expiration_date': datetime.now() + timedelta(days=1)}])
        return session

    def serving(self, user=None, token='tryton-token', account=None,
            session_id='browser-session'):
        """The context of a request that reached the web app: with the Tryton session of
        `user`, and/or from a browser where `account` is logged in."""
        request = SimpleNamespace(
            user_id=user.id if user else None,
            session=SimpleNamespace(token=token) if user and token else None)
        return Transaction().set_context(voyager_context=VoyagerContext(
                request=request, session=self.browser(account, session_id)))

    @with_transaction()
    def test_the_user_of_the_tryton_session_gets_in_with_either_group(self):
        for group in ('group_reader', 'group_admin'):
            user = self.user(group, login=group)
            with self.serving(user):
                self.assertEqual(current_user(), user)

    @with_transaction()
    def test_without_any_session_there_is_no_user_and_the_page_asks_to_log_in(self):
        with self.serving():
            self.assertIsNone(tryton_user())
            self.assertIsNone(current_user())
            self.assertEqual(access_problem(tryton_user())[0], 401)

    @with_transaction()
    def test_a_tryton_user_outside_the_groups_is_refused(self):
        user = self.user(group=None)
        with self.serving(user):
            self.assertIsNone(current_user())
            self.assertEqual(access_problem(tryton_user())[0], 403)

    @with_transaction()
    def test_a_user_without_email_is_refused_because_notices_are_addressed_to_it(self):
        for number, email in enumerate((None, '', '   ')):
            user = self.user(login='mail-%d' % number, email=email)
            with self.serving(user):
                self.assertIsNone(current_user(), repr(email))
                self.assertEqual(access_problem(tryton_user())[0], 403)

    @with_transaction()
    def test_a_web_account_logged_in_gets_in_but_never_as_an_administrator(self):
        account = self.account()
        with self.serving(account=account):
            self.assertEqual(current_user(), account)
            self.assertFalse(is_admin(account))
            self.assertEqual(display_name(account), 'cuenta@gmail.com')

    @with_transaction()
    def test_a_web_account_that_lost_its_password_or_was_deactivated_is_out(self):
        WebUser = Pool().get('web.user')
        account = self.account()
        with self.serving(account=account, session_id='one'):
            self.assertEqual(current_user(), account)
        WebUser.write([account], {'password': None})
        with self.serving(account=account, session_id='two'):
            self.assertIsNone(current_user())
        WebUser.write([account], {'password': PASSWORD, 'active': False})
        with self.serving(account=account, session_id='three'):
            self.assertIsNone(current_user())

    @with_transaction()
    def test_the_tryton_session_wins_over_the_web_account(self):
        user, account = self.user(), self.account()
        with self.serving(user, account=account):
            self.assertEqual(current_user(), user)
        # Without the group the Tryton session is of no use, and the web account still is.
        outsider = self.user(group=None, login='outsider')
        with self.serving(outsider, account=account, session_id='other'):
            self.assertEqual(current_user(), account)

    @with_transaction()
    def test_the_csrf_token_of_a_tryton_session_only_works_for_that_session(self):
        user = self.user()
        with self.serving(user, token='first-session'):
            token = csrf()
            self.assertTrue(csrf_ok(token))
        # Logging out in Tryton and in again is a new token: the old form is worthless.
        with self.serving(user, token='second-session'):
            self.assertFalse(csrf_ok(token))

    @with_transaction()
    def test_without_a_tryton_session_the_csrf_token_is_bound_to_the_browser(self):
        with self.serving(session_id='browser-a'):
            token = csrf()
            self.assertTrue(csrf_ok(token))
        with self.serving(session_id='browser-b'):
            self.assertFalse(csrf_ok(token))
            for value in ('', None):
                self.assertFalse(csrf_ok(value))

    @with_transaction()
    def test_the_csrf_token_follows_the_tryton_session_while_there_is_one(self):
        user, account = self.user(), self.account()
        with self.serving(user, token='tryton', account=account, session_id='both'):
            under_tryton = csrf()
        # The Tryton session ends and the browser keeps its web account: the form made under
        # the Tryton session is worthless and the new one is bound to the browser.
        with self.serving(account=account, session_id='both'):
            self.assertFalse(csrf_ok(under_tryton))
            self.assertTrue(csrf_ok(csrf()))

    @with_transaction()
    def test_logging_in_and_out_a_web_account_changes_the_session_id(self):
        account = self.account()
        session = self.browser(session_id='fixed-by-an-attacker')
        with Transaction().set_context(voyager_context=VoyagerContext(
                request=SimpleNamespace(user_id=None, session=None), session=session)):
            rotate_session(account)
            self.assertEqual(session.user, account)
            self.assertNotEqual(session.session_id, 'fixed-by-an-attacker')
            logged = session.session_id
            rotate_session()
            self.assertIsNone(session.user)
            self.assertNotEqual(session.session_id, logged)

    @with_transaction()
    def test_a_web_account_logs_in_with_its_email_and_password_only(self):
        account = self.account()
        for email in ('cuenta@gmail.com', '  Cuenta@Gmail.com '):
            self.assertEqual(authenticate_web_user(email, PASSWORD), account)
        for email, password in (('cuenta@gmail.com', 'otra'), ('nadie@gmail.com', PASSWORD),
                ('cuenta@gmail.com', ''), ('cuenta@gmail.com', None), (None, PASSWORD)):
            self.assertIsNone(authenticate_web_user(email, password), (email, password))

    @with_transaction()
    def test_an_account_created_only_to_receive_notices_cannot_log_in_and_does_not_fail(self):
        # The ones web_user_for creates for the Tryton users have no password at all.
        self.account('avisos@gmail.com', password=None)
        for password in ('', 'x', PASSWORD):
            self.assertIsNone(authenticate_web_user('avisos@gmail.com', password))
        # web.user.authenticate fails before it can count them: the wrapper counts them.
        self.assertEqual(
            Pool().get('web.user.authenticate.attempt').count('avisos@gmail.com'), 3)

    @with_transaction()
    def test_notices_reach_the_tryton_users_with_access_and_the_web_accounts_that_can_log_in(self):
        pool = Pool()
        company = create_company()
        with set_company(company):
            application, = pool.get('notification.web.application').create([{
                        'name': 'Avisos', 'code': 'videovigilance', 'company': company.id,
                        'origin': 'https://tryton.example', 'base_path': '/cameras/avisos/'}])
        linked = self.account('ana@gmail.com', password=None)
        self.user(web_user=linked.id)
        outsider_account = self.account('fuera@gmail.com', password=None)
        self.user(group=None, login='fuera', email='fuera@gmail.com',
            web_user=outsider_account.id)
        own = self.account('propia@gmail.com')
        no_password = self.account('sinclave@gmail.com', password=None)
        self.assertTrue(application.eligible_user(linked))
        self.assertFalse(application.eligible_user(outsider_account))
        self.assertTrue(application.eligible_user(own))
        self.assertFalse(application.eligible_user(no_password))

    @with_transaction()
    def test_a_session_left_by_the_old_login_stops_acting_as_its_user(self):
        pool = Pool()
        Session = pool.get('www.session')
        account = self.account()
        old = self.browser(session_id='old')
        Session.write([old], {'system_user': self.user().id, 'user': account.id})
        kept = self.browser(account, session_id='web')
        Session.__register__('videovigilance')
        # Searching goes to the database: the cleanup is SQL, which the record cache never sees.
        self.assertEqual(Session.search_count(
                [('id', '=', old.id), ('system_user', '=', None), ('user', '=', None)]), 1)
        # A web account that logged in with the new login is not logged out by the update.
        self.assertEqual(Session.search_count(
                [('id', '=', kept.id), ('user', '=', account.id)]), 1)


del ModuleTestCase
del RouteTestCase
