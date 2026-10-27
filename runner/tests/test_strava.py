import json
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace
from urllib.parse import urlencode

import requests

from onelap_garmin_sync.config import Settings
from onelap_garmin_sync.garmin import UploadResult
from onelap_garmin_sync.ledger import Ledger
from onelap_garmin_sync.reconcile import ReconciliationMatch
from onelap_garmin_sync.service import SyncService
from onelap_garmin_sync.strava import StravaUploader
from onelap_garmin_sync.strava_auth import parse_redirect
from test_service import FakeGarmin, FakeOneLap as BaseOneLap, make_settings


class FakeOneLap(BaseOneLap):
    def iter_pages(self, start_page=1, max_pages=0):
        page = self.fetch_page(start_page)
        yield SimpleNamespace(page=start_page, items=page.items, is_last=page.is_last)


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def mock_strava():
    uploader = Mock(spec=StravaUploader)
    uploader.has_pending_upload.return_value = False
    uploader.reconcile.side_effect = lambda candidates: [ReconciliationMatch(
        candidate.record_id, "missing", None, "no existing activity"
    ) for candidate in candidates]
    return uploader


def tokens(refresh="rotated-refresh"):
    return {
        "access_token": "access-secret",
        "refresh_token": refresh,
        "expires_at": int(time.time()) + 3600,
    }


class StravaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = replace(
            make_settings(self.root),
            strava_sync_enabled=True,
            strava_client_id="123",
            strava_client_secret="client-secret",
            strava_refresh_token="initial-refresh",
            strava_session_key="encryption-secret",
        )
        self.settings.prepare_directories()
        self.ledger = Ledger(self.settings.database_path)
        self.fit = self.root / "ride.fit"
        self.fit.write_bytes(b"test-fit")
        self.http = Mock()
        self.uploader = StravaUploader(
            self.settings, self.ledger, self.http, sleep=lambda _: None
        )

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_success_waits_for_activity_and_persists_encrypted_rotation(self):
        self.http.request.side_effect = [
            response(tokens()),
            response({"id": 55}, 201),
            response({"activity_id": 66}),
        ]
        result = self.uploader.upload("ride", self.fit)
        self.assertTrue(result.success)
        self.assertEqual(result.remote_activity_id, "66")
        calls = self.http.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["POST", "POST", "GET"])
        self.assertEqual(
            calls[1].kwargs["data"],
            {"data_type": "fit", "external_id": "onelap-ride.fit"},
        )
        self.assertEqual(
            calls[1].kwargs["headers"]["Authorization"], "Bearer access-secret"
        )
        for call in calls:
            self.assertFalse(call.kwargs["allow_redirects"])
        encrypted = self.ledger.get_auth_state(self.uploader.provider)
        self.assertNotIn(b"rotated-refresh", encrypted)
        saved = json.loads(self.uploader.fernet.decrypt(encrypted))
        self.assertEqual(saved["refresh_token"], "rotated-refresh")

    def test_new_instance_refreshes_with_rotated_token(self):
        expired = tokens("rotated-refresh")
        expired["expires_at"] = 0
        self.ledger.set_auth_state(
            self.uploader.provider,
            self.uploader.fernet.encrypt(json.dumps(expired).encode()),
        )
        self.http.request.side_effect = [
            response(tokens("new-refresh")),
            response({"activity_id": 67}),
        ]
        uploader = StravaUploader(self.settings, self.ledger, self.http)
        self.assertTrue(uploader.upload("ride", self.fit).success)
        self.assertEqual(
            self.http.request.call_args_list[0].kwargs["data"]["refresh_token"],
            "rotated-refresh",
        )

    def test_processing_timeout_resumes_same_upload_across_runs(self):
        self.http.request.side_effect = [
            response(tokens()),
            response({"id": 55}, 201),
        ] + [response({"id": 55})] * 3
        result = self.uploader.upload("ride", self.fit)
        self.assertFalse(result.success)
        self.assertFalse(result.auth_required)
        resumed_http = Mock()
        resumed_http.request.return_value = response({"activity_id": 66})
        resumed = StravaUploader(self.settings, self.ledger, resumed_http)
        self.assertTrue(resumed.upload("ride", self.fit).success)
        resumed_http.request.assert_called_once()
        self.assertEqual(
            resumed_http.request.call_args.args, ("GET", f"{resumed.API}/uploads/55")
        )

    def test_duplicate_at_submission_or_poll_is_success(self):
        for status in (200, 400):
            with self.subTest(status=status):
                self.http.request.side_effect = [
                    response(tokens()),
                    response({"error": "duplicate of activity 12345"}, status),
                ]
                self.uploader.tokens = None
                result = self.uploader.upload(f"ride-{status}", self.fit)
                self.assertTrue(result.success)
                self.assertTrue(result.duplicate)
                self.assertEqual(result.remote_activity_id, "12345")
                # Remove cached access token so both cases exercise OAuth + submission.
                self.ledger.connection.execute("DELETE FROM auth_state")
                self.ledger.connection.commit()

    def test_terminal_processing_error_is_failure_and_can_resubmit(self):
        self.http.request.side_effect = [
            response(tokens()),
            response({"id": 55}, 201),
            response({"error": "malformed FIT"}),
        ]
        result = self.uploader.upload("ride", self.fit)
        self.assertFalse(result.success)
        self.assertEqual(
            self.ledger.get_metadata(f"{self.uploader.provider}:upload:ride"), ""
        )

    def test_auth_network_rate_and_server_errors_are_sanitized(self):
        for status, auth in (
            (400, True),
            (401, True),
            (403, True),
            (429, False),
            (500, False),
        ):
            with self.subTest(status=status):
                self.uploader.tokens = None
                self.http.request.side_effect = None
                self.http.request.return_value = response(
                    {"message": "client-secret access-secret"}, status
                )
                result = self.uploader.upload("ride", self.fit)
                self.assertFalse(result.success)
                self.assertEqual(result.auth_required, auth)
                self.assertNotIn("secret", result.message)
        self.http.request.side_effect = requests.Timeout("client-secret")
        self.assertNotIn(
            "client-secret", self.uploader.upload("ride", self.fit).message
        )

    def test_unauthorized_upload_invalidates_cached_access_token(self):
        self.http.request.side_effect = [response(tokens()), response({}, 401)]
        self.assertTrue(self.uploader.upload("ride", self.fit).auth_required)
        self.http.request.side_effect = [
            response(tokens("next-refresh")),
            response({"activity_id": 77}),
        ]
        resumed = StravaUploader(self.settings, self.ledger, self.http)
        self.assertTrue(resumed.upload("ride", self.fit).success)
        self.assertEqual(
            self.http.request.call_args_list[-2].kwargs["data"]["refresh_token"],
            "rotated-refresh",
        )

    def test_wrong_encryption_key_does_not_discard_rotated_session(self):
        self.ledger.set_auth_state(
            self.uploader.provider,
            self.uploader.fernet.encrypt(json.dumps(tokens()).encode()),
        )
        uploader = StravaUploader(
            replace(self.settings, strava_session_key="wrong-key"),
            self.ledger,
            self.http,
        )
        self.assertTrue(uploader.upload("ride", self.fit).auth_required)
        self.http.request.assert_not_called()

    def test_network_failure_during_poll_keeps_upload_for_retry(self):
        self.http.request.side_effect = [
            response(tokens()),
            response({"id": 55}, 201),
            requests.Timeout("secret"),
        ]
        self.assertFalse(self.uploader.upload("ride", self.fit).success)
        self.assertEqual(
            self.ledger.get_metadata(f"{self.uploader.provider}:upload:ride"), "55"
        )
        resumed_http = Mock()
        resumed_http.request.return_value = response(
            {"error": "duplicate of activity 66"}
        )
        resumed = StravaUploader(self.settings, self.ledger, resumed_http)
        result = resumed.upload("ride", self.fit)
        self.assertTrue(result.duplicate)
        self.assertEqual(result.remote_activity_id, "66")
        self.assertEqual(resumed_http.request.call_args.args[0], "GET")

    def test_service_exception_keeps_credentials_out_of_logs(self):
        strava = mock_strava()
        strava.upload.side_effect = RuntimeError("access-secret client-secret")
        service = SyncService(
            self.settings,
            self.ledger,
            FakeOneLap([{"id": "ride"}]),
            FakeGarmin(),
            strava=strava,
        )
        with self.assertLogs("onelap_garmin_sync.service", level="ERROR") as logs:
            self.assertEqual(service.run(), 1)
        self.assertNotIn("access-secret", " ".join(logs.output))
        row = self.ledger.connection.execute(
            "SELECT last_error FROM uploads WHERE target_region='STRAVA'"
        ).fetchone()
        self.assertNotIn("secret", row["last_error"])

    def test_reconcile_does_not_initialize_strava(self):
        with patch("onelap_garmin_sync.service.StravaUploader") as factory:
            service = SyncService(
                self.settings, self.ledger, FakeOneLap(), FakeGarmin()
            )
            self.assertEqual(service.reconcile_recent(20), 0)
            factory.assert_not_called()

    def test_missing_upload_id_is_not_success(self):
        self.http.request.side_effect = [
            response(tokens()),
            response({"status": "processing"}),
        ]
        self.assertFalse(self.uploader.upload("ride", self.fit).success)

    def test_malformed_response_is_controlled_failure(self):
        self.http.request.return_value = response([])
        self.assertFalse(self.uploader.upload("ride", self.fit).success)
        self.http.request.return_value.json.side_effect = ValueError("invalid JSON")
        self.assertFalse(self.uploader.upload("ride", self.fit).success)

    def test_service_failure_is_independent_and_retry_skips_successful_garmin(self):
        garmin = FakeGarmin()
        strava = mock_strava()
        strava.upload.return_value = UploadResult(
            False, False, True, None, "authorization required"
        )
        service = SyncService(
            self.settings,
            self.ledger,
            FakeOneLap([{"id": "ride"}]),
            garmin,
            strava=strava,
        )
        self.assertEqual(service.run(), 1)
        rows = {
            r["target_region"]: r["status"]
            for r in self.ledger.connection.execute("SELECT * FROM uploads")
        }
        self.assertEqual(
            rows, {"CN": "success", "GLOBAL": "success", "STRAVA": "auth_required"}
        )
        strava.upload.return_value = UploadResult(True, False, False, "66", "ok")
        self.assertEqual(service.run(retry_auth=True), 0)
        self.assertEqual(len(garmin.uploads), 2)
        self.assertEqual(strava.upload.call_count, 2)

    def test_disabled_service_never_initializes_or_calls_strava(self):
        settings = replace(self.settings, strava_sync_enabled=False)
        with patch("onelap_garmin_sync.service.StravaUploader") as factory:
            service = SyncService(
                settings, self.ledger, FakeOneLap([{"id": "ride"}]), FakeGarmin()
            )
            self.assertEqual(service.run(), 0)
            factory.assert_not_called()
        self.assertNotIn(
            "STRAVA", {row["target_region"] for row in self.ledger.summary()}
        )

    def test_dry_run_enabled_without_credentials_never_initializes_uploader(self):
        settings = replace(
            self.settings, dry_run=True, strava_client_secret="", strava_session_key=""
        )
        settings.validate_for_strava()
        with patch("onelap_garmin_sync.service.StravaUploader") as factory:
            garmin = FakeGarmin()
            service = SyncService(
                settings, self.ledger, FakeOneLap([{"id": "ride"}]), garmin
            )
            self.assertEqual(service.run(), 0)
            factory.assert_not_called()
            self.assertEqual(garmin.uploads, [])
        self.assertEqual({row["status"] for row in self.ledger.summary()}, {"pending"})

    def test_disabling_leaves_strava_failures_out_of_run(self):
        strava = mock_strava()
        strava.upload.return_value = UploadResult(
            False, False, False, None, "unavailable"
        )
        service = SyncService(
            self.settings,
            self.ledger,
            FakeOneLap([{"id": "ride"}]),
            FakeGarmin(),
            strava=strava,
        )
        self.assertEqual(service.run(), 1)
        service.settings = replace(self.settings, strava_sync_enabled=False)
        self.assertEqual(service.run(), 0)
        strava.upload.assert_called_once()

    def test_enabling_adds_strava_task_for_known_activity(self):
        settings = replace(self.settings, strava_sync_enabled=False)
        garmin = FakeGarmin()
        service = SyncService(
            settings, self.ledger, FakeOneLap([{"id": "ride"}]), garmin
        )
        self.assertEqual(service.run(), 0)
        service.settings = self.settings
        service.strava = mock_strava()
        service.strava.upload.return_value = UploadResult(
            True, True, False, "66", "duplicate"
        )
        self.assertEqual(service.run(), 0)
        self.assertEqual(len(garmin.uploads), 2)
        self.assertEqual(self.ledger.summary()[-1]["status"], "duplicate")


