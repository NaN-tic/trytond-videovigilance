import unittest
from pathlib import Path

from service.config import ROOT, Settings

BASE = {'TRYTON_USER': 'service', 'TRYTON_PASSWORD': 'secret'}


class SettingsTest(unittest.TestCase):
    def test_defaults_are_the_raspberry_production_values(self):
        settings = Settings.load(BASE)
        self.assertEqual(settings.model, ROOT / 'models/yolox_s_int8_pct.onnx')
        self.assertEqual((settings.confidence, settings.fps, settings.workers, settings.threads), (0.5, 3.0, 1, 3))
        self.assertEqual(settings.outbox, ROOT / 'var/outbox.db')

    def test_relative_paths_resolve_against_the_service_folder_not_the_working_directory(self):
        settings = Settings.load({**BASE, 'DETECTION_MODEL': 'models/other.onnx', 'OUTBOX_PATH': 'var/x.db'})
        self.assertEqual(settings.model, ROOT / 'models/other.onnx')
        self.assertEqual(settings.outbox, ROOT / 'var/x.db')

    def test_absolute_paths_are_kept(self):
        settings = Settings.load({**BASE, 'DETECTION_MODEL': '/tmp/model.onnx'})
        self.assertEqual(settings.model, Path('/tmp/model.onnx'))

    def test_tryton_credentials_are_required(self):
        for missing in ('TRYTON_USER', 'TRYTON_PASSWORD'):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                Settings.load({k: v for k, v in BASE.items() if k != missing})

    def test_invalid_values_are_rejected_with_their_name(self):
        for name, value in [('DETECTION_FPS', '0'), ('DETECTION_FPS', '31'), ('DETECTION_THREADS', '0'),
                ('DETECTION_WORKERS', '0'), ('CAMERA_REFRESH_S', '0.5'), ('DETECTION_FPS', 'rápido')]:
            with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, 'DETECTION_|CAMERA_'):
                Settings.load({**BASE, name: value})

    def test_the_relay_routes_are_derived_from_the_rpc_address(self):
        self.assertEqual(Settings.load(BASE).relay_url, 'http://127.0.0.1:8010/cameras/avisos/relay')
        settings = Settings.load({**BASE, 'TRYTON_URL': 'https://tryton.example.org/prod/rpc/'})
        self.assertEqual(settings.relay_url, 'https://tryton.example.org/prod/avisos/relay')

    def test_an_address_that_is_not_rpc_needs_the_relay_address_given_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'RELAY_URL'):
            Settings.load({**BASE, 'TRYTON_URL': 'http://tryton.lan:8010/'})
        settings = Settings.load({**BASE, 'TRYTON_URL': 'http://tryton.lan:8010/',
            'RELAY_URL': 'http://tryton.lan:8010/cameras/avisos/relay'})
        self.assertEqual(settings.relay_url, 'http://tryton.lan:8010/cameras/avisos/relay')

    def test_the_live_frame_rate_is_bounded(self):
        for value in ('0', '16', 'rápido'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'LIVE_FPS'):
                Settings.load({**BASE, 'LIVE_FPS': value})


if __name__ == '__main__':
    unittest.main()
