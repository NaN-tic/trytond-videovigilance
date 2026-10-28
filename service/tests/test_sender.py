import tempfile
import threading
import unittest
from pathlib import Path

from service.outbox import Outbox
from service.sender import EventSender
from service.tryton import TrytonRejected, TrytonUnavailable


class FakeTryton:
    def __init__(self):
        self.created = []
        self.fail_next = []  # exceptions to raise from create_event, in order
        self.lose_response = False  # creates the event, then the response fails
        self.reject_uids = set()

    def create_event(self, event, snapshot):
        if event['uid'] in self.reject_uids:
            raise TrytonRejected('validación')
        if self.fail_next:
            raise self.fail_next.pop(0)
        # Tryton keeps the first event of each uid.
        if all(uid != event['uid'] for uid, _ in self.created):
            self.created.append((event['uid'], snapshot))
        if self.lose_response:
            self.lose_response = False
            raise TrytonUnavailable('timeout')


class Harness(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'outbox.db'
        self.outbox = Outbox(self.path)
        self.addCleanup(self.outbox.close)
        self.client = FakeTryton()
        self.now = 1000.0
        self.sender = EventSender(self.client, self.outbox, threading.Event(), clock=lambda: self.now)

    def add(self, uid, snapshot=b'jpeg'):
        payload = {'uid': uid, 'camera': 1, 'type': 'person_motion',
            'started_at': '2026-01-01T10:00:00', 'ended_at': '2026-01-01T10:00:09'}
        self.outbox.add(uid, payload, snapshot, now=self.now)


class SenderTest(Harness):
    def test_event_survives_an_outage_and_is_sent_once_tryton_returns(self):
        self.add('a')
        self.client.fail_next = [TrytonUnavailable('down')]
        self.assertFalse(self.sender.send_once())
        self.assertEqual(self.outbox.count(), 1)
        self.assertFalse(self.sender.send_once())  # still waiting for retry
        self.now += 5
        self.assertTrue(self.sender.send_once())
        self.assertEqual(self.client.created, [('a', b'jpeg')])
        self.assertEqual(self.outbox.count(), 0)

    def test_lost_response_does_not_create_a_duplicate_on_retry(self):
        self.add('a')
        self.client.lose_response = True
        self.sender.send_once()
        self.now += 5
        self.sender.send_once()
        self.assertEqual([uid for uid, _ in self.client.created], ['a'])
        self.assertEqual(self.outbox.count(), 0)

    def test_a_rejected_event_does_not_block_the_ones_behind_it(self):
        self.add('bad')
        self.now += 1
        self.add('good')
        self.client.reject_uids = {'bad'}
        self.assertTrue(self.sender.send_once())
        self.assertTrue(self.sender.send_once())
        self.assertEqual([uid for uid, _ in self.client.created], ['good'])
        self.assertEqual(self.outbox.count(), 1)

    def test_event_is_set_aside_after_repeated_rejections(self):
        self.add('bad')
        self.client.reject_uids = {'bad'}
        for _ in range(5):
            self.assertTrue(self.sender.send_once())
            self.now += 31
        self.assertFalse(self.sender.send_once())
        self.assertEqual((self.outbox.count('pending'), self.outbox.count('failed')), (0, 1))


class OutboxTest(Harness):
    def test_pending_events_and_snapshots_survive_a_restart_in_order(self):
        self.add('first', b'\x00\xff')
        self.now += 1
        self.add('second', None)
        reopened = Outbox(self.path)
        self.addCleanup(reopened.close)
        first = reopened.claim_due(self.now)
        self.assertEqual((first.uid, first.snapshot, first.payload['camera']), ('first', b'\x00\xff', 1))
        reopened.done('first')
        second = reopened.claim_due(self.now)
        self.assertEqual((second.uid, second.snapshot), ('second', None))

    def test_full_queue_discards_the_oldest_pending_event(self):
        outbox = Outbox(Path(self.directory.name) / 'small.db', max_pending=2)
        self.addCleanup(outbox.close)
        for i, uid in enumerate(['a', 'b', 'c']):
            outbox.add(uid, {'uid': uid}, None, now=float(i))
        self.assertEqual(outbox.claim_due(10).uid, 'b')
        self.assertEqual(outbox.count(), 2)


if __name__ == '__main__':
    unittest.main()
