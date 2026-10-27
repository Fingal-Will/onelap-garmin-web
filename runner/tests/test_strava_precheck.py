import json
import os
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

from onelap_garmin_sync.reconcile import reconcile_activities
from onelap_garmin_sync.service import SyncService
from onelap_garmin_sync.strava import StravaError
from onelap_garmin_sync.strava_auth import main as authorize
from onelap_garmin_sync.strava_reconcile import (
    strava_activity, strava_candidate, utc_time,
)
from test_service import FakeGarmin
import test_strava as fixtures


def source(record_id="ride", **changes):
    return {
        "id": record_id, "name": "骑行",
        "start_riding_time": "2026-10-22 08:00:00",
        "time_seconds": 3600, "distance_km": 30, "sport_type": "cycling",
        **changes,
    }


def remote(activity_id=66, **changes):
    return {
        "id": activity_id, "name": "不同的活动名称",
        "start_date": "2026-10-22T00:00:30Z",
        "start_date_local": "2026-10-22T08:00:30Z",
        "elapsed_time": 3610, "moving_time": 2000,
        "distance": 30100, "sport_type": "VirtualRide", **changes,
    }


class StravaFingerprintTest(unittest.TestCase):
    def test_local_utc_offset_and_timestamp_times_agree(self):
        expected = datetime(2026, 10, 22)
        timestamp = int(expected.replace(tzinfo=timezone.utc).timestamp())
        for value in ("2026-10-22 08:00:00", "2026-10-22T08:00:00+08:00",
                      "2026-10-22T00:00:00Z", timestamp, timestamp * 1000):
            with self.subTest(value=value):
                self.assertEqual(utc_time(value, onelap=True), expected)

    def test_matching_uses_elapsed_time_and_utc_not_local_or_name(self):
        result = reconcile_activities(
            [strava_candidate("ride", source())], [strava_activity(remote())]
        )[0]
        self.assertEqual((result.status, result.remote_activity_id), ("present", "66"))

    def test_nonfinite_missing_and_invalid_remote_data_are_unverified(self):
        for changes in ({"distance": float("nan")}, {"elapsed_time": float("inf")},
                        {"elapsed_time": None}, {"id": "bad"},
                        {"start_date": "bad"}, {"distance": -1}):
            with self.subTest(changes=changes):
                result = reconcile_activities(
                    [strava_candidate("ride", source())],
                    [strava_activity(remote(**changes))],
                )[0]
                self.assertEqual(result.status, "unverified")

    def test_incompatible_sport_does_not_match(self):
        result = reconcile_activities(
            [strava_candidate("ride", source())],
            [strava_activity(remote(sport_type="Run"))],
        )[0]
        self.assertEqual(result.status, "missing")

    def test_missing_source_metrics_are_unverified(self):
        for changes in ({"time_seconds": None}, {"distance_km": None},
                        {"distance_km": float("nan")},
                        {"start_riding_time": "2026-10-22"}):
            with self.subTest(changes=changes):
                result = reconcile_activities(
                    [strava_candidate("ride", source(**changes))], []
                )[0]
                self.assertEqual(result.status, "unverified")

    def test_naive_time_does_not_follow_runner_timezone(self):
        original = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/New_York", "Asia/Shanghai"):
                os.environ["TZ"] = zone
                time.tzset()
                self.assertEqual(strava_candidate("ride", source()).start_time_local,
                                 datetime(2026, 10, 22))
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()


