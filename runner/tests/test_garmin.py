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
