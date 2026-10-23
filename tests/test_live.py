# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Tests for the live relay mailbox (no Tryton server, no camera needed).

Run from the module folder:  python -m unittest discover -s tests -v
The module is loaded by path so the tests run against this source tree.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f'vv_{name}', HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live = load('live')
JPEG = b'\xff\xd8\xff\xe0fake jpeg'


class MailboxTestCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        live.DIR = self.folder
        self.addCleanup(setattr, live, 'DIR', None)
        self.box = live.Mailbox('cameras')

    def age(self, path_part, seconds):
        "Make the files of the mailbox that contain `path_part` look `seconds` old."
        for name in os.listdir(self.folder):
            if path_part in name:
                moment = time.time() - seconds
                os.utime(Path(self.folder) / name, (moment, moment))


class FrameTest(MailboxTestCase):
    def test_every_frame_has_a_higher_sequence_than_the_last(self):
        first = self.box.put_frame(1, JPEG)
        second = self.box.put_frame(1, JPEG)
        third = self.box.put_frame(1, JPEG)
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertEqual(self.box.latest(1), (third, JPEG))

    def test_a_service_restart_does_not_make_sequences_go_back(self):
        before = self.box.put_frame(1, JPEG)
        # a new mailbox object, as in another worker or after Tryton restarts
        after = live.Mailbox('cameras').put_frame(1, JPEG)
        self.assertGreater(after, before)

    def test_an_old_frame_is_not_live_and_is_not_served(self):
        self.box.put_frame(1, JPEG)
        self.age('.frame', live.FRESH_S + 1)
        self.assertEqual(self.box.latest(1), (0, None))
        self.assertIsNone(self.box.peek(1))

    def test_peek_only_returns_a_picture_taken_a_moment_ago(self):
        self.box.put_frame(1, JPEG)
        self.assertEqual(self.box.peek(1), JPEG)
        self.age('.frame', 3.5)
        self.assertIsNone(self.box.peek(1))
        self.assertEqual(self.box.latest(1)[1], JPEG)  # still shown to a viewer

    def test_cameras_and_databases_do_not_see_each_others_frames(self):
        self.box.put_frame(1, JPEG)
        self.assertIsNone(self.box.latest(2)[1])
        self.assertIsNone(live.Mailbox('other').latest(1)[1])

    def test_a_frame_that_is_not_a_jpeg_is_refused(self):
        self.box.want(1)
        for bad in (b'', b'GIF89a', b'\xff\xd8' + b'x' * live.MAX_FRAME):
            with self.assertRaises(ValueError):
                self.box.accept_frame(1, bad)
        self.assertIsNone(self.box.latest(1)[1])


class DemandTest(MailboxTestCase):
    def test_a_camera_is_wanted_only_while_someone_asked_recently(self):
        self.assertFalse(self.box.wanted(1))
        self.box.want(1)
        self.assertTrue(self.box.wanted(1))
        self.age('.want', live.IDLE_S + 1)
        self.assertFalse(self.box.wanted(1))

    def test_a_frame_nobody_asked_for_is_not_kept(self):
        self.assertFalse(self.box.accept_frame(1, JPEG))
        self.assertIsNone(self.box.latest(1)[1])
        self.box.want(1)
        self.assertTrue(self.box.accept_frame(1, JPEG))
        self.assertEqual(self.box.latest(1)[1], JPEG)

    def test_the_last_picture_is_removed_when_the_service_polls_and_nobody_watches(self):
        self.box.want(1)
        self.box.accept_frame(1, JPEG)
        self.age('.want', live.IDLE_S + 1)
        self.box.exchange({'1': {}})
        self.assertFalse(os.path.exists(Path(self.folder) / 'cameras-1.frame'))


