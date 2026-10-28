# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Who the web app lets in: a Tryton user with a group and an email, or a web account.

Run with the module installed:  python -m unittest trytond.modules.videovigilance.tests.test_session
"""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from trytond.modules.company.tests import create_company, set_company
from trytond.modules.videovigilance.push import (
    authenticate_web_user, display_name, is_admin)
from trytond.modules.videovigilance.voyager import (
    SITE_TYPE, access_problem, csrf, csrf_ok, current_user, rotate_session, tryton_user)
from trytond.modules.voyager.voyager import VoyagerContext
from trytond.pool import Pool
from trytond.tests.test_tryton import activate_module, with_transaction
from trytond.transaction import Transaction

PASSWORD = 'Clave-de-prueba-9'


class SessionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        activate_module('videovigilance')

    def user(self, group='group_reader', **values):
        pool = Pool()
        groups = [pool.get('ir.model.data').get_id('videovigilance', group)] if group else []
        user, = pool.get('res.user').create([{
                    'name': 'Ana', 'login': 'ana', 'email': 'ana@gmail.com',
                    'groups': [('add', groups)], **values}])
        return user

    def account(self, email='cuenta@gmail.com', password=PASSWORD, **values):
        return Pool().get('web.user').create([{
                    'email': email, 'password': password, **values}])[0]

    def browser(self, account=None, session_id='browser-session'):
        "The Voyager session of the browser, with the web account logged in if any."
        pool = Pool()
        Site, Session = pool.get('www.site'), pool.get('www.session')
        sites = Site.search([('type', '=', SITE_TYPE)], limit=1)
        site = sites[0] if sites else Site.create([
                {'name': 'avisos', 'type': SITE_TYPE, 'url': 'http://x'}])[0]
        session, = Session.create([{
                    'site': site.id, 'session_id': session_id,
                    'user': account.id if account else None,
                    'expiration_date': datetime.now() + timedelta(days=1)}])
        return session

    def serving(self, user=None, token='tryton-token', account=None,
            session_id='browser-session'):
        """The context of a request that reached the web app: with the Tryton session of
        `user`, and/or from a browser where `account` is logged in."""
        request = SimpleNamespace(
            user_id=user.id if user else None,
            session=SimpleNamespace(token=token) if user and token else None)
        return Transaction().set_context(voyager_context=VoyagerContext(
                request=request, session=self.browser(account, session_id)))

    @with_transaction()
    def test_the_user_of_the_tryton_session_gets_in_with_either_group(self):
        for group in ('group_reader', 'group_admin'):
            user = self.user(group, login=group)
            with self.serving(user):
                self.assertEqual(current_user(), user)

    @with_transaction()
    def test_without_any_session_there_is_no_user_and_the_page_asks_to_log_in(self):
        with self.serving():
            self.assertIsNone(tryton_user())
            self.assertIsNone(current_user())
            self.assertEqual(access_problem(tryton_user())[0], 401)

    @with_transaction()
    def test_a_tryton_user_outside_the_groups_is_refused(self):
        user = self.user(group=None)
        with self.serving(user):
            self.assertIsNone(current_user())
            self.assertEqual(access_problem(tryton_user())[0], 403)

    @with_transaction()
    def test_a_user_without_email_is_refused_because_notices_are_addressed_to_it(self):
        for number, email in enumerate((None, '', '   ')):
            user = self.user(login='mail-%d' % number, email=email)
            with self.serving(user):
                self.assertIsNone(current_user(), repr(email))
                self.assertEqual(access_problem(tryton_user())[0], 403)

    @with_transaction()
    def test_a_web_account_logged_in_gets_in_but_never_as_an_administrator(self):
        account = self.account()
        with self.serving(account=account):
            self.assertEqual(current_user(), account)
            self.assertFalse(is_admin(account))
            self.assertEqual(display_name(account), 'cuenta@gmail.com')

    @with_transaction()
    def test_a_web_account_that_lost_its_password_or_was_deactivated_is_out(self):
        WebUser = Pool().get('web.user')
        account = self.account()
        with self.serving(account=account, session_id='one'):
            self.assertEqual(current_user(), account)
        WebUser.write([account], {'password': None})
        with self.serving(account=account, session_id='two'):
            self.assertIsNone(current_user())
        WebUser.write([account], {'password': PASSWORD, 'active': False})
        with self.serving(account=account, session_id='three'):
            self.assertIsNone(current_user())

    @with_transaction()
    def test_the_tryton_session_wins_over_the_web_account(self):
        user, account = self.user(), self.account()
        with self.serving(user, account=account):
            self.assertEqual(current_user(), user)
        # Without the group the Tryton session is of no use, and the web account still is.
        outsider = self.user(group=None, login='outsider')
        with self.serving(outsider, account=account, session_id='other'):
            self.assertEqual(current_user(), account)

    @with_transaction()
    def test_the_csrf_token_of_a_tryton_session_only_works_for_that_session(self):
        user = self.user()
        with self.serving(user, token='first-session'):
            token = csrf()
            self.assertTrue(csrf_ok(token))
        # Logging out in Tryton and in again is a new token: the old form is worthless.
        with self.serving(user, token='second-session'):
            self.assertFalse(csrf_ok(token))

    @with_transaction()
    def test_without_a_tryton_session_the_csrf_token_is_bound_to_the_browser(self):
        with self.serving(session_id='browser-a'):
            token = csrf()
            self.assertTrue(csrf_ok(token))
        with self.serving(session_id='browser-b'):
            self.assertFalse(csrf_ok(token))
            for value in ('', None):
                self.assertFalse(csrf_ok(value))

    @with_transaction()
    def test_the_csrf_token_follows_the_tryton_session_while_there_is_one(self):
        user, account = self.user(), self.account()
        with self.serving(user, token='tryton', account=account, session_id='both'):
            under_tryton = csrf()
        # The Tryton session ends and the browser keeps its web account: the form made under
        # the Tryton session is worthless and the new one is bound to the browser.
        with self.serving(account=account, session_id='both'):
            self.assertFalse(csrf_ok(under_tryton))
            self.assertTrue(csrf_ok(csrf()))

    @with_transaction()
    def test_logging_in_and_out_a_web_account_changes_the_session_id(self):
        account = self.account()
        session = self.browser(session_id='fixed-by-an-attacker')
        with Transaction().set_context(voyager_context=VoyagerContext(
                request=SimpleNamespace(user_id=None, session=None), session=session)):
            rotate_session(account)
            self.assertEqual(session.user, account)
            self.assertNotEqual(session.session_id, 'fixed-by-an-attacker')
            logged = session.session_id
            rotate_session()
            self.assertIsNone(session.user)
            self.assertNotEqual(session.session_id, logged)

    @with_transaction()
    def test_a_web_account_logs_in_with_its_email_and_password_only(self):
        account = self.account()
        for email in ('cuenta@gmail.com', '  Cuenta@Gmail.com '):
            self.assertEqual(authenticate_web_user(email, PASSWORD), account)
        for email, password in (('cuenta@gmail.com', 'otra'), ('nadie@gmail.com', PASSWORD),
                ('cuenta@gmail.com', ''), ('cuenta@gmail.com', None), (None, PASSWORD)):
            self.assertIsNone(authenticate_web_user(email, password), (email, password))

    @with_transaction()
    def test_an_account_created_only_to_receive_notices_cannot_log_in_and_does_not_fail(self):
        # The ones web_user_for creates for the Tryton users have no password at all.
        self.account('avisos@gmail.com', password=None)
        for password in ('', 'x', PASSWORD):
            self.assertIsNone(authenticate_web_user('avisos@gmail.com', password))
        # web.user.authenticate fails before it can count them: the wrapper counts them.
        self.assertEqual(
            Pool().get('web.user.authenticate.attempt').count('avisos@gmail.com'), 3)

    @with_transaction()
    def test_notices_reach_the_tryton_users_with_access_and_the_web_accounts_that_can_log_in(self):
        pool = Pool()
        company = create_company()
        with set_company(company):
            application, = pool.get('notification.web.application').create([{
                        'name': 'Avisos', 'code': 'videovigilance', 'company': company.id,
                        'origin': 'https://tryton.example', 'base_path': '/cameras/avisos/'}])
        linked = self.account('ana@gmail.com', password=None)
        self.user(web_user=linked.id)
        outsider_account = self.account('fuera@gmail.com', password=None)
        self.user(group=None, login='fuera', email='fuera@gmail.com',
            web_user=outsider_account.id)
        own = self.account('propia@gmail.com')
        no_password = self.account('sinclave@gmail.com', password=None)
        self.assertTrue(application.eligible_user(linked))
        self.assertFalse(application.eligible_user(outsider_account))
        self.assertTrue(application.eligible_user(own))
        self.assertFalse(application.eligible_user(no_password))

    @with_transaction()
    def test_a_session_left_by_the_old_login_stops_acting_as_its_user(self):
        pool = Pool()
        Session = pool.get('www.session')
        account = self.account()
        old = self.browser(session_id='old')
        Session.write([old], {'system_user': self.user().id, 'user': account.id})
        kept = self.browser(account, session_id='web')
        Session.__register__('videovigilance')
        # Searching goes to the database: the cleanup is SQL, which the record cache never sees.
        self.assertEqual(Session.search_count(
                [('id', '=', old.id), ('system_user', '=', None), ('user', '=', None)]), 1)
        # A web account that logged in with the new login is not logged out by the update.
        self.assertEqual(Session.search_count(
                [('id', '=', kept.id), ('user', '=', account.id)]), 1)
