# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Camera movements, recorded as events of type «camera_movement».

A movement keeps the picture from just before the camera started to move and the
picture from after it stopped. A burst of move commands (an arrow held down) is one
movement; pressing again within MERGE_S of the end continues the same movement, so an
adjustment of several taps is a single record from the first start to the last stop.
"""
import logging
import threading
import uuid
from datetime import datetime, timedelta

from trytond.pool import Pool, PoolMeta
from trytond.transaction import Transaction, without_check_access

from .camera import PTZ_HOLD_S

logger = logging.getLogger(__name__)

TYPE = 'camera_movement'
# Pressing again within this many seconds of the last command or stop continues the movement.
MERGE_S = 4.0
# After the camera stops, wait this long for the picture to settle before the final frame.
SETTLE_S = 1.5
ARROWS = {(-1, 0): '◄', (1, 0): '►', (0, 1): '▲', (0, -1): '▼'}

# camera id -> (timer that will close its movement, id of that movement)
_pending = {}
_lock = threading.Lock()


def _schedule(camera_id, movement_id, delay):
    "Take the final frame and close the movement after `delay`, unless superseded."
    database = Transaction().database.name
    with _lock:
        previous = _pending.pop(camera_id, None)
        if previous is not None:
            previous[0].cancel()
        timer = threading.Timer(delay, _finish, args=(database, camera_id))
        timer.daemon = True
        _pending[camera_id] = (timer, movement_id)
        timer.start()


def _finish(database, camera_id):
    "Runs in a timer thread: closes the movement and stores its final frame."
    from . import live
    with _lock:
        entry = _pending.get(camera_id)
        if entry is None or entry[0] is not threading.current_thread():
            return  # a newer command took over
        del _pending[camera_id]
        movement_id = entry[1]
    frame = live.Mailbox(database).peek(camera_id)
    try:
        with Transaction().start(database, 0) as transaction:
            Event = Pool().get('videovigilance.event')
            with without_check_access():
                events = Event.search([('id', '=', movement_id)])
                if not events:
                    return
                movement = events[0]
                values = {}
                if movement.ended_at is None:
                    # The stop never came: the camera stopped by itself after its timeout.
                    values['ended_at'] = (movement.last_command_at
                        + timedelta(seconds=PTZ_HOLD_S))
                if frame is not None and not movement.end_frame_id:
                    values['end_frame'] = frame
                if values:
                    Event.write([movement], values)
            transaction.commit()
    except Exception:
        logger.exception("No s'ha pogut tancar el moviment de càmera %s", movement_id)


class CameraMovement(metaclass=PoolMeta):
    __name__ = 'videovigilance.event'

    @classmethod
    def record_command(cls, camera, user, dx, dy, frame=None):
        """A move command was sent to the camera: start a movement or continue the last.

        `user` is the res.user who sent it; `frame` is the live picture from before
        the command, used for a new movement.
        """
        arrow = ARROWS[(dx, dy)]
        user = user.id if user else None
        now = datetime.now()
        with without_check_access():
            last = cls.search(
                [('camera', '=', camera.id), ('type', '=', TYPE)],
                order=[('id', 'DESC')], limit=1)
            movement = last[0] if last else None
            same_user = movement is not None and (
                movement.user.id if movement.user else None) == user
            if (same_user and now - (movement.ended_at or movement.last_command_at)
                    <= timedelta(seconds=MERGE_S)):
                directions = movement.directions.split() if movement.directions else []
                if arrow not in directions:
                    directions.append(arrow)
                values = {
                    'last_command_at': now, 'ended_at': None,
                    'directions': ' '.join(directions)}
                if movement.end_frame_id:
                    values['end_frame'] = None  # it is taken again when it stops
                cls.write([movement], values)
            else:
                if movement is not None and movement.ended_at is None:
                    # an earlier movement whose end was never recorded
                    cls.write([movement], {
                            'ended_at': movement.last_command_at
                            + timedelta(seconds=PTZ_HOLD_S)})
                movement, = cls.create([{
                            'uid': uuid.uuid4().hex, 'type': TYPE,
                            'camera': camera.id, 'user': user, 'started_at': now,
                            'last_command_at': now, 'directions': arrow,
                            'start_frame': frame}])
        _schedule(camera.id, movement.id, PTZ_HOLD_S + SETTLE_S)

    @classmethod
    def record_stop(cls, camera):
        "The camera was told to stop: close the open movement and schedule its last frame."
        now = datetime.now()
        with without_check_access():
            opened = cls.search(
                [('camera', '=', camera.id), ('type', '=', TYPE), ('ended_at', '=', None)],
                order=[('id', 'DESC')], limit=1)
            if not opened:
                return
            cls.write(opened, {'ended_at': now, 'last_command_at': now})
        _schedule(camera.id, opened[0].id, SETTLE_S)