class ExchangeTest(MailboxTestCase):
    def test_the_service_is_connected_only_while_it_keeps_polling(self):
        self.assertFalse(self.box.service_online())
        self.box.exchange({})
        self.assertTrue(self.box.service_online())
        self.age('.service', live.SERVICE_S + 1)
        self.assertFalse(self.box.service_online())

    def test_the_answer_says_which_cameras_are_wanted(self):
        self.box.want(2)
        answer = self.box.exchange({'1': {}, '2': {}})
        self.assertFalse(answer['1']['wanted'])
        self.assertTrue(answer['2']['wanted'])

    def test_what_the_service_reports_is_kept_and_a_clean_report_clears_it(self):
        self.box.exchange({'1': {'error': 'no se puede abrir el vídeo', 'message': None}})
        self.assertEqual(self.box.status(1), ('no se puede abrir el vídeo', None))
        self.box.exchange({'1': {'error': None, 'message': None}})
        self.assertEqual(self.box.status(1), (None, None))

    def test_a_movement_problem_is_shown_for_a_few_seconds_only(self):
        self.box.exchange({'1': {'message': 'La cámara no obedece: sin respuesta'}})
        self.assertEqual(self.box.status(1)[1], 'La cámara no obedece: sin respuesta')
        live.MESSAGE_S, original = 0.05, live.MESSAGE_S
        self.addCleanup(setattr, live, 'MESSAGE_S', original)
        time.sleep(0.1)
        self.assertIsNone(self.box.status(1)[1])

    def test_a_report_without_a_message_does_not_erase_the_message_just_received(self):
        self.box.exchange({'1': {'message': 'fallo'}})
        self.box.exchange({'1': {'error': None, 'message': None}})
        self.assertEqual(self.box.status(1)[1], 'fallo')

    def test_garbage_reports_are_harmless(self):
        answer = self.box.exchange({'1': 'x', '2': None, '3': {'error': 5, 'message': ['a']}})
        self.assertEqual(set(answer), {'1', '2', '3'})


class CommandTest(MailboxTestCase):
    def online(self):
        self.box.exchange({})

    def test_nothing_can_be_sent_while_the_service_is_not_connected(self):
        with self.assertRaises(live.ServiceOffline):
            self.box.send(1, {'op': 'stop'})
        self.assertEqual(self.box.take(1), [])

    def test_commands_reach_the_service_once_and_in_order(self):
        self.online()
        self.box.send(1, {'op': 'move', 'dx': 1, 'dy': 0})
        self.box.send(1, {'op': 'stop'})
        answer = self.box.exchange({'1': {}})
        self.assertEqual([c['op'] for c in answer['1']['commands']], ['move', 'stop'])
        self.assertEqual(self.box.exchange({'1': {}})['1']['commands'], [])

    def test_commands_of_another_camera_are_not_delivered(self):
        self.online()
        self.box.send(2, {'op': 'stop'})
        self.assertEqual(self.box.exchange({'1': {}})['1']['commands'], [])
        self.assertEqual(len(self.box.exchange({'2': {}})['2']['commands']), 1)

    def test_a_command_that_waited_too_long_is_dropped(self):
        self.online()
        self.box.send(1, {'op': 'move', 'dx': 1, 'dy': 0})
        live.COMMAND_TTL_S, original = 0.05, live.COMMAND_TTL_S
        self.addCleanup(setattr, live, 'COMMAND_TTL_S', original)
        time.sleep(0.1)
        self.assertEqual(self.box.take(1), [])

    def test_sending_a_command_also_marks_the_camera_as_wanted(self):
        self.online()
        self.box.send(1, {'op': 'stop'})
        self.assertTrue(self.box.wanted(1))

    def test_two_workers_never_deliver_the_same_command_twice(self):
        self.online()
        self.box.send(1, {'op': 'stop'})
        first, second = live.Mailbox('cameras'), live.Mailbox('cameras')
        self.assertEqual(len(first.take(1)) + len(second.take(1)), 1)


class TokenTest(unittest.TestCase):
    def test_a_token_is_valid_for_its_database_until_it_expires(self):
        token = live.issue_token('secret', 'cameras', now=1000)
        self.assertTrue(live.check_token('secret', 'cameras', token, now=1000 + live.TOKEN_TTL_S - 1))
        self.assertFalse(live.check_token('secret', 'cameras', token, now=1000 + live.TOKEN_TTL_S + 1))

    def test_another_secret_or_database_or_tampering_is_refused(self):
        token = live.issue_token('secret', 'cameras')
        self.assertFalse(live.check_token('other', 'cameras', token))
        self.assertFalse(live.check_token('secret', 'other', token))
        expires, signature = token.split('.')
        self.assertFalse(live.check_token('secret', 'cameras', f'{int(expires) + 99999}.{signature}'))

    def test_garbage_is_refused_without_errors(self):
        for token in (None, '', 'x', '123', 'abc.def', '.', '1.2.3'):
            self.assertFalse(live.check_token('secret', 'cameras', token))


class DirectoryTest(unittest.TestCase):
    def test_a_directory_others_can_read_is_refused(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        os.chmod(folder, 0o755)
        live.DIR = folder
        self.addCleanup(setattr, live, 'DIR', None)
        with self.assertRaisesRegex(RuntimeError, 'privado'):
            live.Mailbox('cameras').want(1)

    def test_the_default_directory_is_private(self):
        live.DIR = None
        folder = live._directory()
        self.assertEqual(oct(folder.stat().st_mode & 0o777), '0o700')


if __name__ == '__main__':
    unittest.main()
