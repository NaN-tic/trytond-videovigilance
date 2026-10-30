# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Events are camera movements and camera/service status changes; people are alarms.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_events_page
"""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from dominate.tags import div
from werkzeug.datastructures import MultiDict

from trytond.model.exceptions import RequiredValidationError
from trytond.modules.voyager.voyager import VoyagerContext
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction
from trytond.transaction import Transaction


class EventsPageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

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

    def render(self, query=()):
        request = SimpleNamespace(args=MultiDict(query), user_id=None)
        context = VoyagerContext(request=request,
            session=SimpleNamespace(session_id='events-page-test'))
        prefix = 'trytond.modules.videovigilance.www.events.'
        with Transaction().set_context(voyager_context=context):
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
        self.assertIn('Cámara movida', html)
        self.assertIn('Cámara sin conexión', html)
        self.assertIn('Servicio de detección sin señal', html)
        self.assertIn('Sin latidos', html)
        self.assertNotIn('persona(s)', html)

    @with_transaction()
    def test_filters_split_movements_from_status_and_a_person_filter_does_not_exist(self):
        camera = self.camera()
        self.event('m1', 'camera_movement', camera)
        self.event('s1', 'camera_online', camera, message='conectada de nuevo')
        movements = self.render([('type', 'movements')])
        self.assertIn('Cámara movida', movements)
        self.assertNotIn('Cámara recuperada', movements)
        status = self.render([('type', 'status')])
        self.assertIn('Cámara recuperada', status)
        self.assertNotIn('Cámara movida </strong>', status)
        self.assertEqual(self.render([('type', 'persons')]), self.render())

    @with_transaction()
    def test_only_service_status_events_may_have_no_camera(self):
        camera = self.camera()
        self.event('ok', 'service_up')
        for type_ in ('camera_offline', 'camera_online', 'camera_movement', 'person_motion'):
            with self.subTest(type=type_), self.assertRaises(RequiredValidationError):
                self.event('no-camera-' + type_, type_)
        self.event('with-camera', 'camera_offline', camera)
