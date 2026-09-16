from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def parse_bool(
    value: str | None,
    default: bool = False,
    *,
    name: str = "布尔配置",
) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} 必须是 true/false、yes/no、on/off 或 1/0"
    )


def parse_json_secret(value: str, name: str) -> Any:
    text = value.strip()
    if text.startswith("base64:"):
        try:
            text = base64.b64decode(text[len("base64:") :]).decode("utf-8")
        except Exception as exc:
            raise ValueError(f"{name} 的 base64 内容无法解码") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 不是有效 JSON") from exc


def parse_optional_int(value: str | None, name: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc


@dataclass(frozen=True)
class GarminDeviceProfile:
    product_id: int
    unit_id: int
    software_version: int | None = None

    def validate(self) -> None:
        if not 1 <= self.product_id <= 65535:
            raise ValueError(
                "Garmin 产品 ID 超出 FIT uint16 范围: "
                f"{self.product_id}"
            )
        if not 1_000_000_000 <= self.unit_id <= 4_294_967_295:
            raise ValueError(
                "Garmin Unit ID 必须是 1000000000 到 "
                "4294967295 之间的整数"
            )
        if (
            self.software_version is not None
            and not 0 <= self.software_version <= 65535
        ):
            raise ValueError(
                "Garmin 固件版本超出 FIT uint16 范围: "
                f"{self.software_version}"
            )


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    download_dir: Path
    database_path: Path
    lock_path: Path
    onelap_token: str
    onelap_refresh_token: str
    onelap_account: str
    onelap_password: str
    onelap_session_key: str
    targets: tuple[str, ...]
    discovery_pages: int
    max_retries: int
    dry_run: bool
    uploader_path: Path
    garminize_fit_enabled: bool = False
    coordinate_transform_enabled: bool = False
    processed_dir: Path | None = None
    garmin_device_profile: GarminDeviceProfile | None = None

    @classmethod
    def from_env(cls, root_dir: Path | None = None) -> "Settings":
        root = (root_dir or Path.cwd()).resolve()
        configured_data_dir = Path(os.getenv("SYNC_DATA_DIR", "data"))
        data_dir = (
            configured_data_dir
            if configured_data_dir.is_absolute()
            else root / configured_data_dir
        ).resolve()

        raw_targets = os.getenv("SYNC_TARGETS", "CN,GLOBAL")
        targets = tuple(
            target.strip().upper()
            for target in raw_targets.split(",")
            if target.strip()
        )
        invalid_targets = sorted(set(targets) - {"CN", "GLOBAL"})
        if invalid_targets or not targets:
            raise ValueError(
                "SYNC_TARGETS 只能包含 CN、GLOBAL，当前值: "
                + (", ".join(invalid_targets) if invalid_targets else "空")
            )

        common_product_id = parse_optional_int(
            os.getenv("GARMIN_DEVICE_PRODUCT_ID"),
            "GARMIN_DEVICE_PRODUCT_ID",
        )
        common_unit_id = parse_optional_int(
            os.getenv("GARMIN_DEVICE_UNIT_ID"),
            "GARMIN_DEVICE_UNIT_ID",
        )
        common_software_version = parse_optional_int(
            os.getenv("GARMIN_DEVICE_SOFTWARE_VERSION"),
            "GARMIN_DEVICE_SOFTWARE_VERSION",
        )
        device_profile = None
        if common_product_id is not None and common_unit_id is not None:
            device_profile = GarminDeviceProfile(
                product_id=common_product_id,
                unit_id=common_unit_id,
                software_version=common_software_version,
            )

        return cls(
            root_dir=root,
            data_dir=data_dir,
            download_dir=data_dir / "downloads",
            database_path=Path(
                os.getenv("SYNC_DATABASE_PATH", str(data_dir / "sync.db"))
            ).resolve(),
            lock_path=Path(
                os.getenv("SYNC_LOCK_PATH", str(data_dir / "sync.lock"))
            ).resolve(),
            onelap_token=os.getenv("ONELAP_TOKEN", "").strip(),
            onelap_refresh_token=os.getenv(
                "ONELAP_REFRESH_TOKEN", ""
            ).strip(),
            onelap_account=os.getenv("ONELAP_ACCOUNT", "").strip(),
            # 密码可能有首尾空格，不能 strip。
            onelap_password=os.getenv("ONELAP_PASSWORD", ""),
            onelap_session_key=os.getenv("ONELAP_SESSION_KEY", ""),
            targets=targets,
            discovery_pages=max(0, int(os.getenv("ONELAP_DISCOVERY_PAGES", "10"))),
            max_retries=max(1, int(os.getenv("SYNC_MAX_RETRIES", "8"))),
            dry_run=parse_bool(
                os.getenv("SYNC_DRY_RUN"),
                False,
                name="SYNC_DRY_RUN",
            ),
            uploader_path=root / "uploader" / "garmin-upload.js",
            garminize_fit_enabled=parse_bool(
                os.getenv("GARMINIZE_FIT_ENABLED"),
                False,
                name="GARMINIZE_FIT_ENABLED",
            ),
            coordinate_transform_enabled=parse_bool(
                os.getenv("FIT_COORDINATE_TRANSFORM_ENABLED"),
                False,
                name="FIT_COORDINATE_TRANSFORM_ENABLED",
            ),
            processed_dir=data_dir / "processed",
            garmin_device_profile=device_profile,
        )

    def prepare_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        if self.processed_dir is not None:
            self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def preprocessing_enabled(self) -> bool:
        return (
            self.garminize_fit_enabled
            or self.coordinate_transform_enabled
        )

    def require_device_profile(self) -> GarminDeviceProfile:
        if self.garmin_device_profile is None:
            raise ValueError(
                "Garmin 设备伪装已启用，请同时设置 "
                "GARMIN_DEVICE_PRODUCT_ID 和 GARMIN_DEVICE_UNIT_ID。"
            )
        return self.garmin_device_profile

    def validate_for_sync(self) -> None:
        self.validate_for_onelap()
        if not self.uploader_path.exists():
            raise ValueError(f"Garmin 上传器不存在: {self.uploader_path}")

        if self.garminize_fit_enabled:
            self.require_device_profile().validate()

        if self.dry_run:
            return

        self._validate_garmin_sessions()

    def validate_for_reconcile(self) -> None:
        self.validate_for_onelap()
        activity_reader = self.uploader_path.with_name("garmin-activities.js")
        if not activity_reader.exists():
            raise ValueError(f"Garmin 活动列表读取器不存在: {activity_reader}")
        if not self.dry_run and not self.uploader_path.exists():
            raise ValueError(f"Garmin 上传器不存在: {self.uploader_path}")
        if self.garminize_fit_enabled:
            self.require_device_profile().validate()
        self._validate_garmin_sessions()

    def validate_for_onelap(self) -> None:
        # 手工 access token、refresh token、持久化会话和账号密码均为
        # 独立可选能力。不要在配置加载阶段强制要求某种组合；真正访问
        # OneLap 时，认证客户端会按可用能力恢复会话并给出运行时错误。
        return None

    def _validate_garmin_sessions(self) -> None:
        missing: list[str] = []
        for region in self.targets:
            prefix = "GARMIN_CN" if region == "CN" else "GARMIN_GLOBAL"
            for suffix in ("OAUTH1", "OAUTH2"):
                name = f"{prefix}_{suffix}"
                if not os.getenv(name, "").strip():
                    missing.append(name)
        if missing:
            raise ValueError("缺少 Garmin OAuth 会话: " + ", ".join(missing))
