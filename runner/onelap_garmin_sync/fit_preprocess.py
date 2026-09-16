from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

from .config import GarminDeviceProfile
from .onelap import sha256_file

LOGGER = logging.getLogger(__name__)

POSITION_FIELD_PAIRS = (
    ("position_lat", "position_long"),
    ("start_position_lat", "start_position_long"),
    ("end_position_lat", "end_position_long"),
    ("nec_lat", "nec_long"),
    ("swc_lat", "swc_long"),
)

PI = 3.1415926535897932384626
EARTH_AXIS = 6378245.0
ECCENTRICITY_SQUARED = 0.00669342162296594323
COORDINATE_ABS_TOLERANCE = 1e-6

CoordinateSnapshot = tuple[tuple[str, str, float, float], ...]


@lru_cache(maxsize=None)
def _configured_fit_editor_type(base_editor: type[Any]) -> type[Any]:
    class ConfiguredGarminFitEditor(base_editor):
        def _should_modify_manufacturer(
            self,
            manufacturer: int | None,
        ) -> bool:
            # An explicit project profile must override the upstream source
            # manufacturer allowlist when rebuilding FileIdMessage.
            return True

    return ConfiguredGarminFitEditor


@dataclass(frozen=True)
class FitSummary:
    record_count: int
    timestamp_count: int
    heart_rate_count: int
    power_count: int
    cadence_count: int
    metrics_sha256: str


@dataclass(frozen=True)
class FitProcessingResult:
    path: Path
    sha256: str
    summary: FitSummary
    product_id: int | None
    software_version: int | None
    garminize_enabled: bool
    coordinate_transform_enabled: bool
    coordinate_points_converted: int


def _out_of_china(longitude: float, latitude: float) -> bool:
    return not (
        73.66 < longitude < 135.05
        and 3.86 < latitude < 53.55
    )


def _transform_latitude(longitude: float, latitude: float) -> float:
    result = (
        -100.0
        + 2.0 * longitude
        + 3.0 * latitude
        + 0.2 * latitude * latitude
        + 0.1 * longitude * latitude
        + 0.2 * math.sqrt(abs(longitude))
    )
    result += (
        20.0 * math.sin(6.0 * longitude * PI)
        + 20.0 * math.sin(2.0 * longitude * PI)
    ) * 2.0 / 3.0
    result += (
        20.0 * math.sin(latitude * PI)
        + 40.0 * math.sin(latitude / 3.0 * PI)
    ) * 2.0 / 3.0
    result += (
        160.0 * math.sin(latitude / 12.0 * PI)
        + 320.0 * math.sin(latitude * PI / 30.0)
    ) * 2.0 / 3.0
    return result


def _transform_longitude(longitude: float, latitude: float) -> float:
    result = (
        300.0
        + longitude
        + 2.0 * latitude
        + 0.1 * longitude * longitude
        + 0.1 * longitude * latitude
        + 0.1 * math.sqrt(abs(longitude))
    )
    result += (
        20.0 * math.sin(6.0 * longitude * PI)
        + 20.0 * math.sin(2.0 * longitude * PI)
    ) * 2.0 / 3.0
    result += (
        20.0 * math.sin(longitude * PI)
        + 40.0 * math.sin(longitude / 3.0 * PI)
    ) * 2.0 / 3.0
    result += (
        150.0 * math.sin(longitude / 12.0 * PI)
        + 300.0 * math.sin(longitude / 30.0 * PI)
    ) * 2.0 / 3.0
    return result


def gcj02_to_wgs84(
    longitude: float,
    latitude: float,
) -> tuple[float, float]:
    if _out_of_china(longitude, latitude):
        return longitude, latitude

    latitude_delta = _transform_latitude(
        longitude - 105.0,
        latitude - 35.0,
    )
    longitude_delta = _transform_longitude(
        longitude - 105.0,
        latitude - 35.0,
    )
    latitude_radians = latitude / 180.0 * PI
    magic = math.sin(latitude_radians)
    magic = 1 - ECCENTRICITY_SQUARED * magic * magic
    sqrt_magic = math.sqrt(magic)
    latitude_delta = (latitude_delta * 180.0) / (
        (EARTH_AXIS * (1 - ECCENTRICITY_SQUARED))
        / (magic * sqrt_magic)
        * PI
    )
    longitude_delta = (longitude_delta * 180.0) / (
        EARTH_AXIS
        / sqrt_magic
        * math.cos(latitude_radians)
        * PI
    )
    return (
        longitude * 2 - (longitude + longitude_delta),
        latitude * 2 - (latitude + latitude_delta),
    )


