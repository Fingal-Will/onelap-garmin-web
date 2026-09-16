import unittest
from datetime import date, datetime

from onelap_garmin_sync.reconcile import (
    GarminActivity,
    OneLapCandidate,
    comparison_date_range,
    fingerprint_matches,
    reconcile_activities,
)


def candidate(
    record_id="ride",
    *,
    start="2026-08-25 07:00:00",
    duration=3600,
    distance=30000,
    remote_id=None,
):
    return OneLapCandidate(
        record_id=record_id,
        start_time_local=datetime.fromisoformat(start) if start else None,
        duration_seconds=duration,
        distance_meters=distance,
        activity_type="cycling",
        remote_activity_id=remote_id,
    )


def garmin(
    activity_id="100",
    *,
    start="2026-08-25 07:00:30",
    duration=3610,
    distance=30100,
):
    return GarminActivity(
        activity_id=activity_id,
        start_time_local=datetime.fromisoformat(start) if start else None,
        duration_seconds=duration,
        distance_meters=distance,
        activity_type="cycling",
    )


class ReconcileTest(unittest.TestCase):
    def test_incomplete_remote_activity_prevents_missing_decision(self):
        candidate = OneLapCandidate(
            "one",
            datetime(2026, 8, 25, 7, 0),
            3600,
            30000,
            "cycling",
        )
        incomplete = GarminActivity(
            "garmin-one",
            None,
            3600,
            30000,
            "cycling",
        )

        result = reconcile_activities([candidate], [incomplete])[0]

        self.assertEqual(result.status, "unverified")

    def test_exact_id_remains_present_with_incomplete_remote_metrics(self):
        candidate = OneLapCandidate(
            "one",
            datetime(2026, 8, 25, 7, 0),
            3600,
            30000,
            "cycling",
            remote_activity_id="garmin-one",
        )
        incomplete = GarminActivity(
            "garmin-one",
            None,
            None,
            None,
            None,
        )

        result = reconcile_activities([candidate], [incomplete])[0]

        self.assertEqual(result.status, "present")

    def test_exact_remote_id_is_preferred(self):
        result = reconcile_activities(
            [candidate(remote_id="saved")],
            [
                garmin("fingerprint"),
                garmin(
                    "saved",
                    start="2026-08-20 07:00:00",
                    duration=100,
                    distance=100,
                ),
            ],
        )[0]
        self.assertEqual(result.status, "present")
        self.assertEqual(result.remote_activity_id, "saved")

    def test_unique_fingerprint_matches(self):
        result = reconcile_activities([candidate()], [garmin()])[0]
        self.assertEqual(result.status, "present")
        self.assertEqual(result.remote_activity_id, "100")

    def test_no_fingerprint_match_is_missing(self):
        result = reconcile_activities(
            [candidate()],
            [garmin(start="2026-08-25 08:00:00")],
        )[0]
        self.assertEqual(result.status, "missing")

    def test_multiple_remote_matches_are_ambiguous(self):
        result = reconcile_activities(
            [candidate()],
            [garmin("100"), garmin("101")],
        )[0]
        self.assertEqual(result.status, "ambiguous")

    def test_one_remote_match_for_two_candidates_is_ambiguous(self):
        results = reconcile_activities(
            [candidate("one"), candidate("two")],
            [garmin()],
        )
        self.assertEqual(
            {result.record_id: result.status for result in results},
            {"one": "ambiguous", "two": "ambiguous"},
        )

    def test_incomplete_fingerprint_is_unverified(self):
        result = reconcile_activities(
            [candidate(duration=None)],
            [],
        )[0]
        self.assertEqual(result.status, "unverified")

    def test_tolerances_are_inclusive(self):
        self.assertTrue(
            fingerprint_matches(
                candidate(),
                garmin(
                    start="2026-08-25 07:02:00",
                    duration=3630,
                    distance=30200,
                ),
            )
        )
        self.assertFalse(
            fingerprint_matches(
                candidate(),
                garmin(start="2026-08-25 07:02:01"),
            )
        )

    def test_date_range_adds_one_day_on_each_side(self):
        result = comparison_date_range(
            [
                candidate(start="2026-08-20 12:00:00"),
                candidate("two", start="2026-08-25 12:00:00"),
            ]
        )
        self.assertEqual(result, (date(2026, 8, 19), date(2026, 8, 26)))


if __name__ == "__main__":
    unittest.main()
