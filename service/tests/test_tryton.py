"""Tests for the client of Tryton's HTTP API, against a small server that plays Tryton."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from service.tryton import CameraConfig, TrytonClient, TrytonRejected, TrytonUnavailable

KEY = 'the-key'
JPEG = b'\xff\xd8jpeg'
ROW = {'id': 3, 'code': 'CAM1', 'host': '10.0.0.5', 'rtsp_port': 554, 'stream_path': 's1',
    'secondary_stream_path': None, 'username': 'u', 'password': 'pw', 'rotation': 90,
    'ptz': True, 'onvif_enabled': True, 'onvif_port': 2020, 'detect_persons': True,
    'detect_motion': False}


def answer(status, body=None, **headers):
    data = b'' if body is None else json.dumps(body).encode()
    return status, {'Content-Type': 'application/json', **headers}, data


class Tryton(BaseHTTPRequestHandler):
    "`answers` maps 'METHOD /path' to what to answer; whatever is not there is a web page 404."
    log = []
    answers = {}

    def log_message(self, *args):
        pass

    def handle_any(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        type(self).log.append((self.command, self.path, dict(self.headers), body))
        if self.headers.get('Authorization') != f'Bearer {KEY}':
            status, headers, data = 401, {}, b''
        else:
            status, headers, data = type(self).answers.get(
                f'{self.command} {self.path}', (404, {}, b'<html>404</html>'))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_PUT = do_POST = handle_any


class ClientTest(unittest.TestCase):
    def setUp(self):
        Tryton.log = []
        Tryton.answers = {}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Tryton)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/cameras/videovigilance'
        self.client = TrytonClient(self.url, KEY)

    def test_a_key_that_tryton_refuses_is_unavailable_not_a_rejection_of_the_data(self):
        client = TrytonClient(self.url, 'another-key')
        with self.assertRaisesRegex(TrytonUnavailable, 'clave'):
            client.create_event({'uid': 'a'}, None)

    def test_cameras_come_from_config_and_are_not_downloaded_again_when_unchanged(self):
        path = 'GET /cameras/videovigilance/config'
        Tryton.answers[path] = answer(200, {'cameras': [ROW]}, ETag='"v1"')
        cameras = self.client.cameras()
        self.assertEqual(cameras, [CameraConfig(
                    id=3, code='CAM1', host='10.0.0.5', rtsp_port=554, stream_path='s1',
                    rotation=90, username='u', password='pw', detect_motion=False)])
        Tryton.answers[path] = answer(304)
        self.assertEqual(self.client.cameras(), cameras)
        self.assertNotIn('If-None-Match', Tryton.log[0][2])
        self.assertEqual(Tryton.log[1][2]['If-None-Match'], '"v1"')

    def test_an_event_goes_to_its_uid_with_its_fields_and_snapshot(self):
        Tryton.answers['PUT /cameras/videovigilance/events/abc'] = answer(201, {'uid': 'abc'})
        event = {'uid': 'abc', 'camera': 3, 'type': 'person_detection',
            'started_at': '2026-10-27T10:00:00', 'ended_at': '2026-10-27T10:00:05'}
        self.client.create_event(event, JPEG)
        _, _, headers, body = Tryton.log[0]
        self.assertTrue(headers['Content-Type'].startswith('multipart/form-data'))
        self.assertIn(b'name="event"', body)
        self.assertIn(json.dumps(event).encode(), body)
        self.assertIn(b'name="snapshot"', body)
        self.assertIn(JPEG, body)

    def test_an_event_without_snapshot_sends_no_snapshot_part(self):
        Tryton.answers['PUT /cameras/videovigilance/events/abc'] = answer(200, {'uid': 'abc'})
        self.client.create_event({'uid': 'abc'}, None)
        self.assertNotIn(b'name="snapshot"', Tryton.log[0][3])

    def test_only_a_refusal_that_tryton_explains_is_a_rejection(self):
        path = 'PUT /cameras/videovigilance/events/abc'
        for status, body, error in [
                (400, {'error': 'tipo de evento no admitido'}, TrytonRejected),
                (400, {'status': 400, 'message': 'Validation', 'description': 'rango'},
                    TrytonRejected),
                (404, {'error': 'no existe la cámara'}, TrytonRejected),
                (413, {'error': 'demasiado grande'}, TrytonRejected),
                # A Tryton without the API answers 404 with a web page: that is not the event's fault.
                (404, None, TrytonUnavailable),
                (500, {'error': 'boom'}, TrytonUnavailable),
                ]:
            with self.subTest(status=status, body=body):
                Tryton.answers[path] = answer(status, body)
                with self.assertRaises(error):
                    self.client.create_event({'uid': 'abc'}, None)
        Tryton.answers.clear()
        with self.assertRaises(TrytonUnavailable):
            self.client.create_event({'uid': 'abc'}, None)

    def test_heartbeat_reports_the_cameras_without_frames_and_then_sends_each_frame(self):
        intervals = {'camera_interval': 5, 'camera_failures': 1, 'service_interval': 5}
        Tryton.answers['POST /cameras/videovigilance/heartbeat'] = answer(200, intervals)
        Tryton.answers['PUT /cameras/videovigilance/cameras/3/frame'] = answer(204)
        reply = self.client.heartbeat([
                {'id': 3, 'code': 'CAM1', 'online': True, 'detail': '', 'frame': JPEG},
                {'id': 4, 'code': 'CAM2', 'online': False, 'detail': 'sin respuesta'}])
        self.assertEqual(reply, intervals)
        self.assertEqual([(entry[0], entry[1]) for entry in Tryton.log], [
                ('POST', '/cameras/videovigilance/heartbeat'),
                ('PUT', '/cameras/videovigilance/cameras/3/frame')])
        self.assertEqual(json.loads(Tryton.log[0][3]), {'cameras': [
                    {'code': 'CAM1', 'online': True, 'detail': ''},
                    {'code': 'CAM2', 'online': False, 'detail': 'sin respuesta'}]})
        self.assertEqual((Tryton.log[1][2]['Content-Type'], Tryton.log[1][3]), ('image/jpeg', JPEG))

    def test_a_frame_that_cannot_be_kept_does_not_fail_the_heartbeat(self):
        Tryton.answers['POST /cameras/videovigilance/heartbeat'] = answer(200, {'camera_interval': 5})
        Tryton.answers['PUT /cameras/videovigilance/cameras/3/frame'] = answer(
            404, {'error': 'no existe la cámara'})
        with self.assertLogs('service.tryton', 'WARNING') as logged:
            reply = self.client.heartbeat(
                [{'id': 3, 'code': 'CAM1', 'online': True, 'detail': '', 'frame': JPEG}])
        self.assertEqual(reply, {'camera_interval': 5})
        self.assertIn('CAM1', logged.output[0])

    def test_poll_returns_the_answers_by_camera_number(self):
        Tryton.answers['POST /cameras/videovigilance/live/poll'] = answer(
            200, {'cameras': {'3': {'wanted': True, 'commands': []}}})
        reply = self.client.poll({3: {'error': None, 'message': None}})
        self.assertEqual(reply, {3: {'wanted': True, 'commands': []}})
        self.assertEqual(
            json.loads(Tryton.log[0][3]), {'cameras': {'3': {'error': None, 'message': None}}})

    def test_push_says_whether_somebody_is_still_watching(self):
        Tryton.answers['POST /cameras/videovigilance/live/3/frame'] = answer(200, {'wanted': False})
        self.assertFalse(self.client.push(3, JPEG))
        self.assertEqual((Tryton.log[0][2]['Content-Type'], Tryton.log[0][3]), ('image/jpeg', JPEG))

    def test_an_answer_that_is_not_the_expected_shape_is_unavailable(self):
        Tryton.answers['POST /cameras/videovigilance/live/poll'] = answer(200, {'nothing': 1})
        Tryton.answers['POST /cameras/videovigilance/live/3/frame'] = answer(200, {'nothing': 1})
        Tryton.answers['GET /cameras/videovigilance/config'] = answer(200, {'nothing': 1})
        with self.assertRaisesRegex(TrytonUnavailable, 'inesperada'):
            self.client.poll({})
        with self.assertRaisesRegex(TrytonUnavailable, 'inesperada'):
            self.client.push(3, JPEG)
        with self.assertRaisesRegex(TrytonUnavailable, 'inesperada'):
            self.client.cameras()

    def test_an_unreachable_tryton_is_unavailable_not_a_crash(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(TrytonUnavailable):
            self.client.poll({})


if __name__ == '__main__':
    unittest.main()
