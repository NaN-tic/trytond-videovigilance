"""Person motion and presence, based on the bounding boxes detected in each image.

Boxes are associated across images (IoU or center distance) to track each
person. A person is "moving" if, within the time window, their center has
shifted, or their box has changed size (they are approaching or moving away),
by at least `threshold` times their height. A stationary person does not count,
even if their box jitters.

An episode starts after `start_frames` consecutive images with motion (or, for
presence, with a person) and is reported at THAT point, without waiting for it to
end: the alert must not depend on how long the person stays. It is reported only
once per episode. The episode ends silently when `end_s` seconds pass without
motion; if it lasts longer than `max_episode_s`, it is reported again as a reminder.
"""

import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class MotionConfig:
    window_s: float = 2.0
    threshold: float = 0.15
    min_iou: float = 0.1
    max_distance: float = 0.75
    track_ttl_s: float = 2.0
    start_frames: int = 2
    end_s: float = 8.0
    max_episode_s: float = 300.0

    def __post_init__(self):
        if min(self.window_s, self.threshold, self.min_iou, self.max_distance,
                self.track_ttl_s, self.end_s, self.max_episode_s) <= 0:
            raise ValueError('Motion times and thresholds must be positive')
        if self.start_frames < 1:
            raise ValueError('start_frames must be at least 1')
        if self.max_episode_s <= self.end_s:
            raise ValueError('max_episode_s must exceed end_s')


@dataclass(frozen=True)
class Person:
    x: float
    y: float
    width: float
    height: float
    score: float

    @property
    def center(self):
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class Episode:
    started_at: float
    ended_at: float
    max_persons: int
    max_confidence: float
    snapshot: bytes | None


def _iou(a, b):
    width = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    height = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    if width <= 0 or height <= 0:
        return 0.0
    intersection = width * height
    return intersection / (a.width * a.height + b.width * b.height - intersection)


class _Track:
    __slots__ = ('person', 'last_seen', 'history')

    def __init__(self, t, person):
        self.person = person
        self.last_seen = t
        self.history = deque([(t, *person.center, person.height)])

    def observe(self, t, person, window):
        self.person = person
        self.last_seen = t
        self.history.append((t, *person.center, person.height))
        # Keep a point at or before the start of the window so comparisons work
        # even when the interval between images is longer than the window.
        while len(self.history) > 1 and self.history[1][0] <= t - window:
            self.history.popleft()

    def is_moving(self, threshold):
        if len(self.history) < 2:
            return False
        _, x0, y0, h0 = self.history[0]
        _, x1, y1, h1 = self.history[-1]
        height = max(h1, 1e-6)
        shift = math.hypot(x1 - x0, y1 - y0) / height
        scale = abs(h1 - h0) / height
        return max(shift, scale) >= threshold


class _Draft:
    def __init__(self, confirmed=False):
        self.confirmed = confirmed
        self.reported = False
        self.started_at = None
        self.ended_at = None
        self.frames = 0
        self.max_persons = 0
        self.max_confidence = 0.0
        self.snapshot = None
        self._snapshot_score = -1.0

    def add(self, t, people, counted, snapshot):
        if self.started_at is None:
            self.started_at = t
        self.ended_at = t
        self.frames += 1
        self.max_persons = max(self.max_persons, len(people))
        best = max(p.score for p, is_counted in zip(people, counted) if is_counted)
        self.max_confidence = max(self.max_confidence, best)
        # After reporting, another image is unnecessary: avoid encoding JPEGs needlessly.
        if snapshot is not None and not self.reported and best > self._snapshot_score:
            data = snapshot(counted)
            if data:
                self.snapshot, self._snapshot_score = data, best

    def report(self):
        return Episode(self.started_at, self.ended_at, self.max_persons,
            min(1.0, float(self.max_confidence)), self.snapshot)


class _EpisodeDetector:
    """Turns analysed images into episodes. Subclasses say which people count in an image
    (`_counted`); an image where nobody counts does not belong to an episode."""

    def __init__(self, config=None):
        self.config = config or MotionConfig()
        self._current = None

    def _counted(self, t, people):
        raise NotImplementedError

    def update(self, t, people, snapshot=None):
        """Process an analyzed image and return episodes that have just started.

        An episode is returned only once, in the image that confirms it.
        `snapshot(counted)` (one flag per person) is called only when the image is the
        best one in the episode so far; it must return JPEG bytes or None.
        """
        counted = self._counted(t, people)
        cfg = self.config
        self._expire(t)
        if not any(counted):
            if self._current is not None and not self._current.confirmed:
                self._current = None
            return []
        current = self._current
        if current is None:
            current = self._current = _Draft()
        elif current.confirmed and t - current.started_at >= cfg.max_episode_s:
            current = self._current = _Draft(confirmed=True)
        current.add(t, people, counted, snapshot)
        if current.frames >= cfg.start_frames:
            current.confirmed = True
        if current.confirmed and not current.reported:
            current.reported = True
            return [current.report()]
        return []

    def tick(self, t):
        """End an expired episode when no new images arrive."""
        self._expire(t)

    def close(self):
        """Forget the current episode (camera or service shutdown)."""
        self._current = None

    def _expire(self, t):
        current = self._current
        if (current is not None and current.confirmed
                and t - current.ended_at >= self.config.end_s):
            self._current = None


class PresenceDetector(_EpisodeDetector):
    "A person in view counts, moving or not."

    def _counted(self, t, people):
        return [True] * len(people)


class MotionDetector(_EpisodeDetector):
    "Only a person who moves counts."

    def __init__(self, config=None):
        super().__init__(config)
        self._tracks = []

    def close(self):
        """Forget the current episode and tracks (camera or service shutdown)."""
        super().close()
        self._tracks.clear()

    def _counted(self, t, people):
        return self._track(t, people)

    def _track(self, t, people):
        cfg = self.config
        pairs = []
        for ti, track in enumerate(self._tracks):
            for pi, person in enumerate(people):
                iou = _iou(track.person, person)
                (x0, y0), (x1, y1) = track.person.center, person.center
                distance = math.hypot(x1 - x0, y1 - y0) / max(
                    track.person.height, person.height, 1e-6)
                if iou >= cfg.min_iou or distance <= cfg.max_distance:
                    pairs.append((-iou, distance, ti, pi))
        pairs.sort()
        matched, used = {}, set()
        for _, _, ti, pi in pairs:
            if ti not in used and pi not in matched:
                used.add(ti)
                matched[pi] = self._tracks[ti]
        moving = []
        for pi, person in enumerate(people):
            track = matched.get(pi)
            if track is None:
                self._tracks.append(_Track(t, person))
                moving.append(False)
            else:
                track.observe(t, person, cfg.window_s)
                moving.append(track.is_moving(cfg.threshold))
        self._tracks = [tr for tr in self._tracks if t - tr.last_seen <= cfg.track_ttl_s]
        return moving
