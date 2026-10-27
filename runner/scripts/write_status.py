"""Export private UI reports using allowlisted fields; never credentials or raw errors."""

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
        if region in {"CN", "GLOBAL", "STRAVA"} and status in allowed
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


def read_activity_items(database: Path) -> list[dict]:
    """Bounded private report. Never export source_json, paths, Unit ID or errors."""
    if not database.is_file():
        return []
    connection = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT source_record_id, name, activity_time, download_status, "
            "processing_status, fit_sha256 FROM activities "
            "ORDER BY activity_time DESC, source_record_id DESC LIMIT 200"
        ).fetchall()
        result = []
        for row in rows:
            identifier = safe_identifier(str(row["source_record_id"]), 100)
            if not identifier:
                continue
            item = {
                "id": identifier,
                "name": str(row["name"] or "OneLap 骑行活动")[:200],
                "activity_time": safe_time(row["activity_time"]),
                "download_status": row["download_status"] if row["download_status"] in {"pending", "downloading", "success", "failed"} else "unknown",
                "processing_status": row["processing_status"] if row["processing_status"] in {"not_requested", "pending", "processing", "success", "failed", "skipped"} else "unknown",
                "fit_sha256": row["fit_sha256"] if re.fullmatch(r"[a-fA-F0-9]{64}", row["fit_sha256"] or "") else "",
                "targets": {},
            }
            uploads = connection.execute(
                "SELECT target_region, status, attempts, remote_activity_id, "
                "next_retry_at, updated_at, uploaded_at, reconciliation_status, "
                "reconciliation_checked_at FROM uploads WHERE source_record_id = ?",
                (identifier,),
            ).fetchall()
            for upload in uploads:
                if upload["target_region"] not in {"CN", "GLOBAL", "STRAVA"}:
                    continue
                item["targets"][upload["target_region"]] = {
                    "status": upload["status"] if upload["status"] in UPLOAD_STATUSES else "unknown",
                    "attempts": max(0, int(upload["attempts"] or 0)),
                    "remote_activity_id": safe_identifier(str(upload["remote_activity_id"] or ""), 100),
                    "next_retry_at": safe_time(upload["next_retry_at"]),
                    "updated_at": safe_time(upload["updated_at"]),
                    "uploaded_at": safe_time(upload["uploaded_at"]),
                    "reconciliation_status": upload["reconciliation_status"] if upload["reconciliation_status"] in RECONCILIATION_STATUSES else "",
                    "reconciliation_checked_at": safe_time(upload["reconciliation_checked_at"]),
                }
            result.append(item)
        return result
    except (sqlite3.Error, ValueError, TypeError):
        # Older ledgers remain readable through the aggregate schema.
        return []
    finally:
        if connection:
            connection.close()


def safe_time(value) -> str:
    if not isinstance(value, str):
        return ""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return value[:40]


def build_status(env: dict[str, str], database: Path) -> dict:
    status = env.get("WEB_JOB_STATUS", "unknown")
    workflow = env.get("WEB_WORKFLOW", "unknown")
    event = env.get("GITHUB_EVENT_NAME", "unknown")
    targets = [region for region in env.get("SYNC_TARGETS", "").split(",") if region in {"CN", "GLOBAL"}]
    if workflow == "sync" and env.get("STRAVA_SYNC_ENABLED") == "true":
        targets.append("STRAVA")
    return {
        "schema_version": 2,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "run": {
            "id": safe_identifier(env.get("GITHUB_RUN_ID", "")),
            "number": safe_identifier(env.get("GITHUB_RUN_NUMBER", "")),
            "attempt": safe_identifier(env.get("GITHUB_RUN_ATTEMPT", "")),
            "workflow": workflow if workflow in {"sync", "reconcile"} else "unknown",
            "event": event if event in {"schedule", "workflow_dispatch"} else "unknown",
            "status": status if status in {"success", "failure", "cancelled", "skipped"} else "unknown",
            "request_id": safe_identifier(env.get("WEB_REQUEST_ID", "")),
            "record_id": safe_identifier(env.get("MANUAL_RECORD_ID", ""), 100),
            "dry_run": env.get("SYNC_DRY_RUN", "true") != "false",
            "targets": targets,
        },
        "ledger": read_ledger(database),
        "activity_items": read_activity_items(database),
        "activity_limit": 200,
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
