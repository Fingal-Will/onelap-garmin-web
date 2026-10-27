"""Validate dispatch data before writing a strictly allowlisted Actions environment."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onelap_garmin_sync.config import parse_bool, resolve_sync_targets
from onelap_garmin_sync.workflow_settings import nonnegative_integer


def boolean(value: object, name: str, default: bool = False) -> str:
    if value is None:
        return str(default).lower()
    if value is True or value == "true":
        return "true"
    if value is False or value == "false":
        return "false"
    raise ValueError(f"{name} 必须为 true 或 false")


def choice(value: object, options: set[str], name: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise ValueError(f"{name} 不在允许范围内")
    return value


def prepare(event: dict, event_name: str, workflow: str, env: dict) -> dict[str, str]:
    repository = event.get("repository", {})
    if repository.get("private") is not True:
        raise ValueError("同步任务只允许在用户自己的私有仓库运行")
    default_branch = repository.get("default_branch", "")
    if not default_branch or env.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise ValueError("只允许从仓库默认分支执行")
    marker = json.loads(Path(".onelap-web.json").read_text(encoding="utf-8"))
    if marker.get("project") != "onelap-garmin-web" or marker.get("schema_version") != 1:
        raise ValueError("仓库标记不匹配")
    if workflow not in {"sync", "reconcile"}:
        raise ValueError("未知工作流")
    if env.get("GARMINIZE_FIT_ENABLED") != "true":
        raise ValueError("请在网页中保存 Garmin 设备设置")
    choice(env.get("FIT_COORDINATE_TRANSFORM_ENABLED"), {"true", "false"}, "轨迹坐标转换")

    scheduled = event_name == "schedule"
    if scheduled:
        if workflow != "sync" or env.get("SCHEDULE_ENABLED") != "true":
            raise ValueError("定时同步未开启")
        inputs = {}
        targets = env.get("DEFAULT_TARGETS") or "AUTO"
    elif event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        targets = inputs.get("target_regions") or "DEFAULT"
        targets = choice(targets, {"DEFAULT", "AUTO", "CN", "GLOBAL", "CN,GLOBAL"}, "同步区域")
        if targets == "DEFAULT":
            targets = env.get("DEFAULT_TARGETS") or "AUTO"
    else:
        raise ValueError("只允许网页手动运行或已开启的定时任务")
    targets = choice(targets, {"AUTO", "CN", "GLOBAL", "CN,GLOBAL"}, "默认同步区域")
    resolved = resolve_sync_targets(targets, env)
    if not resolved:
        raise ValueError("请至少配置一个 Garmin 区域的完整 OAuth 会话，或把默认同步区域设为已配置区域")
    targets = ",".join(resolved)
    request_id = inputs.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,80}", request_id):
        raise ValueError("request_id 必须为最多 80 个英文字母、数字、短横线或下划线")
    record_id = inputs.get("record_id", "")
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,128}", record_id):
        raise ValueError("活动编号格式无效")
    force = boolean(inputs.get("force_reupload"), "force_reupload")
    if force == "true" and not record_id:
        raise ValueError("重新上传必须指定活动编号")
    pages = inputs.get("discovery_pages") or "DEFAULT"
    recent = inputs.get("recent_activities") or "DEFAULT"
    pages = choice(pages, {"DEFAULT", "0", "1", "3", "10", "20", "50", "100"}, "读取页数")
    recent = choice(recent, {"DEFAULT", "1", "10", "20", "50", "100", "200"}, "核对条数")
    default_pages = nonnegative_integer(env.get("DEFAULT_DISCOVERY_PAGES") or "10", "ONELAP_DISCOVERY_PAGES")
    default_recent = nonnegative_integer(env.get("DEFAULT_RECENT_LIMIT") or "20", "RECONCILE_RECENT_LIMIT", 1)
    if default_recent > 200:
        raise ValueError("RECONCILE_RECENT_LIMIT 必须在 1 到 200 之间")
    retries = nonnegative_integer(env.get("SYNC_MAX_RETRIES") or "8", "SYNC_MAX_RETRIES", 1)
    global_dry_run = parse_bool(env.get("DEFAULT_DRY_RUN"), False, name="SYNC_DRY_RUN")
    request_dry_run = not scheduled and boolean(inputs.get("dry_run"), "dry_run", True) == "true"
    dry_run = global_dry_run or request_dry_run
    return {
        "SYNC_TARGETS": targets,
        "ONELAP_DISCOVERY_PAGES": str(default_pages) if pages == "DEFAULT" else pages,
        "RECONCILE_RECENT_LIMIT": str(default_recent) if recent == "DEFAULT" else recent,
        "SYNC_MAX_RETRIES": str(retries),
        "SYNC_DRY_RUN": str(dry_run).lower(),
        "MANUAL_FULL_SCAN": boolean(inputs.get("full_scan"), "full_scan"),
        "MANUAL_RETRY_AUTH": boolean(inputs.get("retry_auth"), "retry_auth"),
        "MANUAL_FORCE_REUPLOAD": force,
        "MANUAL_RECORD_ID": record_id,
        "WEB_REQUEST_ID": request_id,
    }


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    values = prepare(event, os.environ["GITHUB_EVENT_NAME"], os.environ["WEB_WORKFLOW"], dict(os.environ))
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(f"设置验证失败：{error}") from None
