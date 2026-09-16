import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from onelap_garmin_sync.config import GarminDeviceProfile
from onelap_garmin_sync.fit_preprocess import (
    FitPreprocessor,
    FitSummary,
    _configured_fit_editor_type,
    gcj02_to_wgs84,
)


class FakeEditor:
    def __init__(self, profile=None):
        self.profile = profile

    def edit_fit(self, fit_file, output):
        output.write_bytes(b"processed-fit")
        return output


class FitPreprocessTest(unittest.TestCase):
    def test_configured_editor_rewrites_every_source_manufacturer(self):
        class SelectiveEditor:
            def _should_modify_manufacturer(self, manufacturer):
                return manufacturer == 307

        editor_type = _configured_fit_editor_type(SelectiveEditor)
        editor = editor_type()

        for manufacturer in (None, 1, 107, 307):
            with self.subTest(manufacturer=manufacturer):
                self.assertTrue(
                    editor._should_modify_manufacturer(manufacturer)
                )

    def test_process_writes_one_activity_level_output(self):
        summary = FitSummary(10, 10, 8, 7, 6, "fingerprint")
        source_fit = SimpleNamespace(records=[])
        processed_fit = SimpleNamespace(records=[])
        dependencies = (
            FakeEditor,
            object(),
            object(),
            object(),
            object(),
            object(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.fit"
            source.write_bytes(b"fit")
            preprocessor = FitPreprocessor(root / "processed")
            profile = GarminDeviceProfile(4440, 1234567890, 2922)

            with (
                patch.object(
                    FitPreprocessor,
                    "_load_fit_dependencies",
                    return_value=dependencies,
                ),
                patch.object(
                    FitPreprocessor,
                    "_parse_fit",
                    side_effect=(source_fit, processed_fit),
                ),
                patch.object(
                    FitPreprocessor,
                    "_summarize_fit_file",
                    side_effect=(summary, summary),
                ),
                patch.object(
                    FitPreprocessor,
                    "_validate_device_metadata",
                ),
            ):
                result = preprocessor.process(
                    source,
                    "record/id",
                    garminize_enabled=True,
                    coordinate_transform_enabled=False,
                    profile=profile,
                )

            self.assertEqual(result.path.name, "record_id.fit")
            self.assertTrue(result.path.exists())
            self.assertEqual(result.product_id, 4440)
            self.assertEqual(result.software_version, 2922)
            self.assertFalse(result.coordinate_transform_enabled)
            self.assertFalse(
                result.path.with_suffix(".fit.part").exists()
            )

    def test_process_removes_partial_file_when_metrics_change(self):
        source_summary = FitSummary(10, 10, 8, 7, 6, "before")
        changed_summary = FitSummary(10, 10, 8, 7, 6, "after")
        dependencies = (
            FakeEditor,
            object(),
            object(),
            object(),
            object(),
            object(),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.fit"
            source.write_bytes(b"fit")
            preprocessor = FitPreprocessor(root / "processed")
            profile = GarminDeviceProfile(3122, 1234567890, 975)

            with (
                patch.object(
                    FitPreprocessor,
                    "_load_fit_dependencies",
                    return_value=dependencies,
                ),
                patch.object(
                    FitPreprocessor,
                    "_parse_fit",
                    side_effect=(
                        SimpleNamespace(records=[]),
                        SimpleNamespace(records=[]),
                    ),
                ),
                patch.object(
                    FitPreprocessor,
                    "_summarize_fit_file",
                    side_effect=(source_summary, changed_summary),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "心率、功率或踏频",
                ):
                    preprocessor.process(
                        source,
                        "ride",
                        garminize_enabled=True,
                        coordinate_transform_enabled=False,
                        profile=profile,
                    )

            output = root / "processed" / "ride.fit"
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".fit.part").exists())

    def test_process_rewrites_magene_file_id_with_real_dependency(self):
        try:
            (
                _,
                _,
                FitFileBuilder,
                _,
                FileIdMessage,
                RecordMessage,
            ) = FitPreprocessor._load_fit_dependencies()
        except RuntimeError as exc:
            self.skipTest(str(exc))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "magene.fit"

            file_id = FileIdMessage()
            file_id.type = 4
            file_id.manufacturer = 107
            file_id.product = 50
            file_id.serial_number = 987654321
            file_id.time_created = 1787670564000

            record = RecordMessage()
            record.timestamp = 1787670564000
            record.heart_rate = 120
            record.power = 200
            record.cadence = 80

            builder = FitFileBuilder(auto_define=True)
            builder.add(file_id)
            builder.add(record)
            builder.build().to_file(str(source))

            profile = GarminDeviceProfile(4305, 1234567890, 2621)
            result = FitPreprocessor(root / "processed").process(
                source,
                "magene",
                garminize_enabled=True,
                coordinate_transform_enabled=False,
                profile=profile,
            )

            processed_fit = FitPreprocessor._parse_fit(result.path)
            processed_file_id = next(
                record.message
                for record in processed_fit.records
                if isinstance(record.message, FileIdMessage)
            )
            self.assertEqual(processed_file_id.manufacturer, 1)
            self.assertEqual(processed_file_id.product, profile.product_id)
            self.assertEqual(
                processed_file_id.serial_number,
                profile.unit_id,
            )
            self.assertEqual(result.summary.record_count, 1)

    def test_gcj02_to_wgs84_converts_known_beijing_coordinate(self):
        longitude, latitude = gcj02_to_wgs84(116.404, 39.915)
        self.assertAlmostEqual(longitude, 116.397756, places=5)
        self.assertAlmostEqual(latitude, 39.913596, places=5)

    def test_gcj02_to_wgs84_leaves_overseas_coordinate_unchanged(self):
        self.assertEqual(
            gcj02_to_wgs84(-122.4194, 37.7749),
            (-122.4194, 37.7749),
        )

    def test_coordinate_pairs_are_mutated_on_parsed_messages(self):
        inside = SimpleNamespace(
            position_lat=39.915,
            position_long=116.404,
            start_position_lat=None,
            start_position_long=None,
        )
        outside = SimpleNamespace(
            position_lat=37.7749,
            position_long=-122.4194,
        )
        fit_file = SimpleNamespace(
            records=[
                SimpleNamespace(message=inside),
                SimpleNamespace(message=outside),
            ]
        )

        converted = FitPreprocessor._convert_coordinates(fit_file)

        self.assertEqual(converted, 1)
        self.assertAlmostEqual(inside.position_long, 116.397756, places=5)
        self.assertAlmostEqual(inside.position_lat, 39.913596, places=5)
        self.assertEqual(outside.position_long, -122.4194)
        self.assertEqual(outside.position_lat, 37.7749)

    def test_coordinate_snapshot_validation_accepts_fit_quantization(self):
        expected = (
            (
                "position_lat",
                "position_long",
                39.913595718,
                116.397755501,
            ),
        )
        actual = (
            (
                "position_lat",
                "position_long",
                39.913595617,
                116.397755388,
            ),
        )

        FitPreprocessor._validate_coordinates(expected, actual)

    def test_coordinate_snapshot_validation_rejects_unwritten_values(self):
        expected = (
            (
                "position_lat",
                "position_long",
                39.913596,
                116.397756,
            ),
        )
        actual = (
            (
                "position_lat",
                "position_long",
                39.915,
                116.404,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "未按预期写入"):
            FitPreprocessor._validate_coordinates(expected, actual)

    def test_coordinate_snapshot_validation_rejects_missing_pairs(self):
        expected = (
            (
                "position_lat",
                "position_long",
                39.913596,
                116.397756,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "坐标对数量不一致"):
            FitPreprocessor._validate_coordinates(expected, ())


if __name__ == "__main__":
    unittest.main()
