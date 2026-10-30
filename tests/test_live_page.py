# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Selection of an active camera by the live page's query string."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dominate.tags import div
from werkzeug.datastructures import MultiDict

from trytond.modules.voyager.voyager import VoyagerContext
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction
from trytond.transaction import Transaction


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
    def test_a_code_for_an_inactive_camera_falls_back_and_no_active_camera_is_reported(self):
        first = self.camera('First', 'FIRST')
        inactive = self.camera('Inactive', 'INACTIVE', active=False)
        html = self.render([('code', inactive.code)])
        self.assertIn('live_frame/FIRST', html)
        self.assertNotIn('live_frame/INACTIVE', html)

        Pool().get('videovigilance.camera').write([first], {'active': False})
        html = self.render([('code', 'INACTIVE')])
        self.assertIn('No hay ninguna cámara activa.', html)
        self.assertNotIn('id="live"', html)