class FitPreprocessor:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_fit_dependencies() -> tuple[Any, Any, Any, Any, Any, Any]:
        try:
            from fit_file_faker.fit_editor import FitEditor
            from fit_file_faker.utils import apply_fit_tool_patch
            from fit_file_faker.vendor.fit_tool.definition_message import (
                DefinitionMessage,
            )
            from fit_file_faker.vendor.fit_tool.fit_file import FitFile
            from fit_file_faker.vendor.fit_tool.fit_file_builder import (
                FitFileBuilder,
            )
            from fit_file_faker.vendor.fit_tool.profile.messages.file_id_message import (
                FileIdMessage,
            )
            from fit_file_faker.vendor.fit_tool.profile.messages.record_message import (
                RecordMessage,
            )
        except ImportError as exc:
            raise RuntimeError(
                "启用 FIT 预处理需要 Python 3.12+ 和 "
                "fit-file-faker==2.2.1"
            ) from exc

        apply_fit_tool_patch()
        return (
            FitEditor,
            FitFile,
            FitFileBuilder,
            DefinitionMessage,
            FileIdMessage,
            RecordMessage,
        )

    @staticmethod
    @contextmanager
    def _suppress_sensitive_upstream_debug() -> Iterator[None]:
        # Fit File Faker logs the configured serial number at DEBUG level.
        upstream_logger = logging.getLogger("garmin")
        previous_level = upstream_logger.level
        if previous_level < logging.INFO:
            upstream_logger.setLevel(logging.INFO)
        try:
            yield
        finally:
            upstream_logger.setLevel(previous_level)

    @classmethod
    def _parse_fit(cls, path: Path) -> Any:
        _, FitFile, _, _, _, _ = cls._load_fit_dependencies()
        return FitFile.from_file(str(path))

    @classmethod
    def _summarize_fit_file(cls, fit_file: Any) -> FitSummary:
        _, _, _, _, _, RecordMessage = cls._load_fit_dependencies()
        metrics: list[tuple[str, str, str, str]] = []
        timestamp_count = 0
        heart_rate_count = 0
        power_count = 0
        cadence_count = 0
        for record in fit_file.records:
            message = record.message
            if not isinstance(message, RecordMessage):
                continue
            timestamp = getattr(message, "timestamp", None)
            heart_rate = getattr(message, "heart_rate", None)
            power = getattr(message, "power", None)
            cadence = getattr(message, "cadence", None)
            metrics.append(
                tuple(
                    "" if value is None else str(value)
                    for value in (timestamp, heart_rate, power, cadence)
                )
            )
            timestamp_count += int(timestamp is not None)
            heart_rate_count += int(heart_rate not in (None, 0))
            power_count += int(power not in (None, 0))
            cadence_count += int(cadence not in (None, 0))

        fingerprint = hashlib.sha256(
            json.dumps(metrics, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return FitSummary(
            record_count=len(metrics),
            timestamp_count=timestamp_count,
            heart_rate_count=heart_rate_count,
            power_count=power_count,
            cadence_count=cadence_count,
            metrics_sha256=fingerprint,
        )

    @classmethod
    def summarize(cls, path: Path) -> FitSummary:
        return cls._summarize_fit_file(cls._parse_fit(path))

    @staticmethod
    def _convert_coordinates(fit_file: Any) -> int:
        converted_count = 0
        for record in fit_file.records:
            message = record.message
            for latitude_name, longitude_name in POSITION_FIELD_PAIRS:
                if not hasattr(message, latitude_name) or not hasattr(
                    message, longitude_name
                ):
                    continue
                latitude = getattr(message, latitude_name)
                longitude = getattr(message, longitude_name)
                if latitude is None or longitude is None:
                    continue
                converted_longitude, converted_latitude = gcj02_to_wgs84(
                    float(longitude),
                    float(latitude),
                )
                if (
                    converted_latitude == latitude
                    and converted_longitude == longitude
                ):
                    continue
                setattr(message, latitude_name, converted_latitude)
                setattr(message, longitude_name, converted_longitude)
                converted_count += 1
        return converted_count

    @staticmethod
    def _coordinate_snapshot(fit_file: Any) -> CoordinateSnapshot:
        coordinates: list[tuple[str, str, float, float]] = []
        for record in fit_file.records:
            message = record.message
            for latitude_name, longitude_name in POSITION_FIELD_PAIRS:
                if not hasattr(message, latitude_name) or not hasattr(
                    message, longitude_name
                ):
                    continue
                latitude = getattr(message, latitude_name)
                longitude = getattr(message, longitude_name)
                if latitude is None or longitude is None:
                    continue
                coordinates.append(
                    (
                        latitude_name,
                        longitude_name,
                        float(latitude),
                        float(longitude),
                    )
                )
        return tuple(coordinates)

    @staticmethod
    def _validate_coordinates(
        expected: CoordinateSnapshot,
        actual: CoordinateSnapshot,
    ) -> None:
        if len(actual) != len(expected):
            raise RuntimeError(
                "处理前后 FIT 坐标对数量不一致: "
                f"{len(expected)} -> {len(actual)}"
            )

        for index, (expected_item, actual_item) in enumerate(
            zip(expected, actual),
            start=1,
        ):
            (
                expected_latitude_name,
                expected_longitude_name,
                expected_latitude,
                expected_longitude,
            ) = expected_item
            (
                actual_latitude_name,
                actual_longitude_name,
                actual_latitude,
                actual_longitude,
            ) = actual_item
            if (
                actual_latitude_name != expected_latitude_name
                or actual_longitude_name != expected_longitude_name
            ):
                raise RuntimeError(
                    f"处理后第 {index} 个坐标字段顺序发生变化"
                )
            if not (
                math.isclose(
                    actual_latitude,
                    expected_latitude,
                    rel_tol=0.0,
                    abs_tol=COORDINATE_ABS_TOLERANCE,
                )
                and math.isclose(
                    actual_longitude,
                    expected_longitude,
                    rel_tol=0.0,
                    abs_tol=COORDINATE_ABS_TOLERANCE,
                )
            ):
                raise RuntimeError(
                    f"处理后第 {index} 个坐标未按预期写入"
                )

    @classmethod
    def _write_coordinate_only(cls, fit_file: Any, output: Path) -> None:
        FitEditor, _, FitFileBuilder, DefinitionMessage, _, _ = (
            cls._load_fit_dependencies()
        )
        editor = FitEditor()
        editor.strip_unknown_fields(fit_file)
        builder = FitFileBuilder(auto_define=True)
        for record in fit_file.records:
            if isinstance(record.message, DefinitionMessage):
                continue
            builder.add(record.message)
        builder.build().to_file(str(output))

    @classmethod
    def _validate_device_metadata(
        cls,
        fit_file: Any,
        profile: GarminDeviceProfile,
    ) -> None:
        _, _, _, _, FileIdMessage, _ = cls._load_fit_dependencies()
        for record in fit_file.records:
            message = record.message
            if not isinstance(message, FileIdMessage):
                continue
            if message.manufacturer != 1:
                raise RuntimeError(
                    f"处理后 manufacturer 不是 Garmin: {message.manufacturer}"
                )
            if message.product != profile.product_id:
                raise RuntimeError(
                    "处理后的 Garmin 产品 ID 与配置不一致: "
                    f"{message.product}"
                )
            if message.serial_number != profile.unit_id:
                raise RuntimeError("处理后的 Garmin Unit ID 与配置不一致")
            return
        raise RuntimeError("处理后的 FIT 缺少 FileIdMessage")

    def process(
        self,
        source_path: Path,
        record_id: str,
        *,
        garminize_enabled: bool,
        coordinate_transform_enabled: bool,
        profile: GarminDeviceProfile | None,
    ) -> FitProcessingResult:
        if not garminize_enabled and not coordinate_transform_enabled:
            raise ValueError("至少需要启用一种 FIT 预处理")
        if garminize_enabled:
            if profile is None:
                raise ValueError("Garmin 设备伪装已启用，但缺少设备配置")
            profile.validate()

        FitEditor, _, _, _, _, _ = self._load_fit_dependencies()
        fit_file = self._parse_fit(source_path)
        source_summary = self._summarize_fit_file(fit_file)
        converted_count = (
            self._convert_coordinates(fit_file)
            if coordinate_transform_enabled
            else 0
        )
        expected_coordinates = (
            self._coordinate_snapshot(fit_file)
            if coordinate_transform_enabled
            else ()
        )

        safe_record_id = re.sub(r"[^A-Za-z0-9._-]+", "_", record_id)
        output_path = self.output_dir / f"{safe_record_id}.fit"
        part_path = output_path.with_suffix(".fit.part")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        part_path.unlink(missing_ok=True)

        try:
            if garminize_enabled:
                editor_profile = SimpleNamespace(
                    manufacturer=1,
                    device=profile.product_id,
                    serial_number=profile.unit_id,
                    software_version=profile.software_version,
                )
                with self._suppress_sensitive_upstream_debug():
                    editor_type = _configured_fit_editor_type(FitEditor)
                    editor = editor_type(profile=editor_profile)
                    result = editor.edit_fit(fit_file, output=part_path)
                if result is None or not part_path.exists():
                    raise RuntimeError("Fit File Faker 未生成输出文件")
            else:
                self._write_coordinate_only(fit_file, part_path)

            processed_fit = self._parse_fit(part_path)
            processed_summary = self._summarize_fit_file(processed_fit)
            if processed_summary.record_count != source_summary.record_count:
                raise RuntimeError(
                    "处理前后 FIT record 数量不一致: "
                    f"{source_summary.record_count} -> "
                    f"{processed_summary.record_count}"
                )
            if processed_summary.metrics_sha256 != source_summary.metrics_sha256:
                raise RuntimeError(
                    "处理前后时间戳、心率、功率或踏频记录发生变化"
                )
            if coordinate_transform_enabled:
                self._validate_coordinates(
                    expected_coordinates,
                    self._coordinate_snapshot(processed_fit),
                )
            if garminize_enabled:
                self._validate_device_metadata(processed_fit, profile)
            os.replace(part_path, output_path)
        except Exception:
            part_path.unlink(missing_ok=True)
            raise

        return FitProcessingResult(
            path=output_path,
            sha256=sha256_file(output_path),
            summary=processed_summary,
            product_id=profile.product_id if profile else None,
            software_version=profile.software_version if profile else None,
            garminize_enabled=garminize_enabled,
            coordinate_transform_enabled=coordinate_transform_enabled,
            coordinate_points_converted=converted_count,
        )
