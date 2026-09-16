import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from onelap_garmin_sync.ledger import Ledger


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temporary.name) / "sync.db")

    def tearDown(self):
        self.ledger.close()
        self.temporary.cleanup()

    def add_activity(self, record_id, activity_time):
        self.ledger.upsert_activity(
            record_id,
            activity_time,
            record_id,
            json.dumps({"id": record_id}),
            ("CN", "GLOBAL"),
        )

    def test_due_activities_are_oldest_first(self):
        self.add_activity("new", "2026-08-25T02:00:00+00:00")
        self.add_activity("old", "2026-08-24T02:00:00+00:00")
        rows = self.ledger.due_activities(("CN", "GLOBAL"), 8, False)
        self.assertEqual([row["source_record_id"] for row in rows], ["old", "new"])

    def test_due_activities_returns_all_due_rows(self):
        for index in range(25):
            self.add_activity(
                f"ride-{index:02d}",
                f"2026-08-24T{index % 24:02d}:00:00+00:00",
            )
        rows = self.ledger.due_activities(("CN", "GLOBAL"), 8, False)
        self.assertEqual(len(rows), 25)

    def test_targets_have_independent_status(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        self.ledger.mark_upload_started("ride", "CN")
        self.ledger.mark_upload_success("ride", "CN", "123")
        self.assertEqual(
            self.ledger.due_targets("ride", ("CN", "GLOBAL"), 8, False),
            ["GLOBAL"],
        )

    def test_unchanged_activity_does_not_refresh_updated_at(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        before = self.ledger.connection.execute(
            "SELECT updated_at FROM activities WHERE source_record_id = 'ride'"
        ).fetchone()["updated_at"]
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        after = self.ledger.connection.execute(
            "SELECT updated_at FROM activities WHERE source_record_id = 'ride'"
        ).fetchone()["updated_at"]
        self.assertEqual(before, after)

    def test_due_activities_can_filter_one_record_id(self):
        self.add_activity("one", "2026-08-24T02:00:00+00:00")
        self.add_activity("two", "2026-08-25T02:00:00+00:00")
        rows = self.ledger.due_activities(
            ("CN", "GLOBAL"),
            8,
            False,
            record_id="two",
        )
        self.assertEqual([row["source_record_id"] for row in rows], ["two"])

    def test_requeue_activity_only_resets_selected_targets(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        for target in ("CN", "GLOBAL"):
            self.ledger.mark_upload_started("ride", target)
            self.ledger.mark_upload_success("ride", target, f"{target}-1")
        self.ledger.mark_processing_success(
            "ride",
            "/tmp/ride.fit",
            "abc",
            4440,
            2922,
            True,
            True,
            100,
        )

        changed = self.ledger.requeue_activity("ride", ("GLOBAL",))
        uploads = {
            row["target_region"]: dict(row)
            for row in self.ledger.connection.execute(
                "SELECT * FROM uploads WHERE source_record_id = 'ride'"
            ).fetchall()
        }

        self.assertTrue(changed)
        activity = dict(
            self.ledger.connection.execute(
                """
                SELECT * FROM activities
                WHERE source_record_id = 'ride'
                """
            ).fetchone()
        )
        self.assertEqual(uploads["CN"]["status"], "success")
        self.assertEqual(uploads["GLOBAL"]["status"], "pending")
        self.assertEqual(uploads["GLOBAL"]["attempts"], 0)
        self.assertEqual(activity["processing_status"], "not_requested")
        self.assertIsNone(activity["processed_fit_path"])

    def test_existing_database_is_migrated_with_processing_columns(self):
        self.ledger.close()
        database_path = Path(self.temporary.name) / "legacy.db"
        connection = sqlite3.connect(database_path)
        connection.executescript(
            """
            CREATE TABLE activities (
                source_record_id TEXT PRIMARY KEY,
                activity_time TEXT,
                name TEXT NOT NULL DEFAULT '',
                source_json TEXT NOT NULL DEFAULT '{}',
                fit_path TEXT,
                fit_sha256 TEXT,
                download_status TEXT NOT NULL DEFAULT 'pending',
                download_attempts INTEGER NOT NULL DEFAULT 0,
                download_error TEXT,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE uploads (
                source_record_id TEXT NOT NULL,
                target_region TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                remote_activity_id TEXT,
                next_retry_at TEXT,
                uploaded_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_record_id, target_region)
            );
            """
        )
        connection.close()

        migrated = Ledger(database_path)
        try:
            columns = {
                row["name"]
                for row in migrated.connection.execute(
                    "PRAGMA table_info(activities)"
                ).fetchall()
            }
        finally:
            migrated.close()
            self.ledger = Ledger(Path(self.temporary.name) / "sync.db")

        self.assertTrue(
            {
                "processing_status",
                "processing_error",
                "processed_fit_path",
                "processed_fit_sha256",
                "garmin_product_id",
                "garmin_software_version",
                "coordinate_transform_enabled",
                "garminize_enabled",
                "coordinate_points_converted",
            }.issubset(columns)
        )
        upload_connection = sqlite3.connect(database_path)
        try:
            upload_columns = {
                row[1]
                for row in upload_connection.execute(
                    "PRAGMA table_info(uploads)"
                ).fetchall()
            }
        finally:
            upload_connection.close()
        self.assertTrue(
            {
                "reconciliation_status",
                "reconciliation_checked_at",
                "reconciliation_message",
            }.issubset(upload_columns)
        )

    def test_present_reconciliation_repairs_remote_id_and_upload_status(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        self.ledger.mark_upload_started("ride", "CN")
        self.ledger.mark_upload_failed(
            "ride",
            "CN",
            "old error",
            "2026-08-27T00:00:00+00:00",
            False,
        )

        self.ledger.record_reconciliation(
            "ride",
            "CN",
            "present",
            "unique match",
            "garmin-123",
        )

        row = self.ledger.connection.execute(
            """
            SELECT * FROM uploads
            WHERE source_record_id = 'ride' AND target_region = 'CN'
            """
        ).fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["remote_activity_id"], "garmin-123")
        self.assertEqual(row["reconciliation_status"], "present")
        self.assertIsNone(row["last_error"])

    def test_missing_reconciliation_only_requeues_selected_region(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        for target in ("CN", "GLOBAL"):
            self.ledger.mark_upload_started("ride", target)
            self.ledger.mark_upload_success("ride", target, f"{target}-1")

        self.ledger.record_reconciliation(
            "ride",
            "GLOBAL",
            "missing",
            "not found",
            requeue_missing=True,
        )

        rows = {
            row["target_region"]: dict(row)
            for row in self.ledger.connection.execute(
                "SELECT * FROM uploads WHERE source_record_id = 'ride'"
            ).fetchall()
        }
        self.assertEqual(rows["CN"]["status"], "success")
        self.assertEqual(rows["CN"]["remote_activity_id"], "CN-1")
        self.assertEqual(rows["GLOBAL"]["status"], "pending")
        self.assertIsNone(rows["GLOBAL"]["remote_activity_id"])
        self.assertEqual(rows["GLOBAL"]["reconciliation_status"], "missing")

    def test_missing_dry_run_record_does_not_requeue(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        self.ledger.mark_upload_started("ride", "GLOBAL")
        self.ledger.mark_upload_success("ride", "GLOBAL", "GLOBAL-1")

        self.ledger.record_reconciliation(
            "ride",
            "GLOBAL",
            "missing",
            "dry run",
        )

        row = self.ledger.connection.execute(
            """
            SELECT * FROM uploads
            WHERE source_record_id = 'ride' AND target_region = 'GLOBAL'
            """
        ).fetchone()
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["remote_activity_id"], "GLOBAL-1")
        self.assertEqual(row["reconciliation_status"], "missing")

    def test_reset_stale_processing_marks_activity_failed(self):
        self.add_activity("ride", "2026-08-24T02:00:00+00:00")
        self.ledger.mark_processing_started(
            "ride",
            garminize_enabled=True,
            coordinate_transform_enabled=True,
            product_id=3122,
            software_version=975,
        )

        self.ledger.reset_stale_work()

        row = self.ledger.connection.execute(
            """
            SELECT processing_status, processing_error
            FROM activities WHERE source_record_id = 'ride'
            """
        ).fetchone()
        self.assertEqual(row["processing_status"], "failed")
        self.assertIn("预处理过程中中断", row["processing_error"])

    def test_auth_state_round_trip(self):
        self.assertIsNone(self.ledger.get_auth_state("onelap"))
        self.ledger.set_auth_state("onelap", b"encrypted-session")
        self.assertEqual(
            self.ledger.get_auth_state("onelap"),
            b"encrypted-session",
        )


if __name__ == "__main__":
    unittest.main()
