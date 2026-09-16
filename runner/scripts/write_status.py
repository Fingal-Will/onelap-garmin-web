"""Export aggregate counts, never activity metadata, credentials or error payloads."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

UPLOAD_STATUSES = {"pending", "uploading", "success", "duplicate", "failed", "auth_required"}
RECONCILIATION_STATUSES = {"present", "missing", "ambiguous", "unverified"}


def safe_identifier(value: str, maximum: int = 80) -> str:
    return value if re.fullmatch(rf"[A-Za-z0-9_-]{{0,{maximum}}}", value) else ""


def grouped(connection: sqlite3.Connection, column: str, allowed: set[str]) -> list[dict]:
    # column comes only from the two constants in read_ledger, never from external input.
    rows = connection.execute(
        f"SELECT target_region, {column}, COUNT(*) FROM uploads GROUP BY target_region, {column} ORDER BY target_region, {column}"
    ).fetchall()
    return [
        {"target_region": region, "status": status, "count": count}
        for region, status, count in rows
        if region in {"CN", "GLOBAL"} and status in allowed
    ]


def read_ledger(database: Path) -> dict:
    empty = {"available": False, "activities": 0, "uploads": [], "reconciliation": []}
    if not database.is_file():
        return empty
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return {
                "available": True,
                "activities": connection.execute("SELECT COUNT(*) FROM activities").fetchone()[0],
                "uploads": grouped(connection, "status", UPLOAD_STATUSES),
                "reconciliation": grouped(connection, "reconciliation_status", RECONCILIATION_STATUSES),
            }
        finally:
            connection.close()
    except sqlite3.Error:
        # Details might contain activity data; export only a fixed diagnostic code.
        return {**empty, "error": "ledger_unreadable"}


def build_status(env: dict[str, str], database: Path) -> dict:
    status = env.get("WEB_JOB_STATUS", "unknown")
    workflow = env.get("WEB_WORKFLOW", "unknown")
    event = env.get("GITHUB_EVENT_NAME", "unknown")
    return {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run": {
            "id": safe_identifier(env.get("GITHUB_RUN_ID", "")),
            "number": safe_identifier(env.get("GITHUB_RUN_NUMBER", "")),
            "attempt": safe_identifier(env.get("GITHUB_RUN_ATTEMPT", "")),
            "workflow": workflow if workflow in {"sync", "reconcile"} else "unknown",
            "event": event if event in {"schedule", "workflow_dispatch"} else "unknown",
            "status": status if status in {"success", "failure", "cancelled", "skipped"} else "unknown",
            "request_id": safe_identifier(env.get("WEB_REQUEST_ID", "")),
            "dry_run": env.get("SYNC_DRY_RUN", "true") != "false",
            "targets": [region for region in env.get("SYNC_TARGETS", "").split(",") if region in {"CN", "GLOBAL"}],
        },
        "ledger": read_ledger(database),
    }


def main() -> None:
    data = Path("data")
    data.mkdir(exist_ok=True)
    status = build_status(dict(os.environ), data / "sync.db")
    temporary = data / "web-status.json.tmp"
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(data / "web-status.json")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        summary = ["## 同步台账汇总", "", f"运行结果（保存台账前）：`{status['run']['status']}`", "", f"已发现活动：{status['ledger']['activities']} 条", "", "| 区域 | 状态 | 数量 |", "| --- | --- | ---: |"]
        for row in status["ledger"]["uploads"]:
            summary.append(f"| {row['target_region']} | {row['status']} | {row['count']} |")
        summary.extend(["", "该汇总只包含计数；最终任务结果请以 Actions 运行状态为准。", ""])
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write("\n".join(summary))


if __name__ == "__main__":
    main()
