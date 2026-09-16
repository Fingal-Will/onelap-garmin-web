"""Validate dispatch data before writing a strictly allowlisted Actions environment."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path


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
        targets = env.get("SCHEDULE_TARGETS") or "CN,GLOBAL"
    elif event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        targets = inputs.get("target_regions", "CN,GLOBAL")
    else:
        raise ValueError("只允许网页手动运行或已开启的定时任务")
    targets = choice(targets, {"CN", "GLOBAL", "CN,GLOBAL"}, "同步区域")
    request_id = inputs.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,80}", request_id):
        raise ValueError("request_id 必须为最多 80 个英文字母、数字、短横线或下划线")
    record_id = inputs.get("record_id", "")
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,128}", record_id):
        raise ValueError("活动编号格式无效")
    force = boolean(inputs.get("force_reupload"), "force_reupload")
    if force == "true" and not record_id:
        raise ValueError("重新上传必须指定活动编号")
    return {
        "SYNC_TARGETS": targets,
        "ONELAP_DISCOVERY_PAGES": choice(inputs.get("discovery_pages", "1"), {"0", "1", "3", "10", "20", "50", "100"}, "读取页数"),
        "RECONCILE_RECENT_LIMIT": choice(inputs.get("recent_activities", "20"), {"1", "10", "20", "50", "100", "200"}, "核对条数"),
        "SYNC_DRY_RUN": "false" if scheduled else boolean(inputs.get("dry_run"), "dry_run", True),
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