class StravaPrecheckTest(unittest.TestCase):
    setUp = fixtures.StravaTest.setUp
    tearDown = fixtures.StravaTest.tearDown

    def service(self, sources=None):
        self.onelap = fixtures.FakeOneLap(sources if sources is not None else [source()])
        self.onelap.download_fit = Mock(wraps=self.onelap.download_fit)
        self.garmin = FakeGarmin()
        return SyncService(self.settings, self.ledger, self.onelap, self.garmin,
                           strava=self.uploader)

    def preexisting_garmin(self):
        self.ledger.upsert_activity("ride", "2026-10-22T00:00:00Z", "骑行",
                                    json.dumps(source()), self.settings.upload_targets)
        for target in self.settings.targets:
            self.ledger.mark_upload_success("ride", target, target + "-1")

    def strava_row(self):
        return self.ledger.connection.execute(
            "SELECT * FROM uploads WHERE target_region='STRAVA'"
        ).fetchone()

    def http_responses(self, *payloads):
        self.http.request.side_effect = [fixtures.response(fixtures.tokens())] + [
            fixtures.response(payload) for payload in payloads
        ]

    def test_pages_until_empty_even_if_short_and_deduplicates(self):
        self.http_responses([remote()], [remote(), remote(67, distance=60000)], [])
        results = self.uploader.reconcile([strava_candidate("ride", source())])
        self.assertEqual(results[0].status, "present")
        calls = self.http.request.call_args_list[1:]
        self.assertEqual([call.kwargs["params"]["page"] for call in calls], [1, 2, 3])
        after = int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()) - 121
        for call in calls:
            self.assertEqual(call.args, ("GET", self.uploader.API + "/athlete/activities"))
            self.assertEqual(call.kwargs["params"], {
                "after": after, "before": after + 242,
                "page": call.kwargs["params"]["page"], "per_page": 200,
            })

    def test_existing_skips_fit_and_upload_and_second_run_is_local(self):
        self.preexisting_garmin()
        service = self.service()
        self.http_responses([remote()], [])
        with self.assertLogs("onelap_garmin_sync.service", level="INFO") as logs:
            self.assertEqual(service.run(), 0)
        self.assertIn("已存在 1 条（跳过上传）", " ".join(logs.output))
        row = self.strava_row()
        self.assertEqual((row["status"], row["remote_activity_id"], row["attempts"],
                          row["reconciliation_status"]), ("success", "66", 0, "present"))
        self.onelap.download_fit.assert_not_called()
        self.assertEqual(self.garmin.uploads, [])
        self.http.request.reset_mock()
        self.assertEqual(service.run(), 0)
        self.http.request.assert_not_called()

    def test_only_missing_is_submitted_in_a_mixed_batch(self):
        service = self.service([source(), source("new", start_riding_time="2026-10-22 10:00:00")])
        self.http_responses([remote()], [], {"activity_id": 77})
        self.assertEqual(service.run(), 0)
        submissions = [call for call in self.http.request.call_args_list
                       if call.args == ("POST", self.uploader.API + "/uploads")]
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0].kwargs["data"]["external_id"], "onelap-new.fit")
        self.assertEqual(len(self.garmin.uploads), 4)

    def test_failed_later_page_does_not_commit_partial_matches_or_upload(self):
        self.preexisting_garmin()
        service = self.service()
        self.http.request.side_effect = [fixtures.response(fixtures.tokens()),
                                        fixtures.response([remote()]), fixtures.response({}, 500)]
        self.assertEqual(service.run(), 1)
        row = self.strava_row()
        self.assertEqual((row["status"], row["attempts"], row["reconciliation_status"]),
                         ("pending", 0, "unverified"))
        self.onelap.download_fit.assert_not_called()

    def test_network_rate_auth_and_malformed_lists_block_strava_but_not_garmin(self):
        for failure in (fixtures.response({}, 401), fixtures.response({}, 403),
                        fixtures.response({}, 429), fixtures.response({}, 500),
                        fixtures.response({}), fixtures.response(["bad"]),
                        requests.Timeout("access-secret")):
            with self.subTest(failure=failure):
                self.uploader.tokens = fixtures.tokens()
                self.http.request.reset_mock()
                self.http.request.side_effect = [failure]
                service = self.service()
                with self.assertLogs("onelap_garmin_sync.service", level="ERROR") as logs:
                    self.assertEqual(service.run(), 1)
                self.assertNotIn("access-secret", " ".join(logs.output))
                self.assertEqual((self.strava_row()["attempts"], self.strava_row()["status"]),
                                 (0, "pending"))
                self.assertEqual(len(self.garmin.uploads), 2)
                self.assertEqual(self.http.request.call_count, 1)
                self.ledger.connection.execute(
                    "UPDATE uploads SET status='pending' WHERE target_region != 'STRAVA'"
                )
                self.ledger.connection.commit()

    def test_ambiguous_remote_matches_block_only_strava(self):
        service = self.service()
        self.http_responses([remote(), remote(67)], [])
        self.assertEqual(service.run(), 1)
        self.assertEqual(self.strava_row()["reconciliation_status"], "ambiguous")
        self.assertEqual(self.strava_row()["attempts"], 0)
        self.assertEqual(len(self.garmin.uploads), 2)
        self.assertTrue(all(call.args[0] == "GET"
                            for call in self.http.request.call_args_list[1:]))

    def test_two_candidates_cannot_claim_the_same_remote_activity(self):
        service = self.service([source(), source("ride-2")])
        self.http_responses([remote()], [])
        self.assertEqual(service.run(), 1)
        rows = self.ledger.connection.execute(
            "SELECT reconciliation_status, attempts FROM uploads WHERE target_region='STRAVA'"
        ).fetchall()
        self.assertEqual([row["reconciliation_status"] for row in rows], ["ambiguous"] * 2)
        self.assertEqual([row["attempts"] for row in rows], [0, 0])

    def test_unknown_source_data_never_causes_an_upload(self):
        service = self.service([{"id": "ride"}])
        self.assertEqual(service.run(), 1)
        self.http.request.assert_not_called()
        self.assertEqual(len(self.garmin.uploads), 2)
        self.assertEqual(self.strava_row()["reconciliation_status"], "unverified")

    def test_pagination_limit_is_not_treated_as_missing(self):
        self.uploader.MAX_LIST_PAGES = 2
        self.http_responses([remote()], [remote()])
        with self.assertRaisesRegex(StravaError, "分页上限"):
            self.uploader.reconcile([strava_candidate("ride", source())])

    def test_conflicting_page_snapshots_are_not_used(self):
        self.http_responses([remote()], [remote(distance=99999)])
        with self.assertRaisesRegex(StravaError, "发生变化"):
            self.uploader.reconcile([strava_candidate("ride", source())])

    def test_pending_upload_resumes_without_listing_or_resubmitting(self):
        self.ledger.set_metadata(f"{self.uploader.provider}:upload:ride", "55")
        service = self.service()
        self.http_responses({"activity_id": 66})
        self.assertEqual(service.run(), 0)
        self.assertEqual(self.http.request.call_args_list[-1].args,
                         ("GET", self.uploader.API + "/uploads/55"))
        self.assertEqual(self.http.request.call_count, 2)

    def test_force_reupload_explicitly_bypasses_precheck(self):
        service = self.service()
        self.http_responses({"activity_id": 66})
        self.assertEqual(service.run(record_id="ride", force_reupload=True), 0)
        self.assertEqual(self.http.request.call_args_list[-1].args,
                         ("POST", self.uploader.API + "/uploads"))

    def test_dry_run_never_queries_or_mutates_upload_state(self):
        service = self.service()
        service.settings = replace(self.settings, dry_run=True)
        self.assertEqual(service.run(), 0)
        self.http.request.assert_not_called()
        self.assertEqual(self.strava_row()["status"], "pending")
        self.assertIsNone(self.strava_row()["reconciliation_checked_at"])

    def test_unexpected_exception_is_sanitized_and_does_not_upload(self):
        service = self.service()
        with patch.object(self.uploader, "reconcile", side_effect=RuntimeError("access-secret")):
            with self.assertLogs("onelap_garmin_sync.service", level="ERROR") as logs:
                self.assertEqual(service.run(), 1)
        self.assertNotIn("access-secret", " ".join(logs.output))
        self.http.request.assert_not_called()
        self.assertEqual(len(self.garmin.uploads), 2)

    def test_auth_failure_expires_cached_token_for_next_run(self):
        self.uploader.tokens = fixtures.tokens()
        self.http.request.return_value = fixtures.response({}, 401)
        with self.assertRaises(StravaError):
            self.uploader.reconcile([strava_candidate("ride", source())])
        encrypted = self.ledger.get_auth_state(self.uploader.provider)
        self.assertEqual(json.loads(self.uploader.fernet.decrypt(encrypted))["expires_at"], 0)


