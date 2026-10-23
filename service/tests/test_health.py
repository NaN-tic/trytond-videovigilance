import socket
import threading
import unittest
from types import SimpleNamespace

from service.health import (
    DEVICE_INFO_RETRY_S, DEVICE_INFO_TTL_S, CameraHealth, CameraMonitor, HealthSettings,
    HeartbeatSender, rtsp_probe)
from service.onvif import OnvifError
from service.tryton import TrytonUnavailable


def camera(code='CAM', host='127.0.0.1', port=554):
    return SimpleNamespace(code=code, host=host, rtsp_port=port)


def serve_once(reply):
    "A one-shot TCP server that answers whatever it receives with `reply`. Returns its port."
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)

    def answer():
        with server:
            connection, _ = server.accept()
            with connection:
                connection.recv(1024)
                connection.sendall(reply)

    threading.Thread(target=answer, daemon=True).start()
    return server.getsockname()[1]


class SettingsTest(unittest.TestCase):
    def test_tryton_values_are_adopted_and_bad_ones_ignored(self):
        settings = HealthSettings()
        settings.update({'camera_interval': 60, 'camera_failures': '2', 'service_interval': 'soon'})
        self.assertEqual(
            (settings.camera_interval, settings.camera_failures, settings.service_interval),
            (60.0, 2, 5.0))

    def test_values_below_the_minimum_are_raised(self):
        settings = HealthSettings()
        settings.camera_interval, settings.camera_failures, settings.service_interval = 30.0, 3, 30.0
        settings.update({'camera_interval': 1, 'camera_failures': 0, 'service_interval': 2})
        self.assertEqual(
            (settings.camera_interval, settings.camera_failures, settings.service_interval),
            (5.0, 1, 5.0))


class ProbeTest(unittest.TestCase):
    def test_login_required_still_counts_as_alive(self):
        port = serve_once(b'RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n')
        self.assertEqual(rtsp_probe('127.0.0.1', port, timeout=2), (True, ''))

    def test_something_that_is_not_rtsp_is_not_a_camera(self):
        port = serve_once(b'HTTP/1.1 200 OK\r\n\r\n')
        ok, detail = rtsp_probe('127.0.0.1', port, timeout=2)
        self.assertFalse(ok)
        self.assertIn('no es RTSP', detail)

    def test_nobody_listening_is_offline(self):
        with socket.socket() as free:
            free.bind(('127.0.0.1', 0))
            port = free.getsockname()[1]
        ok, detail = rtsp_probe('127.0.0.1', port, timeout=2)
        self.assertFalse(ok)
        self.assertIn('no se puede conectar', detail)


class HysteresisTest(unittest.TestCase):
    def test_unknown_until_decided_then_offline_only_after_consecutive_failures(self):
        health = CameraHealth()
        health.record(False, 'caída', failures_needed=3)
        health.record(False, 'caída', failures_needed=3)
        self.assertIsNone(health.online)
        health.record(False, 'caída', failures_needed=3)
        self.assertIs(health.online, False)
        self.assertEqual(health.detail, 'caída')

    def test_one_isolated_failure_does_not_take_an_online_camera_down(self):
        health = CameraHealth()
        health.record(True, '', 3)
        health.record(False, 'corte', 3)
        health.record(True, '', 3)
        health.record(False, 'corte', 3)
        health.record(False, 'corte', 3)
        self.assertIs(health.online, True)

    def test_one_good_check_brings_it_back(self):
        health = CameraHealth()
        for _ in range(3):
            health.record(False, 'caída', 3)
        health.record(True, '', 3)
        self.assertIs(health.online, True)
        self.assertEqual(health.detail, '')


