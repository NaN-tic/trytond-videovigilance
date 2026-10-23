# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""The calls to the managers, against a stub of the Yeastar API (no PBX needed).

Run with the modules installed:  python -m unittest trytond.modules.videovigilance.tests.test_call
"""
import json
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from trytond.modules.company.tests import create_company, set_company
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction
from trytond.transaction import Transaction


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
        activate_module('videovigilance')
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
    def test_health_notices_never_call(self):
        _, camera = self.scenario()
        Pool().get('videovigilance.alarm').raise_notices([{
                    'type': 'camera_offline', 'camera': camera.id,
                    'title': 'x', 'body': 'y'}])
        self.run_queue()
        self.assertEqual(Pbx.dials, [])

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
        with patch('trytond.modules.videovigilance.call.synthesize',
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