class StravaAuthorizationTest(unittest.TestCase):
    def test_authorization_url_requests_read_all_and_write(self):
        with patch("builtins.input", return_value="123"), \
             patch("getpass.getpass", return_value="client-secret"), \
             patch("builtins.print") as printed, \
             patch("onelap_garmin_sync.strava_auth.parse_redirect", side_effect=ValueError("stop")):
            self.assertEqual(authorize(), 1)
        urls = [call.args[0] for call in printed.call_args_list
                if call.args and str(call.args[0]).startswith("https://www.strava.com/oauth/authorize?")]
        self.assertEqual(len(urls), 1)
        self.assertEqual(parse_qs(urlparse(urls[0]).query)["scope"],
                         ["activity:read_all,activity:write"])

    def test_upgrade_copies_token_without_generating_a_session_key(self):
        token_response = fixtures.response({"refresh_token": "replacement-token"})
        with patch("builtins.input", return_value="123"), \
             patch("getpass.getpass", return_value="local-input"), \
             patch("builtins.print") as printed, \
             patch("onelap_garmin_sync.strava_auth.parse_redirect", return_value="code"), \
             patch("onelap_garmin_sync.strava_auth.requests.post", return_value=token_response), \
             patch("onelap_garmin_sync.strava_auth.sys.platform", "darwin"), \
             patch("onelap_garmin_sync.strava_auth.subprocess.run") as clipboard, \
             patch("onelap_garmin_sync.strava_auth.secrets.token_urlsafe", return_value="state") as random:
            self.assertEqual(authorize(upgrade=True), 0)
        clipboard.assert_called_once_with(
            ["pbcopy"], input="replacement-token", text=True, check=True
        )
        random.assert_called_once_with(32)
        output = [str(call.args[0]) for call in printed.call_args_list if call.args]
        self.assertIn("STRAVA_REFRESH_TOKEN=replacement-token", output)
        self.assertFalse(any(line.startswith("STRAVA_SESSION_KEY=") for line in output))

    def test_upgrade_falls_back_to_manual_copy_if_clipboard_is_unavailable(self):
        token_response = fixtures.response({"refresh_token": "replacement-token"})
        with patch("builtins.input", return_value="123"), \
             patch("getpass.getpass", return_value="local-input"), \
             patch("builtins.print") as printed, \
             patch("onelap_garmin_sync.strava_auth.parse_redirect", return_value="code"), \
             patch("onelap_garmin_sync.strava_auth.requests.post", return_value=token_response), \
             patch("onelap_garmin_sync.strava_auth.sys.platform", "darwin"), \
             patch("onelap_garmin_sync.strava_auth.subprocess.run", side_effect=OSError("unavailable")):
            self.assertEqual(authorize(upgrade=True), 0)
        output = " ".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("请手动复制", output)
