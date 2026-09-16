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
    def prepare(self, event, environment):
        marker = Mock()
        marker.read_text.return_value = (
            '{"project":"onelap-garmin-web","schema_version":1}'
        )
        with patch.object(prepare_request, "Path", return_value=marker):
            return prepare_request.prepare(event, "workflow_dispatch", "sync", environment)

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
            "GARMINIZE_FIT_ENABLED": "true",
            "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
        }
        values = self.prepare(event, environment)
        self.assertEqual(values["SYNC_TARGETS"], "CN")
        self.assertEqual(values["ONELAP_DISCOVERY_PAGES"], "3")
        self.assertEqual(values["MANUAL_RECORD_ID"], "ride_1")
        self.assertEqual(values["SYNC_DRY_RUN"], "false")

    def test_rejects_non_private_or_non_default_branch_requests(self):
        event = {
            "repository": {"private": False, "default_branch": "main"},
            "inputs": {},
        }
        environment = {
            "GITHUB_REF": "refs/heads/main",
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
            "GARMINIZE_FIT_ENABLED": "true",
            "FIT_COORDINATE_TRANSFORM_ENABLED": "false",
        }
        with self.assertRaisesRegex(ValueError, "活动编号"):
            self.prepare(event, environment)


class WriteStatusTest(unittest.TestCase):
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
                },
                database,
            )

        self.assertEqual(status["run"]["status"], "success")
        self.assertEqual(status["run"]["targets"], ["CN", "GLOBAL"])
        self.assertFalse(status["run"]["dry_run"])
        self.assertEqual(status["ledger"]["activities"], 1)
        self.assertEqual(status["ledger"]["uploads"], [
            {"target_region": "CN", "status": "success", "count": 1},
        ])
        self.assertEqual(status["ledger"]["reconciliation"], [
            {"target_region": "CN", "status": "present", "count": 1},
        ])
        self.assertNotIn("private ride name", json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
