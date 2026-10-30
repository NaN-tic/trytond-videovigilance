# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""A camera always belongs to a location: the managers to call are found through it.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_camera
"""
import unittest

from cryptography.fernet import Fernet
import trytond.config as config
from trytond.model.exceptions import (
    AccessButtonError, AccessError, RequiredValidationError, ValidationError)
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction
from trytond.transaction import Transaction


CAMERA = {'name': 'N', 'code': 'CAM', 'host': 'h', 'rtsp_port': 554,
    'stream_path': 's', 'rotation': '0'}


class CameraTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.had_section = config.has_section('videovigilance')
        if not cls.had_section:
            config.add_section('videovigilance')
        cls.secret_key = config.get('videovigilance', 'secret_key', default='')
        if cls.had_section:
            cls.addClassCleanup(config.set, 'videovigilance', 'secret_key', cls.secret_key)
        else:
            cls.addClassCleanup(config.remove_section, 'videovigilance')
        cls.fernet_key = Fernet.generate_key()
        config.set('videovigilance', 'secret_key', cls.fernet_key.decode())
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

    @with_transaction()
    def test_service_token_rotation_is_location_specific(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        locations = Location.create([{'name': 'One'}, {'name': 'Two'}])
        first, second = locations
        creator = Transaction().user
        Location.generate_service_token(locations)
        first_token, second_token = [
            row['service_token'] for row in Location.read(
                [first.id, second.id], ['service_token'])]
        self.assertTrue(first_token)
        self.assertTrue(second_token)
        old_first_application = Application.check(first_token, 'videovigilance')
        self.assertIsNotNone(old_first_application)
        self.assertEqual(old_first_application.user.id, creator)
        self.assertIsNotNone(Application.check(second_token, 'videovigilance'))
        self.assertNotEqual(old_first_application.key, first_token)
        self.assertEqual(
            Fernet(self.fernet_key).decrypt(
                old_first_application.videovigilance_key_encrypted.encode()).decode(),
            first_token)
        self.assertIsNone(Application.check(old_first_application.key, 'videovigilance'))
        self.assertIsNone(Application.check(
            old_first_application.videovigilance_key_encrypted, 'videovigilance'))

        Location.generate_service_token([first])
        rotated_first_token, = [
            row['service_token'] for row in Location.read([first.id], ['service_token'])]
        self.assertTrue(rotated_first_token)
        self.assertNotEqual(rotated_first_token, first_token)
        self.assertIsNone(Application.check(first_token, 'videovigilance'))
        self.assertIsNotNone(Application.check(second_token, 'videovigilance'))
        self.assertEqual(
            Application.check(rotated_first_token, 'videovigilance').id,
            old_first_application.id)
        rotated_application = Application.check(rotated_first_token, 'videovigilance')
        self.assertEqual(
            Fernet(self.fernet_key).decrypt(
                rotated_application.videovigilance_key_encrypted.encode()).decode(),
            rotated_first_token)

    @with_transaction()
    def test_revoking_a_service_token_cancels_its_application(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        location, = Location.create([{'name': 'One'}])
        Location.generate_service_token([location])
        token, = [row['service_token'] for row in Location.read(
            [location.id], ['service_token'])]
        self.assertIsNotNone(Application.check(token, 'videovigilance'))

        Location.revoke_service_token([location])
        self.assertIsNone(Location.read([location.id], ['service_token'])[0]['service_token'])
        self.assertIsNone(Application.check(token, 'videovigilance'))

    @with_transaction()
    def test_reader_cannot_read_or_manage_service_tokens_but_admin_can(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        reader, = User.create([{
            'name': 'Reader', 'login': 'reader',
            'groups': [('add', [ModelData.get_id('videovigilance', 'group_reader')])]}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id('videovigilance', 'group_admin')])]})
        with Transaction().set_user(reader.id), Transaction().set_context(_check_access=True):
            with self.assertRaises(AccessError):
                Location.read([location.id], ['service_token'])
            with self.assertRaises(AccessButtonError):
                Location.generate_service_token([location])
            with self.assertRaises(AccessButtonError):
                Location.revoke_service_token([location])
        with Transaction().set_user(admin), Transaction().set_context(_check_access=True):
            Location.generate_service_token([location])
            self.assertTrue(Location.read([location.id], ['service_token'])[0]['service_token'])
            Location.revoke_service_token([location])

    @with_transaction()
    def test_only_an_admin_can_associate_a_service_application_with_a_location(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Application = pool.get('res.user.application')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        reader, = User.create([{
                'name': 'Reader', 'login': 'reader-application',
                'groups': [('add', [ModelData.get_id(
                    'videovigilance', 'group_reader')])]}])
        rest_application, = Application.create([{
                'user': reader.id, 'application': 'rest', 'key': 'reader-rest-key'}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id(
                    'videovigilance', 'group_admin')])]})

        with Transaction().set_user(reader.id), Transaction().set_context(
                _check_access=True):
            with self.assertRaises(AccessError):
                Application.create([{
                        'user': reader.id, 'application': 'videovigilance',
                        'key': 'reader-service-key',
                        'videovigilance_location': location.id}])
            with self.assertRaises(AccessError):
                Application.write(
                    [rest_application], {'videovigilance_location': location.id})
            self.assertEqual(
                Application.check('reader-rest-key', 'rest').id,
                rest_application.id)

        with Transaction().set_user(admin), Transaction().set_context(
                _check_access=True):
            Location.generate_service_token([location])
            token = Location.read([location.id], ['service_token'])[0]['service_token']
            service_application = Application.check(token, 'videovigilance')
            self.assertEqual(
                service_application.videovigilance_location.id, location.id)
            self.assertEqual(
                Application.check('reader-rest-key', 'rest').id,
                rest_application.id)

    @with_transaction()
    def test_a_rest_application_cannot_be_assigned_to_a_location(self):
        pool = Pool()
        location, = pool.get('videovigilance.location').create([{'name': 'One'}])
        Application = pool.get('res.user.application')
        application, = Application.create([{
                'user': Transaction().user, 'application': 'rest', 'key': 'rest-key'}])
        with self.assertRaises(ValidationError):
            Application.write([application], {'videovigilance_location': location.id})

    @with_transaction()
    def test_show_token_dialog_is_restricted_and_never_saves_its_secret(self):
        pool = Pool()
        Location = pool.get('videovigilance.location')
        Wizard = pool.get('videovigilance.location.show_service_token', type='wizard')
        User = pool.get('res.user')
        ModelData = pool.get('ir.model.data')
        location, = Location.create([{'name': 'One'}])
        Location.generate_service_token([location])
        token = Location.read([location.id], ['service_token'])[0]['service_token']
        reader, = User.create([{
                    'name': 'Reader', 'login': 'reader-token',
                    'groups': [('add', [ModelData.get_id('videovigilance', 'group_reader')])]}])
        admin = Transaction().user
        User.write([User(admin)], {
                'groups': [('add', [ModelData.get_id('videovigilance', 'group_admin')])]})
        context = {'active_model': Location.__name__, 'active_id': location.id,
            'active_ids': [location.id], '_check_access': True}
        with Transaction().set_user(reader.id), Transaction().set_context(context):
            with self.assertRaises(AccessError):
                Wizard.create()
        with Transaction().set_user(admin), Transaction().set_context(context):
            session_id, _, _ = Wizard.create()
            view = Wizard.execute(session_id, {}, 'start')['view']
            self.assertEqual(view['defaults']['token'], token)
            Wizard.execute(session_id, {'start': {'token': token}}, 'end')
            session = pool.get('ir.session.wizard')(session_id)
            self.assertNotIn(token, session.data)
            Wizard.delete(session_id)

    @with_transaction()
    def test_plaintext_service_keys_are_migrated_without_changing_device_credentials(self):
        Application = Pool().get('res.user.application')
        token = 'existing-device-credential'
        application, = Application.create([{
                    'user': Transaction().user, 'application': 'videovigilance', 'key': token}])
        table = Application.__table__()
        cursor = Transaction().connection.cursor()
        # Simulate the row left by the version which stored the key in plaintext.
        cursor.execute(*table.update(
                [table.key, table.videovigilance_key_encrypted], [token, None],
                where=table.id == application.id))
        Application._encrypt_service_keys()
        cursor.execute(*table.select(table.key, table.videovigilance_key_encrypted,
                where=table.id == application.id))
        fingerprint, encrypted = cursor.fetchone()
        self.assertNotEqual(fingerprint, token)
        self.assertEqual(Fernet(self.fernet_key).decrypt(encrypted.encode()).decode(), token)
        self.assertEqual(Application.check(token, 'videovigilance').id, application.id)
        Application._encrypt_service_keys()
        cursor.execute(*table.select(table.videovigilance_key_encrypted,
                where=table.id == application.id))
        self.assertEqual(cursor.fetchone()[0], encrypted)

    @with_transaction()
    def test_other_application_keys_keep_their_normal_authentication(self):
        Application = Pool().get('res.user.application')
        application, = Application.create([{
                    'user': Transaction().user, 'application': 'rest', 'key': 'rest-key'}])
        self.assertEqual(application.key, 'rest-key')
        self.assertIsNone(application.videovigilance_key_encrypted)
        self.assertEqual(Application.check('rest-key', 'rest').id, application.id)
        Application.write([application], {'key': 'replacement-rest-key'})
        self.assertIsNone(Application.check('rest-key', 'rest'))
        self.assertEqual(Application.check('replacement-rest-key', 'rest').id, application.id)
