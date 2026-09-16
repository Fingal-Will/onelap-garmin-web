import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from onelap_garmin_sync.config import (
    GarminDeviceProfile,
    Settings,
    parse_bool,
    parse_json_secret,
)


class ConfigTest(unittest.TestCase):
    def test_invalid_boolean_is_rejected_instead_of_disabling_dry_run(self):
        with self.assertRaisesRegex(ValueError, "SYNC_DRY_RUN"):
            parse_bool("treu", name="SYNC_DRY_RUN")

    def test_blank_boolean_uses_default(self):
        self.assertTrue(parse_bool(" ", True, name="SYNC_DRY_RUN"))

    def test_parse_base64_json_secret(self):
        encoded = base64.b64encode(json.dumps({"a": "b"}).encode()).decode()
        self.assertEqual(parse_json_secret(f"base64:{encoded}", "X"), {"a": "b"})

    def test_targets_are_normalized(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "SYNC_TARGETS": "global, cn",
                },
                clear=True,
            ):
                settings = Settings.from_env(Path(temporary))
        self.assertEqual(settings.targets, ("GLOBAL", "CN"))

    def test_garmin_device_profile_uses_one_shared_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "SYNC_TARGETS": "CN,GLOBAL",
                    "GARMIN_DEVICE_PRODUCT_ID": "3122",
                    "GARMIN_DEVICE_UNIT_ID": "1234567890",
                    "GARMIN_DEVICE_SOFTWARE_VERSION": "975",
                },
                clear=True,
            ):
                settings = Settings.from_env(Path(temporary))

        self.assertEqual(
            settings.require_device_profile(),
            GarminDeviceProfile(3122, 1234567890, 975),
        )

    def test_garminize_requires_shared_device_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploader = root / "uploader" / "garmin-upload.js"
            uploader.parent.mkdir()
            uploader.write_text("", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "ONELAP_TOKEN": "token",
                    "SYNC_TARGETS": "CN,GLOBAL",
                    "SYNC_DRY_RUN": "true",
                    "GARMINIZE_FIT_ENABLED": "true",
                    "GARMIN_DEVICE_PRODUCT_ID": "3122",
                },
                clear=True,
            ):
                settings = Settings.from_env(root)

            with self.assertRaisesRegex(ValueError, "同时设置"):
                settings.validate_for_sync()

    def test_device_profile_validation_does_not_echo_unit_id(self):
        profile = GarminDeviceProfile(product_id=3122, unit_id=999)
        with self.assertRaises(ValueError) as raised:
            profile.validate()
        self.assertNotIn("999", str(raised.exception))

    def test_coordinate_transform_enables_shared_preprocessing(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "FIT_COORDINATE_TRANSFORM_ENABLED": "true",
                },
                clear=True,
            ):
                settings = Settings.from_env(Path(temporary))

        self.assertTrue(settings.coordinate_transform_enabled)
        self.assertTrue(settings.preprocessing_enabled)
        self.assertEqual(settings.processed_dir.name, "processed")

    def test_live_reconcile_requires_upload_script(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            activity_reader = root / "uploader" / "garmin-activities.js"
            activity_reader.parent.mkdir()
            activity_reader.write_text("", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "ONELAP_TOKEN": "token",
                    "SYNC_TARGETS": "CN",
                    "SYNC_DRY_RUN": "false",
                    "GARMIN_CN_OAUTH1": "{}",
                    "GARMIN_CN_OAUTH2": "{}",
                },
                clear=True,
            ):
                settings = Settings.from_env(root)

            with self.assertRaisesRegex(ValueError, "Garmin 上传器不存在"):
                settings.validate_for_reconcile()

    def test_manual_tokens_are_independently_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "ONELAP_REFRESH_TOKEN": "refresh-only",
                },
                clear=True,
            ):
                settings = Settings.from_env(Path(temporary))

        settings.validate_for_onelap()
        self.assertEqual(settings.onelap_token, "")
        self.assertEqual(settings.onelap_refresh_token, "refresh-only")

    def test_password_is_not_stripped(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(
                os.environ,
                {
                    "ONELAP_ACCOUNT": " user@example.com ",
                    "ONELAP_PASSWORD": " password with spaces ",
                    "ONELAP_SESSION_KEY": "key",
                },
                clear=True,
            ):
                settings = Settings.from_env(Path(temporary))

        self.assertEqual(settings.onelap_account, "user@example.com")
        self.assertEqual(settings.onelap_password, " password with spaces ")
        self.assertEqual(settings.onelap_session_key, "key")


if __name__ == "__main__":
    unittest.main()
