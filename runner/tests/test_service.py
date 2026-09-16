import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from onelap_garmin_sync.config import GarminDeviceProfile, Settings
from onelap_garmin_sync.fit_preprocess import (
    FitProcessingResult,
    FitSummary,
)
from onelap_garmin_sync.garmin import ActivityListResult, UploadResult
from onelap_garmin_sync.ledger import Ledger
from onelap_garmin_sync.service import SyncService, activity_log_label


class FakeOneLap:
    def __init__(self, activities=()):
        self.activities = list(activities)

    def fetch_page(self, page, page_size=20):
        start = (page - 1) * page_size
        items = self.activities[start : start + page_size]
        return SimpleNamespace(
            items=items,
            is_last=start + page_size >= len(self.activities),
        )

    @staticmethod
    def validate_fit(path):
        return path.exists()

    @staticmethod
    def download_fit(activity, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{activity['id']}.fit"
        path.write_bytes(b"\x0e\x10\x00\x00\x00\x00\x00\x00.FITpayload")
        return path


class FakeGarmin:
    def __init__(
        self,
        failing_regions=(),
        raising_regions=(),
        activities_by_region=None,
    ):
        self.failing_regions = set(failing_regions)
        self.raising_regions = set(raising_regions)
        self.uploads = []
        self.activities_by_region = activities_by_region or {}
        self.list_calls = []

    def upload(self, region, fit_path):
        self.uploads.append((region, fit_path))
        if region in self.raising_regions:
            raise RuntimeError("uploader crashed")
        if region in self.failing_regions:
            return UploadResult(
                success=False,
                duplicate=False,
                auth_required=False,
                remote_activity_id=None,
                message="temporary error",
            )
        return UploadResult(
            success=True,
            duplicate=False,
            auth_required=False,
            remote_activity_id=f"{region.lower()}-1",
            message="ok",
        )

    def list_activities(self, region, start_date, end_date):
        self.list_calls.append((region, start_date, end_date))
        return ActivityListResult(
            success=True,
            auth_required=False,
            activities=list(self.activities_by_region.get(region, [])),
            truncated=False,
            message="ok",
        )


class FakePreprocessor:
    def __init__(self, output_dir, failing=False):
        self.output_dir = output_dir
        self.failing = failing
        self.calls = []

    def process(self, source_path, record_id, **kwargs):
        self.calls.append((source_path, record_id, kwargs))
        if self.failing:
            raise RuntimeError("processing failed")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / f"{record_id}.fit"
        path.write_bytes(source_path.read_bytes())
        profile = kwargs["profile"]
        return FitProcessingResult(
            path=path,
            sha256="abc",
            summary=FitSummary(
                record_count=100,
                timestamp_count=100,
                heart_rate_count=90,
                power_count=80,
                cadence_count=70,
                metrics_sha256="fingerprint",
            ),
            product_id=profile.product_id if profile else None,
            software_version=profile.software_version if profile else None,
            garminize_enabled=kwargs["garminize_enabled"],
            coordinate_transform_enabled=kwargs[
                "coordinate_transform_enabled"
            ],
            coordinate_points_converted=88,
        )


def make_settings(
    root,
    *,
    dry_run=False,
    preprocessing=False,
):
    return Settings(
        root_dir=root,
        data_dir=root / "data",
        download_dir=root / "data" / "downloads",
        database_path=root / "data" / "sync.db",
        lock_path=root / "data" / "sync.lock",
        onelap_token="token",
        onelap_refresh_token="",
        onelap_account="",
        onelap_password="",
        onelap_session_key="",
        targets=("CN", "GLOBAL"),
        discovery_pages=10,
        max_retries=8,
        dry_run=dry_run,
        uploader_path=root / "garmin-upload.js",
        garminize_fit_enabled=preprocessing,
        coordinate_transform_enabled=preprocessing,
        processed_dir=root / "data" / "processed",
        garmin_device_profile=(
            GarminDeviceProfile(3122, 1234567890, 975)
            if preprocessing
            else None
        ),
    )


def add_activity(ledger, settings):
    ledger.upsert_activity(
        "ride",
        "2026-08-24T02:00:00+00:00",
        "ride",
        json.dumps({"id": "ride"}),
        settings.targets,
    )
    return ledger.due_activities(settings.targets, 8, False)[0]


class ServiceTest(unittest.TestCase):
    def test_reconcile_repairs_all_confirmed_missing_activities(self):
        sources = [
            {
                "id": "newer",
                "start_riding_time": "2026-08-25 08:00:00",
                "time_seconds": 3600,
                "distance_km": 30,
                "sport_name": "骑行",
            },
            {
                "id": "older",
                "start_riding_time": "2026-08-24 08:00:00",
                "time_seconds": 1800,
                "distance_km": 15,
                "sport_name": "骑行",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(sources),
                    garmin,
                )

                result = service.reconcile_recent(10)
            finally:
                ledger.close()

        self.assertEqual(result, 0)
        self.assertEqual(
            [(region, path.name) for region, path in garmin.uploads],
            [
                ("CN", "older.fit"),
                ("GLOBAL", "older.fit"),
                ("CN", "newer.fit"),
                ("GLOBAL", "newer.fit"),
            ],
        )

    def test_reconcile_repairs_only_region_confirmed_missing(self):
        source = {
            "id": "ride",
            "start_riding_time": "2026-08-25 07:00:00",
            "time_seconds": 3600,
            "distance_km": 30,
            "sport_name": "骑行",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin(
                activities_by_region={
                    "CN": [
                        {
                            "activityId": "cn-existing",
                            "startTimeLocal": "2026-08-25 07:00:30",
                            "durationSeconds": 3610,
                            "distanceMeters": 30100,
                            "activityType": "cycling",
                        }
                    ],
                    "GLOBAL": [],
                }
            )
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap([source]),
                    garmin,
                )

                result = service.reconcile_recent(10)
                uploads = {
                    row["target_region"]: dict(row)
                    for row in ledger.connection.execute(
                        "SELECT * FROM uploads WHERE source_record_id = 'ride'"
                    ).fetchall()
                }
            finally:
                ledger.close()

        self.assertEqual(result, 0)
        self.assertEqual(
            [(region, path.name) for region, path in garmin.uploads],
            [("GLOBAL", "ride.fit")],
        )
        self.assertEqual(uploads["CN"]["remote_activity_id"], "cn-existing")
        self.assertEqual(uploads["CN"]["reconciliation_status"], "present")
        self.assertEqual(uploads["GLOBAL"]["status"], "success")
        self.assertEqual(uploads["GLOBAL"]["reconciliation_status"], "present")

    def test_reconcile_dry_run_does_not_replace_success_upload_state(self):
        source = {
            "id": "ride",
            "start_riding_time": "2026-08-25 07:00:00",
            "time_seconds": 3600,
            "distance_km": 30,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, dry_run=True)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            try:
                ledger.upsert_activity(
                    "ride",
                    "2026-08-24T23:00:00+00:00",
                    "ride",
                    json.dumps(source),
                    settings.targets,
                )
                for target in settings.targets:
                    ledger.mark_upload_started("ride", target)
                    ledger.mark_upload_success(
                        "ride",
                        target,
                        f"{target}-existing",
                    )
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap([source]),
                    garmin,
                )

                result = service.reconcile_recent(10)
                uploads = {
                    row["target_region"]: dict(row)
                    for row in ledger.connection.execute(
                        "SELECT * FROM uploads WHERE source_record_id = 'ride'"
                    ).fetchall()
                }
            finally:
                ledger.close()

        self.assertEqual(result, 0)
        self.assertEqual(garmin.uploads, [])
        self.assertEqual(uploads["CN"]["status"], "success")
        self.assertEqual(uploads["CN"]["remote_activity_id"], "CN-existing")
        self.assertEqual(uploads["GLOBAL"]["status"], "success")
        self.assertEqual(
            uploads["GLOBAL"]["remote_activity_id"],
            "GLOBAL-existing",
        )
        self.assertEqual(uploads["CN"]["reconciliation_status"], "missing")
        self.assertEqual(uploads["GLOBAL"]["reconciliation_status"], "missing")

    def test_activity_log_label_contains_human_readable_details(self):
        label = activity_log_label(
            {
                "source_record_id": "6a8a861b62c3d1c91f03a22a",
                "activity_time": "2026-08-23T07:01:16+00:00",
                "name": "",
                "source_json": json.dumps(
                    {
                        "id": "6a8a861b62c3d1c91f03a22a",
                        "start_riding_time": "2026-08-23 07:01:16",
                        "distance_km": 93.74,
                        "time_formatted": "2:59:12",
                    }
                ),
            }
        )

        self.assertEqual(
            label,
            "活动[2026-08-23 07:01 | 93.74 km | 2:59:12 | "
            "ID 6a8a861b62c3d1c91f03a22a]",
        )

    def test_activity_log_label_falls_back_to_seconds(self):
        label = activity_log_label(
            {
                "source_record_id": "ride",
                "activity_time": "2026-08-24T02:00:00+00:00",
                "name": "晨骑",
                "source_json": json.dumps(
                    {
                        "distance_km": "30.2",
                        "time_seconds": 4047,
                    },
                    ensure_ascii=False,
                ),
            }
        )

        self.assertEqual(
            label,
            "活动[2026-08-24 02:00 | 晨骑 | 30.20 km | 1:07:27 | ID ride]",
        )

    def test_one_region_upload_failure_does_not_block_other_region(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin(failing_regions=("GLOBAL",))
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                )
                failures = service._process_activity(
                    add_activity(ledger, settings),
                    retry_auth=False,
                )
                states = {
                    row["target_region"]: row["status"]
                    for row in ledger.connection.execute(
                        "SELECT target_region, status FROM uploads"
                    ).fetchall()
                }
            finally:
                ledger.close()

        self.assertEqual(failures, 1)
        self.assertEqual(states, {"CN": "success", "GLOBAL": "failed"})

    def test_one_region_upload_exception_does_not_block_other_region(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin(raising_regions=("CN",))
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                )
                failures = service._process_activity(
                    add_activity(ledger, settings),
                    retry_auth=False,
                )
                states = {
                    row["target_region"]: row["status"]
                    for row in ledger.connection.execute(
                        "SELECT target_region, status FROM uploads"
                    ).fetchall()
                }
            finally:
                ledger.close()

        self.assertEqual(failures, 1)
        self.assertEqual(
            [region for region, _ in garmin.uploads],
            ["CN", "GLOBAL"],
        )
        self.assertEqual(states, {"CN": "failed", "GLOBAL": "success"})

    def test_preprocessor_runs_once_and_both_regions_share_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, preprocessing=True)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            preprocessor = FakePreprocessor(settings.processed_dir)
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                    fit_preprocessor=preprocessor,
                )
                failures = service._process_activity(
                    add_activity(ledger, settings),
                    retry_auth=False,
                )
                activity = dict(
                    ledger.connection.execute(
                        """
                        SELECT * FROM activities
                        WHERE source_record_id = 'ride'
                        """
                    ).fetchone()
                )
            finally:
                ledger.close()

        self.assertEqual(failures, 0)
        self.assertEqual(len(preprocessor.calls), 1)
        self.assertEqual(
            [(region, path.name) for region, path in garmin.uploads],
            [("CN", "ride.fit"), ("GLOBAL", "ride.fit")],
        )
        self.assertIs(garmin.uploads[0][1], garmin.uploads[1][1])
        self.assertEqual(activity["processing_status"], "success")
        self.assertEqual(activity["coordinate_points_converted"], 88)

    def test_preprocessing_failure_blocks_both_regions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, preprocessing=True)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            preprocessor = FakePreprocessor(
                settings.processed_dir,
                failing=True,
            )
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                    fit_preprocessor=preprocessor,
                )
                failures = service._process_activity(
                    add_activity(ledger, settings),
                    retry_auth=False,
                )
                activity = dict(
                    ledger.connection.execute(
                        """
                        SELECT * FROM activities
                        WHERE source_record_id = 'ride'
                        """
                    ).fetchone()
                )
                statuses = [
                    row["status"]
                    for row in ledger.connection.execute(
                        "SELECT status FROM uploads ORDER BY target_region"
                    ).fetchall()
                ]
            finally:
                ledger.close()

        self.assertEqual(failures, 2)
        self.assertEqual(len(preprocessor.calls), 1)
        self.assertEqual(garmin.uploads, [])
        self.assertEqual(activity["processing_status"], "failed")
        self.assertEqual(statuses, ["pending", "pending"])

    def test_dry_run_preprocesses_once_without_uploading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(
                root,
                dry_run=True,
                preprocessing=True,
            )
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            preprocessor = FakePreprocessor(settings.processed_dir)
            try:
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                    fit_preprocessor=preprocessor,
                )
                failures = service._process_activity(
                    add_activity(ledger, settings),
                    retry_auth=False,
                )
            finally:
                ledger.close()

        self.assertEqual(failures, 0)
        self.assertEqual(len(preprocessor.calls), 1)
        self.assertEqual(garmin.uploads, [])

    def test_redownload_invalidates_old_processed_fit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, preprocessing=True)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            preprocessor = FakePreprocessor(settings.processed_dir)
            old_processed = settings.processed_dir / "ride.fit"
            old_processed.write_bytes(b"old-processed-fit")
            old_digest = hashlib.sha256(
                old_processed.read_bytes()
            ).hexdigest()
            try:
                ledger.upsert_activity(
                    "ride",
                    "2026-08-24T02:00:00+00:00",
                    "ride",
                    json.dumps({"id": "ride"}),
                    settings.targets,
                )
                ledger.mark_download_success(
                    "ride",
                    str(settings.download_dir / "missing.fit"),
                    "old-source",
                )
                ledger.mark_processing_success(
                    "ride",
                    str(old_processed),
                    old_digest,
                    3122,
                    975,
                    True,
                    True,
                    88,
                )
                activity = ledger.due_activities(
                    settings.targets,
                    8,
                    False,
                )[0]
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                    fit_preprocessor=preprocessor,
                )

                failures = service._process_activity(
                    activity,
                    retry_auth=False,
                )
                rewritten_bytes = old_processed.read_bytes()
            finally:
                ledger.close()

        self.assertEqual(failures, 0)
        self.assertEqual(len(preprocessor.calls), 1)
        self.assertNotEqual(rewritten_bytes, b"old-processed-fit")

    def test_changed_processed_file_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = make_settings(root, preprocessing=True)
            settings.prepare_directories()
            ledger = Ledger(settings.database_path)
            garmin = FakeGarmin()
            preprocessor = FakePreprocessor(settings.processed_dir)
            source = settings.download_dir / "ride.fit"
            source.write_bytes(b"source-fit")
            processed = settings.processed_dir / "ride.fit"
            processed.write_bytes(b"changed-after-ledger-write")
            try:
                ledger.upsert_activity(
                    "ride",
                    "2026-08-24T02:00:00+00:00",
                    "ride",
                    json.dumps({"id": "ride"}),
                    settings.targets,
                )
                ledger.mark_download_success("ride", str(source), "source-sha")
                ledger.mark_processing_success(
                    "ride",
                    str(processed),
                    "different-sha",
                    3122,
                    975,
                    True,
                    True,
                    88,
                )
                activity = ledger.due_activities(
                    settings.targets,
                    8,
                    False,
                )[0]
                service = SyncService(
                    settings,
                    ledger,
                    FakeOneLap(),
                    garmin,
                    fit_preprocessor=preprocessor,
                )

                failures = service._process_activity(
                    activity,
                    retry_auth=False,
                )
            finally:
                ledger.close()

        self.assertEqual(failures, 0)
        self.assertEqual(len(preprocessor.calls), 1)


if __name__ == "__main__":
    unittest.main()
