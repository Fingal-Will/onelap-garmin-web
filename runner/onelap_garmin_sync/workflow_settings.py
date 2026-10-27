"""Resolve Actions inputs and repository configuration without network calls."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

from .config import parse_bool, resolve_sync_targets


REGIONS = {
    "中国区和国际区": "CN,GLOBAL",
    "仅国际区": "GLOBAL",
    "仅中国区": "CN",
}
DEFAULT_CHOICE = "使用默认同步设置"
PAGE_CHOICES = {
    f"{pages}页（最多{pages * 20}条活动）": pages
    for pages in (1, 3, 10, 20, 50, 100)
}
PAGE_CHOICES["不限页数（从当前进度读到历史末尾）"] = 0


def nonnegative_integer(raw: str, name: str, minimum: int = 0) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} 必须是整数") from None
    if value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def prepare_settings(workflow: str, env: Mapping[str, str]) -> dict[str, str]:
    if workflow not in {"sync", "reconcile"}:
        raise ValueError("无法识别工作流")
    manual = env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    raw_targets = env.get("SYNC_TARGETS")
    choice = env.get("INPUT_TARGET_REGIONS", "") if manual else ""
    if choice and choice != DEFAULT_CHOICE:
        if choice not in REGIONS:
            raise ValueError("无法识别佳明目标区域")
        raw_targets = REGIONS[choice]
    targets = resolve_sync_targets(raw_targets, env)
    if not targets:
        raise ValueError("自动模式至少需要一个完整区域会话，请配置 Garmin OAuth1/OAuth2")
    values = {"SYNC_TARGETS": ",".join(targets)}
    for name in ("GARMINIZE_FIT_ENABLED", "FIT_COORDINATE_TRANSFORM_ENABLED"):
        values[name] = str(parse_bool(env.get(name), False, name=name)).lower()
    retries = nonnegative_integer(env.get("SYNC_MAX_RETRIES") or "8", "SYNC_MAX_RETRIES", 1)
    values["SYNC_MAX_RETRIES"] = str(retries)
    dry_run = parse_bool(env.get("SYNC_DRY_RUN"), False, name="SYNC_DRY_RUN")
    if manual:
        # A repository-wide dry-run remains in effect even if the manual box is unchecked.
        dry_run = dry_run or parse_bool(env.get("INPUT_DRY_RUN"), False, name="dry_run")
    values["SYNC_DRY_RUN"] = str(dry_run).lower()
    if workflow == "sync":
        pages = nonnegative_integer(env.get("ONELAP_DISCOVERY_PAGES") or "10", "ONELAP_DISCOVERY_PAGES")
        choice = env.get("INPUT_DISCOVERY_PAGES", "") if manual else ""
        if choice and choice != DEFAULT_CHOICE:
            if choice not in PAGE_CHOICES:
                raise ValueError("无法识别读取页数")
            pages = PAGE_CHOICES[choice]
        values["ONELAP_DISCOVERY_PAGES"] = str(pages)
    else:
        choice = env.get("INPUT_RECENT_ACTIVITIES", "") if manual else ""
        if choice and choice != DEFAULT_CHOICE:
            if choice not in {f"{n}条" for n in (1, 10, 20, 50, 100, 200)}:
                raise ValueError("无法识别核对条数")
            limit = int(choice[:-1])
        else:
            limit = nonnegative_integer(env.get("RECONCILE_RECENT_LIMIT") or "20", "RECONCILE_RECENT_LIMIT", 1)
        values["RECONCILE_RECENT_LIMIT"] = str(limit)
    return values


def main() -> None:
    try:
        values = prepare_settings(sys.argv[1], os.environ)
    except (ValueError, IndexError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()