class MonitorTest(unittest.TestCase):
    def monitor(self, cameras, probe, failures=2, **kwargs):
        settings = HealthSettings()
        settings.camera_failures = failures
        return CameraMonitor(lambda: cameras, settings, threading.Event(), probe=probe, **kwargs)

    def test_responding_camera_without_video_is_offline_with_that_reason(self):
        monitor = self.monitor([(camera(), False, lambda: None)], lambda host, port: (True, ''))
        monitor.step()
        self.assertEqual(monitor.reports(), [])  # still undecided after one failure
        monitor.step()
        report, = monitor.reports()
        self.assertEqual((report['code'], report['online']), ('CAM', False))
        self.assertIn('no entrega vídeo', report['detail'])

    def test_unreachable_camera_reports_why(self):
        monitor = self.monitor([(camera(), True, lambda: None)], lambda host, port: (False, 'sin respuesta'), failures=1)
        monitor.step()
        self.assertEqual(monitor.reports(), [{'code': 'CAM', 'online': False, 'detail': 'sin respuesta'}])

    def test_online_camera_reports_its_latest_frame_taken_at_report_time(self):
        frames = [b'\xff\xd8one', b'\xff\xd8two']
        monitor = self.monitor([(camera(), True, lambda: frames.pop(0))], lambda host, port: (True, ''))
        monitor.step()
        self.assertEqual(monitor.reports()[0]['frame'], b'\xff\xd8one')
        self.assertEqual(monitor.reports()[0]['frame'], b'\xff\xd8two')

    def test_camera_without_a_frame_or_offline_reports_no_frame(self):
        monitor = self.monitor([(camera('A'), True, lambda: None), (camera('B'), False, lambda: b'\xff\xd8old')],
            lambda host, port: (True, ''), failures=1)
        monitor.step()
        self.assertEqual(
            {r['code']: 'frame' in r for r in monitor.reports()}, {'A': False, 'B': False})

    def test_camera_removed_from_tryton_stops_being_reported(self):
        cameras = [(camera('A'), True, lambda: None), (camera('B'), True, lambda: None)]
        monitor = self.monitor(cameras, lambda host, port: (True, ''))
        monitor.step()
        self.assertEqual({r['code'] for r in monitor.reports()}, {'A', 'B'})
        cameras.pop()
        monitor.step()
        self.assertEqual({r['code'] for r in monitor.reports()}, {'A'})

    def onvif_camera(self, code='CAM', **values):
        return SimpleNamespace(code=code, host='h', rtsp_port=554, onvif_enabled=True,
            onvif_port=2020, username='user', password='secret', **values)

    def test_brand_and_model_are_reported_and_read_once_until_they_are_due_again(self):
        reads, now = [], [0.0]
        info = {'manufacturer': 'tp-link', 'model': 'Tapo TC43'}
        monitor = self.monitor(
            [(self.onvif_camera(), True, lambda: None)], lambda host, port: (True, ''),
            device_info=lambda config: reads.append(config.code) or dict(info),
            clock=lambda: now[0])
        monitor.step()
        monitor.step()
        self.assertEqual(reads, ['CAM'])
        self.assertEqual(monitor.reports()[0]['device'], info)
        now[0] = DEVICE_INFO_TTL_S + 1  # the firmware may have changed
        monitor.step()
        self.assertEqual(reads, ['CAM', 'CAM'])

    def test_a_failed_read_is_retried_later_and_keeps_what_was_known(self):
        results, now = [{'model': 'M'}, OnvifError('Authority failure'), {'model': 'M2'}], [0.0]

        def read(config):
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        monitor = self.monitor(
            [(self.onvif_camera(), True, lambda: None)], lambda host, port: (True, ''),
            device_info=read, clock=lambda: now[0])
        monitor.step()
        now[0] = DEVICE_INFO_TTL_S + 1
        monitor.step()  # fails
        self.assertEqual(monitor.reports()[0]['device'], {'model': 'M'})
        now[0] += DEVICE_INFO_RETRY_S / 2
        monitor.step()  # not yet
        self.assertEqual(len(results), 1)
        now[0] += DEVICE_INFO_RETRY_S
        monitor.step()
        self.assertEqual(monitor.reports()[0]['device'], {'model': 'M2'})

    def test_cameras_without_onvif_or_login_or_connection_are_not_asked(self):
        asked = []
        monitor = self.monitor([
                (camera('PLAIN'), True, lambda: None),
                (self.onvif_camera('NOLOGIN', ), True, lambda: None)],
            lambda host, port: (True, ''), device_info=lambda c: asked.append(c.code) or {'model': 'M'})
        monitor._cameras()[1][0].password = None
        monitor.step()
        self.assertEqual(asked, [])
        down = self.monitor([(self.onvif_camera(), True, lambda: None)],
            lambda host, port: (False, 'sin respuesta'), failures=1,
            device_info=lambda c: asked.append(c.code) or {'model': 'M'})
        down.step()
        self.assertEqual(asked, [])
        self.assertNotIn('device', down.reports()[0])


class SenderTest(unittest.TestCase):
    class Client:
        def __init__(self, replies):
            self.replies = list(replies)
            self.sent = []

        def heartbeat(self, cameras):
            self.sent.append(cameras)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    def test_failure_is_flagged_then_recovery_adopts_tryton_intervals(self):
        settings = HealthSettings()
        monitor = SimpleNamespace(reports=lambda: [{'code': 'CAM', 'online': True, 'detail': ''}])
        client = self.Client([TrytonUnavailable('ConnectionError'), {'service_interval': 45}])
        sender = HeartbeatSender(client, monitor, settings, threading.Event())
        sender.step()
        self.assertTrue(sender.failing)
        self.assertEqual(settings.service_interval, 5.0)
        sender.step()
        self.assertFalse(sender.failing)
        self.assertEqual(settings.service_interval, 45.0)
        self.assertEqual(client.sent, [[{'code': 'CAM', 'online': True, 'detail': ''}]] * 2)


if __name__ == '__main__':
    unittest.main()
