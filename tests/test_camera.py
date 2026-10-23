# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""A camera always belongs to a location: the managers to call are found through it.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_camera
"""
import unittest

from trytond.model.exceptions import RequiredValidationError
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction

CAMERA = {'name': 'N', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
    'stream_path': 's', 'rotation': '0'}


class CameraTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
