"""Tests for the live relay: Tryton and the camera are faked, the capture child is a script."""
import json
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from service import live, onvif
from service.tryton import CameraConfig, TrytonUnavailable


def camera(**changes):
    values = dict(id=1, code='CAM', host='192.168.2.5', rtsp_port=554, stream_path='stream1',
        rotation=0, username='user', password='pw', secondary_stream_path='stream2',
        ptz=True, onvif_enabled=True, onvif_port=2020)
    values.update(changes)
    return CameraConfig(**values)


def wait_for(check, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def capture_script(body):
    "A stand-in for live_capture.py: reads the settings line, then runs `body`."
    folder = tempfile.mkdtemp()
    script = Path(folder) / 'capture.py'
    script.write_text(textwrap.dedent('''
        import json, struct, sys, time
        settings = json.loads(sys.stdin.readline())
        def frame(data):
            sys.stdout.buffer.write(struct.pack('>I', len(data)) + data)
            sys.stdout.buffer.flush()
    ''') + textwrap.dedent(body))
    return script


class FakeRelay:
    "Tryton's side of the relay: records what the service sends and answers as told."

    def __init__(self):
        self.lock = threading.Lock()
        self.frames = []
        self.reports = []
        self.wanted = set()
        self.commands = {}
        self.down = False

    def poll(self, reports):
        with self.lock:
            if self.down:
                raise TrytonUnavailable('ConnectionError')
            self.reports.append(reports)
            answer = {cid: {'wanted': cid in self.wanted, 'commands': self.commands.pop(cid, [])}
                for cid in reports}
            return answer

    def push(self, camera_id, jpeg):
        with self.lock:
            if self.down:
                raise TrytonUnavailable('ConnectionError')
            self.frames.append((camera_id, jpeg))
            return camera_id in self.wanted


class FakeOnvif:
    instances = []

    def __init__(self, host, port, username, password, timeout=6):
        self.args = (host, port, username, password)
        self.calls = []
        self.delay = 0
        self.fail = None
        type(self).instances.append(self)

    def move(self, pan, tilt, seconds=None):
        time.sleep(self.delay)
        if self.fail:
            raise onvif.OnvifError(self.fail)
        self.calls.append(('move', pan, tilt, seconds))

    def stop(self):
        time.sleep(self.delay)
        if self.fail:
            raise onvif.OnvifError(self.fail)
        self.calls.append(('stop',))


class RelayTestCase(unittest.TestCase):
    def build(self, script_body='time.sleep(30)', cameras=(), ptz_factory=FakeOnvif):
        FakeOnvif.instances = []
        self.relay = FakeRelay()
        self.stop = threading.Event()
        self.live = live.LiveRelay(lambda: self.relay, 5.0, self.stop,
            script=capture_script(script_body), ptz_factory=ptz_factory)
        self.live.set_cameras(cameras or [camera()])
        self.addCleanup(self.live.close)
        self.addCleanup(self.stop.set)
        return self.live

    def poll(self):
        self.live.step()


class StreamingTest(RelayTestCase):
    def test_nothing_is_captured_or_sent_while_nobody_watches(self):
        self.build()
        self.poll()
        self.assertEqual(self.live._streams, {})
        self.assertEqual(self.relay.frames, [])

    def test_a_watched_camera_streams_its_secondary_stream_and_credentials_only_go_on_stdin(self):
        self.build('''
            frame(json.dumps({'url': settings['url'], 'rotation': settings['rotation'],
                'fps': settings['fps']}).encode())
            time.sleep(30)
        ''', cameras=[camera(rotation=180)])
        self.relay.wanted = {1}
        self.poll()
        self.assertTrue(wait_for(lambda: self.relay.frames))
        pushed = json.loads(self.relay.frames[0][1])
        self.assertEqual(pushed['url'], 'rtsp://user:pw@192.168.2.5:554/stream2')
        self.assertEqual((pushed['rotation'], pushed['fps']), (180, 5.0))
        self.assertEqual(self.relay.frames[0][0], 1)
        self.assertNotIn('pw', ' '.join(self.live._streams[1].capture._process.args))

    def test_the_main_stream_is_used_when_there_is_no_secondary_one(self):
        self.build('''
            frame(settings['url'].encode())
            time.sleep(30)
        ''', cameras=[camera(secondary_stream_path=None)])
        self.relay.wanted = {1}
        self.poll()
        self.assertTrue(wait_for(lambda: self.relay.frames))
        self.assertTrue(self.relay.frames[0][1].endswith(b'/stream1'))

    def test_the_capture_stops_when_nobody_watches_any_more(self):
        self.build()
        self.relay.wanted = {1}
        self.poll()
        process = self.live._streams[1].capture._process
        self.relay.wanted = set()
        self.poll()
        self.assertEqual(self.live._streams, {})
        self.assertTrue(wait_for(lambda: process.poll() is not None))

    def test_a_slow_connection_drops_frames_instead_of_falling_behind(self):
        self.build('''
            for n in range(1, 31):
                frame(str(n).encode())
                time.sleep(0.02)
            time.sleep(30)
        ''')
        original = self.relay.push

        def slow(camera_id, jpeg):
            time.sleep(0.25)
            return original(camera_id, jpeg)
        self.relay.push = slow
        self.relay.wanted = {1}
        self.poll()
        self.assertTrue(wait_for(lambda: self.relay.frames and self.relay.frames[-1][1] == b'30'))
        self.assertLess(len(self.relay.frames), 10)  # most of the 30 were skipped, the last arrived

    def test_a_capture_that_fails_is_reported_and_retried_later_not_in_a_storm(self):
        self.build('sys.exit(2)')
        live.RETRY_S, original = 0.4, live.RETRY_S
        self.addCleanup(setattr, live, 'RETRY_S', original)
        self.relay.wanted = {1}
        self.poll()
        first = self.live._streams[1]
        self.assertTrue(wait_for(lambda: first.error))
        self.poll()
        self.assertEqual(self.relay.reports[-1][1]['error'], 'no se puede abrir el vídeo de la cámara')
        self.assertIs(self.live._streams[1], first)  # too soon to try again
        time.sleep(0.5)
        self.poll()
        self.assertIsNot(self.live._streams[1], first)

    def test_a_changed_stream_or_rotation_restarts_the_capture(self):
        self.build()
        self.relay.wanted = {1}
        self.poll()
        first = self.live._streams[1]
        self.live.set_cameras([camera(rotation=90)])
        self.poll()
        self.assertIsNot(self.live._streams[1], first)
        self.assertTrue(wait_for(lambda: first.capture._process.poll() is not None))

    def test_a_camera_removed_in_tryton_stops_streaming(self):
        self.build()
        self.relay.wanted = {1}
        self.poll()
        self.live.set_cameras([])
        self.poll()
        self.assertEqual(self.live._streams, {})

    def test_when_tryton_is_unreachable_for_too_long_every_capture_stops(self):
        self.build()
        live.OFFLINE_S, original = 0.2, live.OFFLINE_S
        self.addCleanup(setattr, live, 'OFFLINE_S', original)
        self.relay.wanted = {1}
        self.poll()
        self.relay.down = True
        self.poll()
        self.assertIn(1, self.live._streams)  # a short outage keeps the picture
        time.sleep(0.3)
        self.poll()
        self.assertEqual(self.live._streams, {})

    def test_polling_is_slow_when_idle_fast_when_watched_and_backs_off_on_failure(self):
        self.build()
        self.poll()
        self.assertEqual(self.live.interval(), live.POLL_IDLE_S)
        self.relay.wanted = {1}
        self.poll()
        self.assertEqual(self.live.interval(), live.POLL_BUSY_S)
        self.relay.down = True
        self.poll()
        first = self.live.interval()
        self.poll()
        self.assertGreater(self.live.interval(), first)
        self.assertLessEqual(self.live.interval(), 10.0)


class MovementTest(RelayTestCase):
    def test_a_move_command_goes_to_the_camera_as_seen_on_screen_with_a_timeout(self):
        self.build(cameras=[camera(rotation=180)])
        self.relay.commands[1] = [{'op': 'move', 'dx': 1, 'dy': 0, 'speed': 0.5, 'seconds': 2.0}]
        self.poll()
        self.poll()  # the commands are carried out on their own thread
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and FakeOnvif.instances[0].calls))
        self.assertEqual(FakeOnvif.instances[0].args, ('192.168.2.5', 2020, 'user', 'pw'))
        # right on screen with the picture turned 180° is the camera's left
        self.assertEqual(FakeOnvif.instances[0].calls[0], ('move', -0.5, -0.0, 2.0))

    def test_stop_halts_the_camera(self):
        self.build()
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and FakeOnvif.instances[0].calls))
        self.assertEqual(FakeOnvif.instances[0].calls, [('stop',)])

    def test_a_command_can_never_make_the_camera_run_without_a_timeout_or_too_fast(self):
        self.build()
        self.relay.commands[1] = [
            {'op': 'move', 'dx': 9, 'dy': -9, 'speed': 50, 'seconds': 9999}]
        self.poll()
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and FakeOnvif.instances[0].calls))
        self.assertEqual(FakeOnvif.instances[0].calls[0], ('move', 1.0, -1.0, 5.0))

    def test_only_the_newest_pending_command_is_carried_out_when_the_camera_is_slow(self):
        self.build()
        original = FakeOnvif.move
        gate = threading.Event()

        def slow_first(self, pan, tilt, seconds=None):
            if not self.calls:
                gate.wait(5)
            original(self, pan, tilt, seconds)
        FakeOnvif.move = slow_first
        self.addCleanup(setattr, FakeOnvif, 'move', original)
        for dx in (1, -1, 0):
            self.relay.commands[1] = [{'op': 'move', 'dx': dx, 'dy': 1, 'speed': 1, 'seconds': 2}]
            self.poll()
            time.sleep(0.05)
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        gate.set()
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and ('stop',) in FakeOnvif.instances[0].calls))
        kinds = [call[0] for call in FakeOnvif.instances[0].calls]
        self.assertEqual(kinds[-1], 'stop')
        self.assertLess(len(kinds), 4)  # the moves queued behind the slow one were skipped

    def test_a_refusal_from_the_camera_is_reported_once_to_tryton(self):
        class Refusing(FakeOnvif):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.fail = 'Sender not authorized'
        self.build(ptz_factory=Refusing)
        self.relay.commands[1] = [{'op': 'move', 'dx': 1, 'dy': 0, 'speed': 1, 'seconds': 2}]
        self.poll()
        self.assertTrue(wait_for(lambda: self.live._ptz[1]._message))
        self.poll()
        self.assertEqual(self.relay.reports[-1][1]['message'],
            'La cámara no obedece: Sender not authorized')
        self.poll()
        self.assertIsNone(self.relay.reports[-1][1]['message'])

    def test_a_camera_that_cannot_move_says_so(self):
        self.build(cameras=[camera(ptz=False)])
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        self.assertTrue(wait_for(lambda: self.live._ptz[1]._message))
        self.poll()
        self.assertIn('no se puede mover', self.relay.reports[-1][1]['message'])
        self.assertEqual(FakeOnvif.instances, [])

    def test_a_malformed_command_is_reported_and_does_not_kill_the_thread(self):
        self.build()
        self.relay.commands[1] = [{'op': 'dance'}]
        self.poll()
        self.assertTrue(wait_for(lambda: self.live._ptz[1]._message))
        self.live._ptz[1].take_message()
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and FakeOnvif.instances[0].calls))

    def test_new_credentials_in_tryton_are_used_by_the_next_command(self):
        self.build()
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        self.assertTrue(wait_for(lambda: FakeOnvif.instances and FakeOnvif.instances[0].calls))
        self.live.set_cameras([camera(password='new')])
        self.relay.commands[1] = [{'op': 'stop'}]
        self.poll()
        self.assertTrue(wait_for(lambda: len(FakeOnvif.instances) == 2))
        self.assertEqual(FakeOnvif.instances[1].args[3], 'new')


class CameraConfigTest(unittest.TestCase):
    def test_live_settings_do_not_count_as_a_change_of_the_detection_camera(self):
        self.assertEqual(camera(), camera(secondary_stream_path='other', ptz=False, onvif_port=80))
        self.assertNotEqual(camera(), camera(stream_path='stream9'))

    def test_the_live_address_uses_the_secondary_stream_and_falls_back_to_the_main_one(self):
        self.assertEqual(camera().live_url, 'rtsp://user:pw@192.168.2.5:554/stream2')
        self.assertEqual(camera(secondary_stream_path=None).live_url, camera().url)

    def test_a_camera_moves_only_with_ptz_onvif_and_complete_credentials(self):
        self.assertTrue(camera().can_move)
        for changes in ({'ptz': False}, {'onvif_enabled': False}, {'username': None},
                {'password': None}):
            with self.subTest(changes=changes):
                self.assertFalse(camera(**changes).can_move)


if __name__ == '__main__':
    unittest.main()
