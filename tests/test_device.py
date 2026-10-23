# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""What a camera reports about itself over ONVIF (heartbeat `device`) is stored on the camera.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_device
"""
import unittest
from datetime import datetime

from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction

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
