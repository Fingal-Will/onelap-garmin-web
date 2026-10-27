import json
import subprocess
import unittest
from unittest.mock import patch

from pathlib import Path

from onelap_garmin_sync.garmin import GarminUploader


class GarminUploaderTest(unittest.TestCase):
    @patch("onelap_garmin_sync.garmin.subprocess.run")
    def test_upload_timeout_returns_a_retryable_failure(self, run):
        run.side_effect = subprocess.TimeoutExpired(["node"], 180)
        uploader = GarminUploader(Path("uploader/garmin-upload.js"))

        result = uploader.upload("CN", Path("activity.fit"))

        self.assertFalse(result.success)
        self.assertFalse(result.auth_required)
        self.assertIn("超时", result.message)

    @patch("onelap_garmin_sync.garmin.subprocess.run")
    def test_activity_reader_start_failure_is_structured(self, run):
        run.side_effect = OSError("node missing")
        uploader = GarminUploader(Path("uploader/garmin-upload.js"))

        result = uploader.list_activities("GLOBAL", "2026-08-24", "2026-08-26")

        self.assertFalse(result.success)
        self.assertFalse(result.auth_required)
        self.assertIn("无法启动", result.message)

    def test_extracts_last_structured_result(self):
        output = "\n".join(
            [
                "library log",
                '{"type":"garmin-upload-result","ok":true,"duplicate":false}',
            ]
        )
        result = GarminUploader._extract_result(output)
        self.assertTrue(result["ok"])

    def test_recognizes_auth_error(self):
        self.assertTrue(GarminUploader._looks_like_auth_error("HTTP 401"))
        self.assertFalse(GarminUploader._looks_like_auth_error("HTTP 500"))

    @patch("onelap_garmin_sync.garmin.subprocess.run")
    def test_parses_activity_list_result(self, run):
        run.return_value.stdout = "\n".join(
            [
                "library log",
                '{"type":"garmin-activities-result","ok":true,'
                '"authRequired":false,"truncated":false,'
                '"activities":[{"activityId":"123"}],"message":"ok"}',
            ]
        )
        run.return_value.stderr = ""
        uploader = GarminUploader(Path("uploader/garmin-upload.js"))

        result = uploader.list_activities(
            "CN",
            "2026-08-24",
            "2026-08-26",
        )

        self.assertTrue(result.success)
        self.assertEqual(result.activities, [{"activityId": "123"}])
        self.assertFalse(result.truncated)

    @patch("onelap_garmin_sync.garmin.subprocess.run")
    def test_activity_stage_logs_survive_subprocess_capture(self, run):
        events = [
            {"type": "garmin-activities-stage", "stage": "load_token", "status": "completed", "elapsedMs": 5},
            {"type": "garmin-activities-stage", "stage": "user_profile", "status": "completed", "elapsedMs": 12},
            {"type": "garmin-activities-stage", "stage": "activities", "status": "started", "start": 100, "limit": 100},
            {"type": "garmin-activities-stage", "stage": "activities", "status": "failed",
             "elapsedMs": 5000, "httpStatus": 503, "code": "ECONNRESET", "data": "private-token"},
            {"type": "garmin-activities-result", "ok": False, "authRequired": False,
             "activities": [], "truncated": False, "message": "查询活动列表失败：Network error"},
        ]
        run.return_value.stdout = "library log\n" + "\n".join(json.dumps(event) for event in events)
        run.return_value.stderr = ""
        uploader = GarminUploader(Path("uploader/garmin-upload.js"))
        with self.assertLogs("onelap_garmin_sync.garmin", level="INFO") as captured:
            result = uploader.list_activities("CN", "2026-10-01", "2026-10-23")
        self.assertFalse(result.success)
        self.assertFalse(result.auth_required)
        self.assertEqual(result.message, "查询活动列表失败：Network error")
        logs = "\n".join(captured.output)
        self.assertIn("加载授权 / 完成", logs)
        self.assertIn("读取用户资料 / 完成", logs)
        self.assertIn("查询活动列表 / 开始（偏移=100，请求条数=100）", logs)
        self.assertIn("WARNING", logs)
        self.assertIn("耗时毫秒=5000，HTTP=503，错误码=ECONNRESET", logs)
        self.assertNotIn("private-token", logs)
        self.assertEqual(run.call_count, 1)

    @patch("onelap_garmin_sync.garmin.subprocess.run")
    def test_truncated_activity_list_is_not_successful(self, run):
        run.return_value.stdout = (
            '{"type":"garmin-activities-result","ok":false,'
            '"authRequired":false,"truncated":true,'
            '"activities":[],"message":"too many"}\n'
        )
        run.return_value.stderr = ""
        uploader = GarminUploader(Path("uploader/garmin-upload.js"))

        result = uploader.list_activities(
            "GLOBAL",
            "2026-08-24",
            "2026-08-26",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.truncated)


if __name__ == "__main__":
    unittest.main()
