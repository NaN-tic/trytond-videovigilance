"""Tests for the ONVIF client (no camera needed): a fake camera answers over HTTP."""
import base64
import hashlib
import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from service import onvif

NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:tt="http://www.onvif.org/ver10/schema" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
    'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
    'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"')
DEVICE = ('<tds:Manufacturer>tp-link</tds:Manufacturer><tds:Model> Tapo TC43</tds:Model>'
    '<tds:FirmwareVersion>1.4.1 Build 260612</tds:FirmwareVersion>'
    '<tds:SerialNumber>746163c2</tds:SerialNumber><tds:HardwareId>2.0</tds:HardwareId>')


def envelope(body):
    return f'<s:Envelope {NS}><s:Body>{body}</s:Body></s:Envelope>'.encode()


class FakeCamera(BaseHTTPRequestHandler):
    "An ONVIF camera that records what it receives. Class attributes configure it."
    requests = []
    clock = None  # camera UTC time as a datetime, or None
    profiles = ('<trt:Profiles token="plain"/>'
        '<trt:Profiles token="mobile"><tt:PTZConfiguration token="c"/></trt:Profiles>')
    fault = None  # operation name that answers with a SOAP fault
    device = DEVICE

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length'])).decode()
        type(self).requests.append(body)
        operation = re.search(r'<(?:\w+:)?(\w+)[ >/]', body.split('<s:Body>')[1]).group(1)
        if operation == type(self).fault:
            reply = envelope('<s:Fault><s:Reason><s:Text xml:lang="en">Sender not '
                'authorized</s:Text></s:Reason></s:Fault>')
        else:
            reply = envelope(self.answer(operation))
        self.send_response(200)
        self.send_header('Content-Length', str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def answer(self, operation):
        host, port = self.server.server_address
        if operation == 'GetSystemDateAndTime':
            moment = type(self).clock or datetime.now(timezone.utc)
            return ('<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime><tt:UTCDateTime>'
                f'<tt:Time><tt:Hour>{moment.hour}</tt:Hour><tt:Minute>{moment.minute}</tt:Minute>'
                f'<tt:Second>{moment.second}</tt:Second></tt:Time><tt:Date><tt:Year>{moment.year}'
                f'</tt:Year><tt:Month>{moment.month}</tt:Month><tt:Day>{moment.day}</tt:Day>'
                '</tt:Date></tt:UTCDateTime></tds:SystemDateAndTime>'
                '</tds:GetSystemDateAndTimeResponse>')
        if operation == 'GetDeviceInformation':
            return ('<tds:GetDeviceInformationResponse>' + type(self).device +
                '</tds:GetDeviceInformationResponse>')
        if operation == 'GetCapabilities':
            # The camera announces an internal address: the client must ignore its host.
            service = 'http://10.99.99.99:2020/onvif/service'
            return ('<tds:GetCapabilitiesResponse><tds:Capabilities>'
                f'<tt:Media><tt:XAddr>{service}</tt:XAddr></tt:Media>'
                f'<tt:PTZ><tt:XAddr>{service}</tt:XAddr></tt:PTZ>'
                '</tds:Capabilities></tds:GetCapabilitiesResponse>')
        if operation == 'GetProfiles':
            return f'<trt:GetProfilesResponse>{type(self).profiles}</trt:GetProfilesResponse>'
        if operation == 'GetPresets':
            return ('<tptz:GetPresetsResponse><tptz:Preset token="1"><tt:Name>Door</tt:Name>'
                '</tptz:Preset><tptz:Preset token="2"/></tptz:GetPresetsResponse>')
        return f'<tptz:{operation}Response/>'


class OnvifTestCase(unittest.TestCase):
    def setUp(self):
        FakeCamera.requests = []
        FakeCamera.clock = None
        FakeCamera.fault = None
        FakeCamera.device = DEVICE
        FakeCamera.profiles = ('<trt:Profiles token="plain"/>'
            '<trt:Profiles token="mobile"><tt:PTZConfiguration token="c"/></trt:Profiles>')
        self.server = HTTPServer(('127.0.0.1', 0), FakeCamera)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.camera = onvif.OnvifCamera('127.0.0.1', self.server.server_address[1], 'user', 'secret')

    def operations(self):
        return [re.search(r'<(?:\w+:)?(\w+)[ >/]', body.split('<s:Body>')[1]).group(1)
            for body in FakeCamera.requests]

    def last(self):
        return FakeCamera.requests[-1]


class AxesTest(unittest.TestCase):
    def test_directions_follow_the_picture_not_the_camera_body(self):
        # right on screen, with the picture turned 0, 90, 180 and 270 degrees
        self.assertEqual(onvif.axes_for_rotation(1, 0, 0), (1, 0))
        self.assertEqual(onvif.axes_for_rotation(1, 0, 90), (0, 1))
        self.assertEqual(onvif.axes_for_rotation(1, 0, 180), (-1, 0))
        self.assertEqual(onvif.axes_for_rotation(1, 0, 270), (0, -1))
        # up on screen
        self.assertEqual(onvif.axes_for_rotation(0, 1, 0), (0, 1))
        self.assertEqual(onvif.axes_for_rotation(0, 1, 90), (-1, 0))
        self.assertEqual(onvif.axes_for_rotation(0, 1, 180), (0, -1))
        self.assertEqual(onvif.axes_for_rotation(0, 1, 270), (1, 0))

    def test_opposite_buttons_always_give_opposite_commands(self):
        for rotation in (0, 90, 180, 270):
            for dx, dy in ((1, 0), (0, 1)):
                pan, tilt = onvif.axes_for_rotation(dx, dy, rotation)
                back = onvif.axes_for_rotation(-dx, -dy, rotation)
                self.assertEqual((-pan, -tilt), back)

    def test_an_unknown_rotation_is_rejected(self):
        with self.assertRaises(ValueError):
            onvif.axes_for_rotation(1, 0, 45)


class SignatureTest(OnvifTestCase):
    def test_digest_is_sha1_of_nonce_created_and_password(self):
        FakeCamera.clock = None
        self.camera.stop()
        header = next(b for b in FakeCamera.requests if '<UsernameToken>' in b)
        nonce = base64.b64decode(re.search(r'<Nonce[^>]*>([^<]+)<', header).group(1))
        created = re.search(r'<Created[^>]*>([^<]+)<', header).group(1)
        digest = re.search(r'<Password[^>]*>([^<]+)<', header).group(1)
        expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + b'secret').digest()).decode()
        self.assertEqual(digest, expected)
        self.assertNotIn('secret', header)  # the password itself is never sent

    def test_each_request_uses_a_fresh_nonce(self):
        self.camera.stop()
        self.camera.stop()
        nonces = re.findall(r'<Nonce[^>]*>([^<]+)<', ''.join(FakeCamera.requests))
        self.assertEqual(len(nonces), len(set(nonces)))

    def test_a_skewed_camera_clock_is_followed_so_it_does_not_reject_us(self):
        FakeCamera.clock = datetime.now(timezone.utc) + timedelta(hours=3)
        self.camera.stop()
        header = next(b for b in FakeCamera.requests if '<UsernameToken>' in b)
        created = datetime.strptime(re.search(r'<Created[^>]*>([^<]+)<', header).group(1),
            '%Y-%m-%dT%H:%M:%S.000Z').replace(tzinfo=timezone.utc)
        skew = created - datetime.now(timezone.utc)
        self.assertLess(abs(skew - timedelta(hours=3)), timedelta(seconds=5))