class StravaConfigurationTest(unittest.TestCase):
    def test_default_off_and_explicit_enable(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
            self.assertFalse(settings.strava_sync_enabled)
            self.assertEqual(settings.upload_targets, settings.targets)
            settings.validate_for_strava()
        with patch.dict(
            os.environ,
            {
                "SYNC_TARGETS": "CN,GLOBAL",
                "STRAVA_SYNC_ENABLED": "true",
                "STRAVA_CLIENT_ID": "123",
            },
            clear=True,
        ):
            settings = Settings.from_env()
            self.assertEqual(settings.upload_targets, ("CN", "GLOBAL", "STRAVA"))
            with self.assertRaisesRegex(ValueError, "STRAVA_CLIENT_SECRET"):
                settings.validate_for_strava()
        with patch.dict(os.environ, {"STRAVA_SYNC_ENABLED": "treu"}, clear=True):
            with self.assertRaisesRegex(ValueError, "STRAVA_SYNC_ENABLED"):
                Settings.from_env()

    def test_strava_preserves_friend_global_and_auto_region_selection(self):
        for strategy in ("GLOBAL", "AUTO"):
            with self.subTest(strategy=strategy), patch.dict(
                os.environ,
                {
                    "SYNC_TARGETS": strategy,
                    "GARMIN_GLOBAL_OAUTH1": "{}",
                    "GARMIN_GLOBAL_OAUTH2": "{}",
                    "STRAVA_SYNC_ENABLED": "true",
                },
                clear=True,
            ):
                settings = Settings.from_env()
                self.assertEqual(settings.targets, ("GLOBAL",))
                self.assertEqual(settings.upload_targets, ("GLOBAL", "STRAVA"))

    def test_redirect_validates_state_scope_and_callback(self):
        query = {
            "state": "test-state",
            "scope": "read,activity:read_all,activity:write",
            "code": "auth-code",
        }
        callback = "http://localhost/strava-callback?"
        self.assertEqual(
            parse_redirect(callback + urlencode(query), "test-state"), "auth-code"
        )
        for change in (
            {"state": "wrong"},
            {"scope": "read"},
            {"scope": "activity:write"},
            {"scope": "activity:read,activity:write"},
            {"scope": "activity:read_all"},
            {"error": "denied"},
            {"code": ""},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_redirect(callback + urlencode({**query, **change}), "test-state")
        with self.assertRaises(ValueError):
            parse_redirect("https://example.com/?" + urlencode(query), "test-state")

    def test_old_target_constraint_migrates_without_losing_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.db"
            ledger = Ledger(path)
            ledger.upsert_activity("ride", "2026-10-22", "ride", "{}", ("CN", "GLOBAL"))
            ledger.mark_upload_started("ride", "CN")
            ledger.mark_upload_success("ride", "CN", "123")
            schema = ledger.connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='uploads'"
            ).fetchone()[0]
            rows = [dict(r) for r in ledger.connection.execute("SELECT * FROM uploads")]
            ledger.connection.execute("DROP TABLE uploads")
            ledger.connection.execute(
                schema.replace("'CN', 'GLOBAL', 'STRAVA'", "'CN', 'GLOBAL'")
            )
            for row in rows:
                columns = ",".join(row)
                ledger.connection.execute(
                    f"INSERT INTO uploads ({columns}) VALUES ({','.join('?' for _ in row)})",
                    list(row.values()),
                )
            ledger.connection.commit()
            ledger.close()
            # 故障注入：建临时表和复制数据后失败，所有 DDL/DML 均应回滚。
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            probe = Ledger.__new__(Ledger)
            probe.connection = Mock(wraps=connection)

            def fail_before_drop(sql, *args):
                if sql == "DROP TABLE uploads":
                    raise sqlite3.OperationalError("injected migration failure")
                return connection.execute(sql, *args)

            probe.connection.execute.side_effect = fail_before_drop
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
                probe._migrate_strava_target()
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='uploads_with_strava'"
                ).fetchone()
            )
            self.assertEqual(
                [dict(r) for r in connection.execute("SELECT * FROM uploads")], rows
            )
            connection.close()

            migrated = Ledger(path)
            self.assertEqual(
                [dict(r) for r in migrated.connection.execute("SELECT * FROM uploads")],
                rows,
            )
            self.assertEqual(
                migrated.connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            migrated.upsert_activity("ride", "2026-10-22", "ride", "{}", ("STRAVA",))
            self.assertEqual(
                migrated.due_targets("ride", ("STRAVA",), 8, False), ["STRAVA"]
            )
            migrated.close()
            reopened = Ledger(path)
            self.assertEqual(len(reopened.summary()), 3)
            reopened.close()
