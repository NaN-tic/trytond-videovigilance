import random
import unittest

from service.motion import MotionConfig, MotionDetector, Person

DT = 1 / 3


def person(cx, cy=300.0, height=200.0, score=0.9):
    width = height * 0.4
    return Person(cx - width / 2, cy - height / 2, width, height, score)


def walk(detector, start, frames, step=60.0, **kwargs):
    """A person moving `step` pixels per image; return the reports emitted."""
    reports = []
    for i in range(frames):
        reports += detector.update(start + i * DT, [person(100 + i * step, **kwargs)])
    return reports


class StillPersonTest(unittest.TestCase):
    def test_jitter_of_a_standing_person_never_creates_an_episode(self):
        rng = random.Random(1)
        detector = MotionDetector()
        reports = []
        for i in range(300):
            jitter = lambda: rng.uniform(-0.03, 0.03) * 200
            reports += detector.update(i * DT, [person(400 + jitter(), 300 + jitter(), 200 + jitter())])
        self.assertEqual(reports, [])


class EpisodeTest(unittest.TestCase):
    def test_walking_person_is_reported_once_as_soon_as_movement_is_confirmed(self):
        # The first image only creates the track; the second shows motion but is not enough.
        self.assertEqual(walk(MotionDetector(), 0, 2), [])
        (report,) = walk(MotionDetector(), 0, 3)
        self.assertAlmostEqual(report.started_at, DT)
        self.assertAlmostEqual(report.ended_at, 2 * DT)
        self.assertEqual(report.max_persons, 1)
        self.assertAlmostEqual(report.max_confidence, 0.9)

    def test_a_report_is_not_repeated_while_the_person_keeps_moving_nor_when_it_ends(self):
        detector = MotionDetector()
        self.assertEqual(len(walk(detector, 0, 9)), 1)
        detector.tick(8 * DT + 100)
        self.assertIsNone(detector.close())

    def test_a_single_moving_image_is_noise_not_an_episode(self):
        detector = MotionDetector()
        reports = detector.update(0, [person(100)])
        reports += detector.update(DT, [person(300)])
        reports += detector.update(2 * DT, [person(300)])
        self.assertEqual(reports, [])

    def test_pause_shorter_than_end_merges_and_longer_splits(self):
        for pause, expected in [(4.0, 1), (9.0, 2)]:
            with self.subTest(pause=pause):
                detector = MotionDetector()
                reports = walk(detector, 0, 6)
                reports += detector.update(5 * DT + pause / 2, [person(2000)])
                resume = 5 * DT + pause
                reports += walk(detector, resume, 6, step=-60.0)
                self.assertEqual(len(reports), expected)

    def test_continuous_activity_is_reminded_every_max_episode(self):
        detector = MotionDetector(MotionConfig(end_s=2.0, max_episode_s=10.0))
        reports = walk(detector, 0, 90, step=10.0)
        self.assertGreaterEqual(len(reports), 3)
        for earlier, later in zip(reports, reports[1:]):
            self.assertAlmostEqual(later.ended_at - earlier.ended_at, 10.0, delta=2 * DT)

    def test_close_forgets_the_episode_so_new_movement_is_reported_again(self):
        detector = MotionDetector()
        self.assertEqual(len(walk(detector, 0, 5)), 1)
        detector.close()
        self.assertEqual(len(walk(detector, 5 * DT + 1, 5)), 1)

    def test_approaching_person_counts_as_moving_even_if_the_center_is_fixed(self):
        detector = MotionDetector()
        reports = []
        for i in range(6):
            reports += detector.update(i * DT, [person(400, 300, 150 + i * 40)])
        self.assertEqual(len(reports), 1)

    def test_only_the_moving_person_is_flagged_and_both_are_counted(self):
        detector = MotionDetector()
        flags, reports = [], []
        for i in range(6):
            people = [person(100), person(300 + i * 60)]
            reports += detector.update(i * DT, people, snapshot=lambda moving: flags.append(list(moving)) or b'x')
        self.assertEqual(flags[-1], [False, True])
        (report,) = reports
        self.assertEqual(report.max_persons, 2)


class SnapshotTest(unittest.TestCase):
    def test_report_carries_the_best_image_up_to_confirmation_and_nothing_is_encoded_afterwards(self):
        detector = MotionDetector()
        scores = [0.5, 0.6, 0.7, 0.95, 0.99]
        calls, reports = [], []
        for i, score in enumerate(scores):
            def snapshot(moving, score=score):
                calls.append(score)
                return f'jpeg-{score}'.encode()
            reports += detector.update(i * DT, [person(100 + i * 60, score=score)], snapshot=snapshot)
        (report,) = reports
        # The first image does not move (it only creates the track); later images after reporting are redundant.
        self.assertEqual(calls, [0.6, 0.7])
        self.assertEqual(report.snapshot, b'jpeg-0.7')
        self.assertAlmostEqual(report.max_confidence, 0.7)

    def test_failed_snapshot_keeps_the_report_without_image(self):
        detector = MotionDetector()
        reports = []
        for i in range(5):
            reports += detector.update(i * DT, [person(100 + i * 60)], snapshot=lambda moving: None)
        (report,) = reports
        self.assertIsNone(report.snapshot)


class ConfigTest(unittest.TestCase):
    def test_invalid_values_are_rejected(self):
        for kwargs in [{'threshold': 0}, {'start_frames': 0}, {'end_s': 10, 'max_episode_s': 10}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MotionConfig(**kwargs)


if __name__ == '__main__':
    unittest.main()