class CommandsTest(OnvifTestCase):
    def test_move_sends_speeds_and_a_whole_second_timeout(self):
        self.camera.move(0.5, -1, seconds=2.0)
        body = self.last()
        self.assertIn('<tptz:Timeout>PT2S</tptz:Timeout>', body)  # the camera drops «PT2.0S»
        self.assertIn('<tt:PanTilt x="0.500" y="-1.000"/>', body)
        self.assertIn('<tptz:ProfileToken>mobile</tptz:ProfileToken>', body)

    def test_the_timeout_is_never_below_one_second(self):
        self.camera.move(1, 0, seconds=0.2)
        self.assertIn('PT1S', self.last())

    def test_speeds_are_clamped(self):
        self.camera.move(7, -9)
        self.assertIn('x="1.000" y="-1.000"', self.last())

    def test_the_profile_that_can_move_is_the_one_used(self):
        self.camera.stop()
        self.assertIn('<tptz:ProfileToken>mobile</tptz:ProfileToken>', self.last())

    def test_stop_halts_pan_tilt_and_zoom(self):
        self.camera.stop()
        self.assertIn('<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>', self.last())

    def test_services_are_reached_through_the_known_host_not_the_announced_one(self):
        self.camera.stop()  # would fail to connect to 10.99.99.99 otherwise
        self.assertIn('Stop', self.operations())

    def test_device_information_is_trimmed_and_asks_only_the_device_service(self):
        self.assertEqual(self.camera.device_info(), {
                'manufacturer': 'tp-link', 'model': 'Tapo TC43',
                'firmware': '1.4.1 Build 260612', 'serial': '746163c2', 'hardware': '2.0'})
        self.assertNotIn('GetCapabilities', self.operations())

    def test_device_information_leaves_out_what_the_camera_does_not_report(self):
        FakeCamera.device = '<tds:Manufacturer>Acme</tds:Manufacturer><tds:Model/>'
        self.assertEqual(self.camera.device_info(), {'manufacturer': 'Acme'})

    def test_a_camera_that_reports_nothing_is_an_error(self):
        FakeCamera.device = ''
        with self.assertRaisesRegex(onvif.OnvifError, 'marca'):
            self.camera.device_info()

    def test_discovery_happens_once(self):
        self.camera.stop()
        self.camera.stop()
        self.assertEqual(self.operations().count('GetCapabilities'), 1)
        self.assertEqual(self.operations().count('GetProfiles'), 1)

    def test_presets_use_the_token_when_a_preset_has_no_name(self):
        self.assertEqual(self.camera.presets(), [('1', 'Door'), ('2', '2')])

    def test_a_refusal_from_the_camera_becomes_an_error_with_its_reason(self):
        FakeCamera.fault = 'ContinuousMove'
        with self.assertRaisesRegex(onvif.OnvifError, 'Sender not authorized'):
            self.camera.move(1, 0)

    def test_an_unreachable_camera_is_an_error_not_a_crash(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaisesRegex(onvif.OnvifError, 'no responde'):
            self.camera.stop()


if __name__ == '__main__':
    unittest.main()
