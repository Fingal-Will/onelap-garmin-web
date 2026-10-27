import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).parents[1] / "scripts"
WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


def load_script(name):
    specification = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


prepare_request = load_script("prepare_request")
write_status = load_script("write_status")


class WorkflowCredentialIsolationTest(unittest.TestCase):
    def test_sync_workflows_expose_repository_token_only_while_persisting(self):
        for filename in ("sync.yml", "reconcile.yml"):
            with self.subTest(workflow=filename):
                workflow = (WORKFLOWS / filename).read_text(encoding="utf-8")
                persist_marker = "- name: 保存台账与汇总"
                persist_start = workflow.index(persist_marker)
                before_persist = workflow[:persist_start]
                persist_step = workflow[persist_start:]

                self.assertIn("group: onelap-garmin-ledger", workflow)
                self.assertIn("persist-credentials: false", workflow)
                self.assertNotIn("persist-credentials: true", workflow)
                self.assertNotIn("git pull", before_persist)
                self.assertNotRegex(
                    before_persist,
                    r"github\.token|GITHUB_TOKEN|REPOSITORY_TOKEN",
                )
                self.assertEqual(workflow.count("${{ github.token }}"), 1)
                self.assertIn(
                    "REPOSITORY_TOKEN: ${{ github.token }}",
                    persist_step,
                )
                self.assertIn(
                    "http.https://github.com/.extraheader",
                    persist_step,
                )
                self.assertIn(
                    "git config --local --unset-all "
                    "http.https://github.com/.extraheader",
                    persist_step,
                )
                self.assertIn("unset REPOSITORY_TOKEN basic_auth", persist_step)
                self.assertIn("trap cleanup_git_credentials EXIT", persist_step)
                self.assertIn(
                    'git pull --ff-only "$remote" "$DEFAULT_BRANCH"',
                    persist_step,
                )
                self.assertIn(
                    'git push "$remote" "HEAD:$DEFAULT_BRANCH"',
                    persist_step,
                )
                self.assertNotIn("git push --force", persist_step)


