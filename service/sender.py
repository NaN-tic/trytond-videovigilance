import logging
import threading
import time

from .tryton import TrytonRejected, TrytonUnavailable

log = logging.getLogger(__name__)

REJECTION_LIMIT = 5
REJECTION_DELAY_S = 30
MAX_BACKOFF_S = 60


class EventSender(threading.Thread):
    def __init__(self, client, outbox, stop, clock=time.time):
        super().__init__(name='event-sender', daemon=True)
        self._client, self._outbox, self._stop_event, self._clock = client, outbox, stop, clock

    def run(self):
        while not self._stop_event.is_set():
            if not self.send_once():
                self._stop_event.wait(1)

    def send_once(self):
        """Send the next due event; False if there is nothing to do now."""
        now = self._clock()
        item = self._outbox.claim_due(now)
        if item is None:
            return False
        try:
            # Tryton keeps the first event of each uid: sending it again after a lost
            # answer creates no duplicate.
            self._client.create_event(item.payload, item.snapshot)
            self._outbox.done(item.uid)
            log.info('Evento %s enviado a Tryton', item.uid)
            return True
        except TrytonUnavailable as error:
            delay = min(2 ** min(item.attempts, 6), MAX_BACKOFF_S)
            log.warning('Tryton no disponible (%s); reintento en %ds', error, delay)
            self._outbox.defer(item.uid, delay, now)
            return False
        except TrytonRejected as error:
            log.error('Tryton rechazó el evento %s: %s', item.uid, error)
            self._outbox.reject(item.uid, str(error), now, REJECTION_LIMIT, REJECTION_DELAY_S)
            return True
