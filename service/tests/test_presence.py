import unittest

from service.motion import MotionConfig, Person, PresenceDetector

DT = 1 / 3
STILL = [Person(380.0, 200.0, 80.0, 200.0, 0.9)]


def feed(detector, start, frames, people=STILL):
    reports = []
    for i in range(frames):
        reports += detector.update(start + i * DT, people)
    return reports


class PresenceTest(unittest.TestCase):
    def test_a_standing_person_is_reported_once_after_two_images(self):
        self.assertEqual(feed(PresenceDetector(MotionConfig()), 0, 1), [])
        (report,) = feed(PresenceDetector(MotionConfig()), 0, 2)
        self.assertAlmostEqual(report.started_at, 0)
        self.assertAlmostEqual(report.ended_at, DT)
        self.assertEqual((report.max_persons, report.max_confidence), (1, 0.9))
        detector = PresenceDetector(MotionConfig())
        self.assertEqual(len(feed(detector, 0, 60)), 1)

    def test_a_single_image_with_a_person_is_noise(self):
        detector = PresenceDetector(MotionConfig())
        detector.update(0, STILL)
        detector.update(DT, [])
        self.assertEqual(detector.update(2 * DT, STILL), [])

    def test_a_new_episode_starts_after_the_person_has_been_gone_long_enough(self):
        detector = PresenceDetector(MotionConfig(end_s=8.0))
        self.assertEqual(len(feed(detector, 0, 3)), 1)
        detector.tick(DT * 2 + 8.0)
        self.assertEqual(len(feed(detector, 20, 3)), 1)

    def test_a_short_gap_does_not_split_the_episode(self):
        detector = PresenceDetector(MotionConfig(end_s=8.0))
        self.assertEqual(len(feed(detector, 0, 3)), 1)
        detector.update(3.0, [])
        self.assertEqual(feed(detector, 4, 3), [])

    def test_a_long_stay_is_reminded_once_per_max_episode(self):
        detector = PresenceDetector(MotionConfig(end_s=8.0, max_episode_s=30.0))
        reports = feed(detector, 0, 3 * 100)  # 100 s at 3 images/s
        self.assertEqual(len(reports), 1 + 100 // 30)

    def test_snapshot_is_taken_only_for_the_best_image_before_reporting(self):
        calls = []
        detector = PresenceDetector(MotionConfig())
        for i, score in enumerate([0.6, 0.8, 0.7, 0.9]):
            people = [Person(380.0, 200.0, 80.0, 200.0, score)]
            for report in detector.update(i * DT, people,
                    snapshot=lambda counted, score=score: calls.append(score) or b'jpeg'):
                self.assertEqual(report.snapshot, b'jpeg')
        self.assertEqual(calls, [0.6, 0.8])  # reported at the 2nd image; no more encoding after


if __name__ == '__main__':
    unittest.main()