class PrepareRequestTest(unittest.TestCase):
    def prepare(self, event, environment, event_name="workflow_dispatch", workflow="sync"):
        marker = Mock()
        marker.read_text.return_value = (
            '{"project":"onelap-garmin-web","schema_version":1}'
        )
        with patch.object(prepare_request, "Path", return_value=marker):
            return prepare_request.prepare(event, event_name, workflow, environment)

    def test_manual_request_is_allowlisted_before_writing_environment(self):
        event = {
            "repository": {"private": True, "default_branch": "main"},
            "inputs": {
                "target_regions": "CN",
                "discovery_pages": "3",
                "full_scan": "true",
                "retry_auth": "false",
                "record_id": "ride_1",
                "force_reupload": "true",
                "dry_run": "false",
                "request_id": "request_1",
            },
        }
        environment = {
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_TARGETS": "GLOBAL",
            "GARMINIZE_FIT_ENABLED": "true",
            "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
        }
        values = self.prepare(event, environment)
        self.assertEqual(values["SYNC_TARGETS"], "CN")
        self.assertEqual(values["ONELAP_DISCOVERY_PAGES"], "3")
        self.assertEqual(values["MANUAL_RECORD_ID"], "ride_1")
        self.assertEqual(values["SYNC_DRY_RUN"], "false")

    def test_saved_default_applies_to_manual_sync_reconcile_and_schedule(self):
        event = {"repository": {"private": True, "default_branch": "main"}, "inputs": {"target_regions": "DEFAULT"}}
        env = {"GITHUB_REF": "refs/heads/main", "GARMINIZE_FIT_ENABLED": "true",
               "FIT_COORDINATE_TRANSFORM_ENABLED": "false", "DEFAULT_TARGETS": "GLOBAL", "SCHEDULE_ENABLED": "true"}
        for name, workflow in [("workflow_dispatch", "sync"), ("workflow_dispatch", "reconcile"), ("schedule", "sync")]:
            with self.subTest(name=name, workflow=workflow):
                values = self.prepare(event, env, name, workflow)
                self.assertEqual(values["SYNC_TARGETS"], "GLOBAL")
                self.assertEqual(values["SYNC_DRY_RUN"], "false" if name == "schedule" else "true")
        event["inputs"]["target_regions"] = "CN"
        self.assertEqual(self.prepare(event, env)["SYNC_TARGETS"], "CN")
        self.assertEqual(env["DEFAULT_TARGETS"], "GLOBAL")

    def test_missing_saved_default_auto_selects_international_only_and_rejects_no_accounts(self):
        event = {"repository": {"private": True, "default_branch": "main"}, "inputs": {}}
        env = {"GITHUB_REF": "refs/heads/main", "GARMINIZE_FIT_ENABLED": "true",
               "FIT_COORDINATE_TRANSFORM_ENABLED": "false", "SCHEDULE_ENABLED": "true",
               "GARMIN_GLOBAL_OAUTH1": "{}", "GARMIN_GLOBAL_OAUTH2": "{}", "GARMIN_CN_OAUTH1": "{}"}
        for name, workflow in [("workflow_dispatch", "sync"), ("workflow_dispatch", "reconcile"), ("schedule", "sync")]:
            with self.subTest(name=name, workflow=workflow):
                self.assertEqual(self.prepare(event, env, name, workflow)["SYNC_TARGETS"], "GLOBAL")
        env["GARMIN_GLOBAL_OAUTH2"] = ""
        with self.assertRaisesRegex(ValueError, "至少配置一个"):
            self.prepare(event, env)
        env["DEFAULT_TARGETS"] = "INVALID"
        with self.assertRaisesRegex(ValueError, "允许范围"):
            self.prepare(event, env)

    def test_saved_limits_apply_to_manual_and_schedule_with_explicit_overrides(self):
        event = {"repository": {"private": True, "default_branch": "main"}, "inputs": {}}
        env = {"GITHUB_REF": "refs/heads/main", "GARMINIZE_FIT_ENABLED": "true",
               "FIT_COORDINATE_TRANSFORM_ENABLED": "false", "DEFAULT_TARGETS": "GLOBAL", "SCHEDULE_ENABLED": "true",
               "DEFAULT_DISCOVERY_PAGES": "0", "DEFAULT_RECENT_LIMIT": "50", "SYNC_MAX_RETRIES": "12"}
        for event_name, workflow in [("workflow_dispatch", "sync"), ("workflow_dispatch", "reconcile"), ("schedule", "sync")]:
            values = self.prepare(event, env, event_name, workflow)
            self.assertEqual(values["ONELAP_DISCOVERY_PAGES"], "0")
            self.assertEqual(values["RECONCILE_RECENT_LIMIT"], "50")
            self.assertEqual(values["SYNC_MAX_RETRIES"], "12")
        event["inputs"] = {"discovery_pages": "3", "recent_activities": "10"}
        values = self.prepare(event, env)
        self.assertEqual(values["ONELAP_DISCOVERY_PAGES"], "3")
        self.assertEqual(values["RECONCILE_RECENT_LIMIT"], "10")
        values = self.prepare(event, env, "schedule")
        self.assertEqual(values["ONELAP_DISCOVERY_PAGES"], "0")

    def test_global_dry_run_cannot_be_disabled_by_manual_or_schedule(self):
        event = {"repository": {"private": True, "default_branch": "main"}, "inputs": {"dry_run": "false"}}
        env = {"GITHUB_REF": "refs/heads/main", "GARMINIZE_FIT_ENABLED": "true", "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
               "DEFAULT_TARGETS": "GLOBAL", "SCHEDULE_ENABLED": "true", "DEFAULT_DRY_RUN": "true"}
        for event_name, workflow in [("workflow_dispatch", "sync"), ("workflow_dispatch", "reconcile"), ("schedule", "sync")]:
            self.assertEqual(self.prepare(event, env, event_name, workflow)["SYNC_DRY_RUN"], "true")
        for key, value in [("SYNC_MAX_RETRIES", "0"), ("DEFAULT_RECENT_LIMIT", "201"),
                           ("DEFAULT_DISCOVERY_PAGES", "1\nINJECT=true"), ("DEFAULT_DRY_RUN", "invalid")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.prepare(event, {**env, key: value})

    def test_rejects_non_private_or_non_default_branch_requests(self):
        event = {
            "repository": {"private": False, "default_branch": "main"},
            "inputs": {},
        }
        environment = {
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_TARGETS": "GLOBAL",
            "GARMINIZE_FIT_ENABLED": "true",
            "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
        }
        with self.assertRaisesRegex(ValueError, "私有仓库"):
            self.prepare(event, environment)

        event["repository"]["private"] = True
        environment["GITHUB_REF"] = "refs/heads/feature"
        with self.assertRaisesRegex(ValueError, "默认分支"):
            self.prepare(event, environment)

    def test_rejects_reupload_without_record_identifier(self):
        event = {
            "repository": {"private": True, "default_branch": "main"},
            "inputs": {"force_reupload": "true"},
        }
        environment = {
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_TARGETS": "GLOBAL",
            "GARMINIZE_FIT_ENABLED": "true",
            "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
        }
        with self.assertRaisesRegex(ValueError, "活动编号"):
            self.prepare(event, environment)


class WriteStatusTest(unittest.TestCase):
    def test_private_activity_report_uses_real_schema_and_limits_metadata(self):
        from onelap_garmin_sync.ledger import Ledger
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "sync.db"
            ledger = Ledger(database)
            try:
                for index in range(205):
                    ledger.connection.execute(
                        "INSERT INTO activities (source_record_id, name, activity_time, source_json, "
                        "fit_path, fit_sha256, download_status, processing_status, download_error, "
                        "garmin_product_id, discovered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"record-{index:04}", "测试骑行", "2026-10-26T02:00:00+00:00", '{"token":"source-secret"}',
                         "/private/files/secret.fit", "a" * 64, "success", "success", "raw-download-error", 4305,
                         "2026-10-26T02:00:00+00:00", "2026-10-26T02:00:00+00:00"),
                    )
                for region, status in [("CN", "success"), ("GLOBAL", "auth_required"), ("STRAVA", "uploading")]:
                    ledger.connection.execute(
                        "INSERT INTO uploads (source_record_id, target_region, status, attempts, remote_activity_id, "
                        "last_error, reconciliation_status, reconciliation_checked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("record-0204", region, status, 1, "123" if region == "CN" else None,
                         "raw-auth-secret", "unverified", "2026-10-26T02:00:00+00:00", "2026-10-26T02:00:00+00:00"),
                    )
                ledger.connection.commit()
                status = write_status.build_status({"WEB_WORKFLOW": "sync", "MANUAL_RECORD_ID": "record-0204"}, database)
                self.assertEqual(status["run"]["record_id"], "record-0204")
                self.assertEqual(status["schema_version"], 2)
                self.assertEqual(status["ledger"]["activities"], 205)
                self.assertEqual(len(status["activity_items"]), 200)
                activity = status["activity_items"][0]
                self.assertEqual(activity["id"], "record-0204")
                self.assertEqual(activity["name"], "测试骑行")
                self.assertEqual(activity["fit_sha256"], "a" * 64)
                self.assertEqual(activity["targets"]["CN"]["remote_activity_id"], "123")
                self.assertEqual(activity["targets"]["GLOBAL"]["status"], "auth_required")
                self.assertEqual(activity["targets"]["STRAVA"]["status"], "uploading")
                serialized = json.dumps(status)
                for forbidden in ["source-secret", "secret.fit", "raw-download-error", "raw-auth-secret", "garmin_product_id", "source_json"]:
                    self.assertNotIn(forbidden, serialized)
                self.assertEqual(ledger.connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 205)
            finally:
                ledger.close()

    def test_missing_ledger_reports_unavailable_instead_of_claiming_no_activities(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = write_status.build_status({}, Path(temporary) / "missing.db")
        self.assertFalse(status["ledger"]["available"])
        self.assertEqual(status["activity_items"], [])

    def test_status_uses_fixed_values_and_never_includes_untrusted_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "sync.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("CREATE TABLE activities (name TEXT)")
                connection.execute(
                    "INSERT INTO activities VALUES (?)", ("private ride name",)
                )
                connection.execute(
                    "CREATE TABLE uploads (target_region TEXT, status TEXT, reconciliation_status TEXT)"
                )
                connection.execute(
                    "INSERT INTO uploads VALUES ('CN', 'success', 'present')"
                )
                connection.execute(
                    "INSERT INTO uploads VALUES ('UNTRUSTED', 'secret', 'secret')"
                )
                connection.execute("INSERT INTO uploads VALUES ('STRAVA', 'duplicate', 'present')")
                connection.commit()
            finally:
                connection.close()
            status = write_status.build_status(
                {
                    "WEB_JOB_STATUS": "success",
                    "WEB_WORKFLOW": "sync",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_RUN_ID": "123",
                    "WEB_REQUEST_ID": "safe_request",
                    "SYNC_DRY_RUN": "false",
                    "SYNC_TARGETS": "CN,UNTRUSTED,GLOBAL",
                    "STRAVA_SYNC_ENABLED": "true",
                },
                database,
            )

        self.assertEqual(status["run"]["status"], "success")
        self.assertEqual(status["run"]["targets"], ["CN", "GLOBAL", "STRAVA"])
        self.assertFalse(status["run"]["dry_run"])
        self.assertEqual(status["ledger"]["activities"], 1)
        self.assertEqual(status["ledger"]["uploads"], [
            {"target_region": "CN", "status": "success", "count": 1},
            {"target_region": "STRAVA", "status": "duplicate", "count": 1},
        ])
        self.assertEqual(status["ledger"]["reconciliation"], [
            {"target_region": "CN", "status": "present", "count": 1},
            {"target_region": "STRAVA", "status": "present", "count": 1},
        ])
        self.assertEqual(status["activity_items"], [])  # Legacy schema remains readable as aggregates.
        self.assertNotIn("private ride name", json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
