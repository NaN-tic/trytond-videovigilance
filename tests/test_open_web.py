# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""The Tryton menu entry that opens the web app.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_open_web
"""
import unittest

from trytond.exceptions import UserError
from trytond.modules.company.tests import create_company, set_company
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction


class OpenWebTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def open(self):
        Wizard = Pool().get('videovigilance.open_web', type='wizard')
        session_id, _, _ = Wizard.create()
        return Wizard.execute(session_id, {}, 'open')

    @with_transaction()
    def test_without_a_web_app_it_says_so_instead_of_opening_a_blank_page(self):
        with self.assertRaises(UserError):
            self.open()

    @with_transaction()
    def test_it_opens_the_address_of_the_web_app(self):
        company = create_company()
        with set_company(company):
            Pool().get('notification.web.application').create([{
                        'name': 'Avisos', 'code': 'videovigilance', 'company': company.id,
                        'origin': 'https://tryton.example', 'base_path': '/cameras/avisos/'}])
        action, = self.open()['actions']
        self.assertEqual(action[0]['url'], 'https://tryton.example/cameras/avisos/')
