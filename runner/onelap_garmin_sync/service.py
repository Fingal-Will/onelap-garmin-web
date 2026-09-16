from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .fit_preprocess import FitPreprocessor
from .garmin import GarminUploader
from .ledger import Ledger
from .onelap import (
    OneLapClient,
    activity_time_of,
    record_id_of,
    sha256_file,
)
from .reconcile import (
    candidate_from_source,
    comparison_date_range,
    garmin_activity_from_dict,
    reconcile_activities,
)

LOGGER = logging.getLogger(__name__)


def _source_activity_of(activity: dict[str, Any]) -> dict[str, Any]:
    source_json = activity.get("source_json")
    if isinstance(source_json, dict):
        return source_json
    if not source_json:
        return {}
    try:
        parsed = json.loads(str(source_json))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _display_activity_time(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    compact = text.replace("T", " ")
    return compact[:16] if len(compact) >= 16 else compact


def _display_distance_km(source: dict[str, Any]) -> str:
    value = source.get("distance_km")
    if value is None:
        return ""
    try:
        return f"{float(value):.2f} km"
    except (TypeError, ValueError):
        return ""


def _display_duration(source: dict[str, Any]) -> str:
    formatted = str(source.get("time_formatted") or "").strip()
    if formatted:
        return formatted
    try:
        seconds = max(0, int(float(source.get("time_seconds"))))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def activity_log_label(activity: dict[str, Any]) -> str:
    source = _source_activity_of(activity)
    record_id = str(
        activity.get("source_record_id")
        or source.get("id")
        or source.get("_id")
        or "未知"
    )
    started_at = _display_activity_time(
        source.get("start_riding_time")
        or source.get("activity_time")
        or source.get("date")
        or activity.get("activity_time")
    )
    name = str(
        activity.get("name")
        or source.get("name")
        or source.get("title")
        or ""
    ).strip()
    parts = [
        item
        for item in (
            started_at,
            name,
            _display_distance_km(source),
            _display_duration(source),
            f"ID {record_id}",
        )
        if item
    ]
    return f"活动[{' | '.join(parts)}]"


def retry_time(attempt: int, auth_required: bool) -> str:
    if auth_required:
        delay = timedelta(hours=6)
    else:
        minutes = min(360, 5 * (2 ** max(0, attempt - 1)))
        delay = timedelta(minutes=minutes)
    return (
        datetime.now(timezone.utc) + delay
    ).replace(microsecond=0).isoformat()


class SyncService:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        onelap: OneLapClient,
        garmin: GarminUploader,
        fit_preprocessor: FitPreprocessor | None = None,
    ):
        self.settings = settings
        self.ledger = ledger
        self.onelap = onelap
        self.garmin = garmin
        self.fit_preprocessor = fit_preprocessor

    def run(
        self,
        full_scan: bool = False,
        retry_auth: bool = False,
        record_id: str | None = None,
        force_reupload: bool = False,
    ) -> int:
        self.ledger.reset_stale_work()
        discovered = self.discover(full_scan=full_scan)
        LOGGER.info("OneLap 发现阶段完成：新增 %s 条活动", discovered)

        if force_reupload:
            if record_id is None:
                raise ValueError("--force-reupload 必须与 --record-id 一起使用")
            if not self.ledger.requeue_activity(record_id, self.settings.targets):
                raise ValueError(
                    f"台账中找不到活动 {record_id} 的目标区域任务。"
                    "可先增大扫描页数或使用 --full-scan。"
                )
            LOGGER.warning(
                "已将活动 ID %s 的 %s 上传任务重新排队",
                record_id,
                ", ".join(self.settings.targets),
            )

        activities = self.ledger.due_activities(
            self.settings.targets,
            self.settings.max_retries,
            retry_auth,
            record_id=record_id,
        )
        if record_id:
            LOGGER.info(
                "本次仅处理活动 ID %s，符合条件的任务数：%s",
                record_id,
                len(activities),
            )
        else:
            LOGGER.info("本次处理全部 %s 条待同步活动", len(activities))

        failures = 0
        for activity in activities:
            failures += self._process_activity(activity, retry_auth)

        for row in self.ledger.summary():
            LOGGER.info(
                "台账汇总：%s / %s = %s",
                row["target_region"],
                row["status"],
                row["count"],
            )
        return 1 if failures else 0

    def reconcile_recent(
        self,
        recent_limit: int,
    ) -> int:
        if recent_limit < 1:
            raise ValueError("核对活动数量必须大于 0")

        self.ledger.reset_stale_work()
        recent_items = self._fetch_recent_items(recent_limit)
        if not recent_items:
            LOGGER.info("OneLap 最近活动列表为空，没有可核对活动")
            return 0

        self._register_items(recent_items)
        record_ids = [
            record_id
            for item in recent_items
            if (record_id := record_id_of(item))
        ]
        LOGGER.info(
            "开始核对最近 %s 条 OneLap 活动，目标区域：%s",
            len(record_ids),
            ", ".join(self.settings.targets),
        )

        results_by_target: dict[str, list[Any]] = {}
        query_failures = 0
        for target in self.settings.targets:
            rows = self.ledger.reconciliation_candidates(record_ids, target)
            candidates = [
                candidate_from_source(
                    str(row["source_record_id"]),
                    _source_activity_of(row),
                    (
                        str(row["remote_activity_id"])
                        if row.get("remote_activity_id") is not None
                        else None
                    ),
                )
                for row in rows
            ]
            date_range = comparison_date_range(candidates)
            if date_range is None:
                message = "所选 OneLap 活动均缺少可用开始时间，无法查询 Garmin"
                self._record_unverified(target, candidates, message)
                query_failures += 1
                continue

            start_date, end_date = date_range
            LOGGER.info(
                "Garmin %s 仅查询 %s 至 %s 的活动，不扫描全部历史",
                target,
                start_date.isoformat(),
                end_date.isoformat(),
            )
            try:
                listed = self.garmin.list_activities(
                    target,
                    start_date.isoformat(),
                    end_date.isoformat(),
                )
            except Exception as exc:
                message = f"Garmin 活动列表读取异常：{exc}"
                self._record_unverified(target, candidates, message)
                query_failures += 1
                LOGGER.exception("Garmin %s 核对异常", target)
                continue
            if not listed.success or listed.truncated:
                message = listed.message or "Garmin 活动列表读取不完整"
                self._record_unverified(target, candidates, message)
                query_failures += 1
                LOGGER.error("Garmin %s 核对失败：%s", target, message)
                continue

            garmin_activities = [
                garmin_activity_from_dict(item) for item in listed.activities
            ]
            results = reconcile_activities(candidates, garmin_activities)
            results_by_target[target] = results
            for result in results:
                self.ledger.record_reconciliation(
                    result.record_id,
                    target,
                    result.status,
                    result.message,
                    result.remote_activity_id,
                )
                LOGGER.info(
                    "核对结果：Garmin %s / %s / %s / %s",
                    target,
                    result.record_id,
                    result.status,
                    result.message,
                )

        missing_targets: dict[str, list[str]] = {}
        for target, results in results_by_target.items():
            for result in results:
                if result.status == "missing":
                    missing_targets.setdefault(result.record_id, []).append(target)

        missing_record_ids = [
            record_id for record_id in record_ids if record_id in missing_targets
        ]
        if self.settings.dry_run:
            LOGGER.info(
                "[dry-run] 共确认 %s 条活动缺失，不改变上传队列、不上传",
                len(missing_targets),
            )
            return 1 if query_failures else 0

        for record_id in missing_record_ids:
            for target in missing_targets[record_id]:
                self.ledger.record_reconciliation(
                    record_id,
                    target,
                    "missing",
                    "确认缺失，已加入本次补同步队列",
                    requeue_missing=True,
                )

        upload_failures = 0
        for record_id in reversed(missing_record_ids):
            activity = self.ledger.activity_by_record_id(record_id)
            if activity is None:
                query_failures += 1
                LOGGER.error("台账中找不到待补同步活动 %s", record_id)
                continue
            upload_failures += self._process_activity(
                activity,
                retry_auth=False,
                explicit_targets=missing_targets[record_id],
            )

        status_counts: dict[tuple[str, str], int] = {}
        for target, results in results_by_target.items():
            for result in results:
                key = (target, result.status)
                status_counts[key] = status_counts.get(key, 0) + 1
        for (target, status), count in sorted(status_counts.items()):
            LOGGER.info("核对汇总：%s / %s = %s", target, status, count)
        LOGGER.info(
            "补同步汇总：确认缺失 %s 条，本次处理 %s 条",
            len(missing_targets),
            len(missing_record_ids),
        )
        return 1 if query_failures or upload_failures else 0

    def _fetch_recent_items(self, limit: int) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_number = 1
        while len(unique) < limit:
            page = self.onelap.fetch_page(page_number, page_size=20)
            for item in page.items:
                record_id = record_id_of(item)
                if not record_id or record_id in seen:
                    continue
                seen.add(record_id)
                unique.append(item)
            if page.is_last:
                break
            page_number += 1
        unique.sort(
            key=lambda item: (
                candidate_from_source(record_id_of(item), item).start_time_local
                or datetime.min
            ),
            reverse=True,
        )
        return unique[:limit]

    def _record_unverified(
        self,
        target: str,
        candidates: list[Any],
        message: str,
    ) -> None:
        for candidate in candidates:
            self.ledger.record_reconciliation(
                candidate.record_id,
                target,
                "unverified",
                message,
            )

    def discover(self, full_scan: bool = False) -> int:
        known_ids = self.ledger.known_record_ids()
        new_count = 0

        if full_scan:
            self.ledger.set_metadata("onelap_backfill_complete", "0")
            self.ledger.set_metadata("onelap_next_page", "1")

        backfill_complete = (
            self.ledger.get_metadata("onelap_backfill_complete", "0") == "1"
        )
        if backfill_complete:
            for page in self.onelap.iter_pages(
                start_page=1,
                max_pages=self.settings.discovery_pages,
            ):
                page_ids = {
                    record_id_of(item) for item in page.items if record_id_of(item)
                }
                new_count += self._register_items(page.items)
                if page_ids and page_ids.issubset(known_ids):
                    break
                known_ids.update(page_ids)
            return new_count

        start_page = int(self.ledger.get_metadata("onelap_next_page", "1") or "1")
        max_pages = 0 if full_scan else self.settings.discovery_pages

        # 回填期间也扫描第一页，避免历史很多时漏掉刚产生的新活动。
        if start_page > 1:
            recent = self.onelap.fetch_page(1)
            new_count += self._register_items(recent.items)

        last_page_number = start_page - 1
        reached_end = False
        for page in self.onelap.iter_pages(
            start_page=start_page,
            max_pages=max_pages,
        ):
            new_count += self._register_items(page.items)
            last_page_number = page.page
            if page.is_last:
                reached_end = True
                break

        if reached_end:
            self.ledger.set_metadata("onelap_backfill_complete", "1")
            self.ledger.set_metadata("onelap_next_page", "1")
        elif last_page_number >= start_page:
            self.ledger.set_metadata(
                "onelap_next_page", str(last_page_number + 1)
            )
        return new_count

    def _register_items(self, items: list[dict[str, Any]]) -> int:
        new_count = 0
        for activity in items:
            record_id = record_id_of(activity)
            if not record_id:
                LOGGER.warning("跳过缺少 record_id 的 OneLap 活动")
                continue
            name = str(
                activity.get("name")
                or activity.get("title")
                or activity.get("sport_name")
                or ""
            )
            was_new = self.ledger.upsert_activity(
                record_id=record_id,
                activity_time=activity_time_of(activity),
                name=name,
                source_json=json.dumps(
                    activity, ensure_ascii=False, separators=(",", ":")
                ),
                targets=self.settings.targets,
            )
            new_count += int(was_new)
        return new_count

    def _process_activity(
        self,
        activity: dict[str, Any],
        retry_auth: bool,
        explicit_targets: list[str] | None = None,
    ) -> int:
        record_id = str(activity["source_record_id"])
        if explicit_targets is None:
            due_targets = self.ledger.due_targets(
                record_id,
                self.settings.targets,
                self.settings.max_retries,
                retry_auth,
            )
        else:
            allowed_targets = set(self.settings.targets)
            due_targets = sorted(
                {
                    target
                    for target in explicit_targets
                    if target in allowed_targets
                }
            )
        if not due_targets:
            return 0

        activity_label = activity_log_label(activity)
        LOGGER.info(
            "开始处理%s，目标区域：%s",
            activity_label,
            ", ".join(due_targets),
        )
        fit_path = self._ensure_fit(activity)
        if fit_path is None:
            return len(due_targets)

        upload_path = self._ensure_processed_fit(
            activity,
            fit_path,
            activity_label,
        )
        if upload_path is None:
            return len(due_targets)

        failures = 0
        for target in due_targets:
            if self.settings.dry_run:
                LOGGER.info(
                    "[dry-run] %s 将使用 %s 上传到 Garmin %s",
                    activity_label,
                    upload_path.name,
                    target,
                )
                continue

            attempt = self.ledger.mark_upload_started(record_id, target)
            LOGGER.info(
                "上传%s到 Garmin %s，第 %s 次尝试",
                activity_label,
                target,
                attempt,
            )
            try:
                result = self.garmin.upload(target, upload_path)
            except Exception as exc:
                failures += 1
                message = f"Garmin 上传器异常：{exc}"
                self.ledger.mark_upload_failed(
                    record_id,
                    target,
                    message,
                    retry_time(attempt, auth_required=False),
                    auth_required=False,
                )
                LOGGER.exception(
                    "Garmin %s 上传异常；%s",
                    target,
                    activity_label,
                )
                continue
            if result.success:
                self.ledger.mark_upload_success(
                    record_id,
                    target,
                    result.remote_activity_id,
                    duplicate=result.duplicate,
                )
                LOGGER.info(
                    "Garmin %s 上传完成：%s%s",
                    target,
                    activity_label,
                    "（目标区已存在，按成功记账）" if result.duplicate else "",
                )
            else:
                failures += 1
                self.ledger.mark_upload_failed(
                    record_id,
                    target,
                    result.message,
                    retry_time(attempt, result.auth_required),
                    result.auth_required,
                )
                LOGGER.error(
                    "Garmin %s 上传失败：%s；%s",
                    target,
                    result.message,
                    activity_label,
                )
        return failures

    def _ensure_processed_fit(
        self,
        activity: dict[str, Any],
        source_path: Path,
        activity_label: str,
    ) -> Path | None:
        if not self.settings.preprocessing_enabled:
            return source_path

        record_id = str(activity["source_record_id"])
        profile = (
            self.settings.require_device_profile()
            if self.settings.garminize_fit_enabled
            else None
        )
        existing_path = (
            Path(str(activity["processed_fit_path"]))
            if activity.get("processed_fit_path")
            else None
        )
        expected_product_id = profile.product_id if profile else None
        expected_software_version = (
            profile.software_version if profile else None
        )
        if (
            activity.get("processing_status") == "success"
            and existing_path
            and existing_path.exists()
            and self.onelap.validate_fit(existing_path)
            and bool(activity.get("processed_fit_sha256"))
            and sha256_file(existing_path)
            == activity.get("processed_fit_sha256")
            and activity.get("garmin_product_id") == expected_product_id
            and activity.get("garmin_software_version")
            == expected_software_version
            and bool(activity.get("garminize_enabled"))
            == self.settings.garminize_fit_enabled
            and bool(activity.get("coordinate_transform_enabled"))
            == self.settings.coordinate_transform_enabled
        ):
            LOGGER.info(
                "使用已处理的 FIT 文件 %s：%s",
                existing_path.name,
                activity_label,
            )
            return existing_path

        if self.fit_preprocessor is None:
            LOGGER.error(
                "FIT 预处理器未初始化：%s",
                activity_label,
            )
            return None

        self.ledger.mark_processing_started(
            record_id,
            garminize_enabled=self.settings.garminize_fit_enabled,
            coordinate_transform_enabled=(
                self.settings.coordinate_transform_enabled
            ),
            product_id=expected_product_id,
            software_version=expected_software_version,
        )
        try:
            result = self.fit_preprocessor.process(
                source_path=source_path,
                record_id=record_id,
                profile=profile,
                garminize_enabled=self.settings.garminize_fit_enabled,
                coordinate_transform_enabled=(
                    self.settings.coordinate_transform_enabled
                ),
            )
        except Exception as exc:
            self.ledger.mark_processing_failed(
                record_id,
                str(exc),
                garminize_enabled=self.settings.garminize_fit_enabled,
                coordinate_transform_enabled=(
                    self.settings.coordinate_transform_enabled
                ),
                product_id=expected_product_id,
                software_version=expected_software_version,
            )
            LOGGER.error(
                "FIT 预处理失败，两个 Garmin 区域均不上传：%s；%s",
                exc,
                activity_label,
            )
            return None

        self.ledger.mark_processing_success(
            record_id,
            str(result.path),
            result.sha256,
            result.product_id,
            result.software_version,
            result.garminize_enabled,
            result.coordinate_transform_enabled,
            result.coordinate_points_converted,
        )
        software_label = (
            str(result.software_version)
            if result.software_version is not None
            else "未设置"
        )
        LOGGER.info(
            "FIT 预处理完成：file=%s，garminize=%s，坐标转换=%s，"
            "坐标对=%s，product=%s，software=%s，records=%s，"
            "心率=%s，功率=%s，踏频=%s；%s",
            result.path.name,
            result.garminize_enabled,
            result.coordinate_transform_enabled,
            result.coordinate_points_converted,
            result.product_id if result.product_id is not None else "未设置",
            software_label,
            result.summary.record_count,
            result.summary.heart_rate_count,
            result.summary.power_count,
            result.summary.cadence_count,
            activity_label,
        )
        if result.summary.heart_rate_count == 0:
            LOGGER.warning(
                "处理后的 FIT 没有有效心率记录，Garmin 训练指标可能不完整：%s",
                activity_label,
            )
        if result.summary.power_count == 0:
            LOGGER.warning(
                "处理后的 FIT 没有有效功率记录，骑行训练负荷计算可能受限：%s",
                activity_label,
            )
        return result.path

    def _ensure_fit(self, activity: dict[str, Any]) -> Path | None:
        record_id = str(activity["source_record_id"])
        activity_label = activity_log_label(activity)
        existing_path = (
            Path(str(activity["fit_path"])) if activity.get("fit_path") else None
        )
        if (
            existing_path
            and existing_path.exists()
            and self.onelap.validate_fit(existing_path)
        ):
            LOGGER.info("使用已下载的 FIT 文件：%s", activity_label)
            return existing_path

        self.ledger.mark_download_started(record_id)
        try:
            LOGGER.info("正在下载 FIT 文件：%s", activity_label)
            source_activity = json.loads(str(activity["source_json"]))
            fit_path = self.onelap.download_fit(
                source_activity, self.settings.download_dir
            )
            digest = sha256_file(fit_path)
            self.ledger.mark_download_success(record_id, str(fit_path), digest)
            activity.update(
                {
                    "fit_path": str(fit_path),
                    "fit_sha256": digest,
                    "download_status": "success",
                    "processing_status": "not_requested",
                    "processing_error": None,
                    "processed_fit_path": None,
                    "processed_fit_sha256": None,
                    "garmin_product_id": None,
                    "garmin_software_version": None,
                    "coordinate_transform_enabled": 0,
                    "garminize_enabled": 0,
                    "coordinate_points_converted": 0,
                }
            )
            LOGGER.info("FIT 下载完成：%s", activity_label)
            return fit_path
        except Exception as exc:
            self.ledger.mark_download_failed(record_id, str(exc))
            LOGGER.exception("FIT 下载失败：%s", activity_label)
            return None
