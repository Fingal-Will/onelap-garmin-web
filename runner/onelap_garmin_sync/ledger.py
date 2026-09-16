from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Ledger:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.initialize()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS activities (
                source_record_id TEXT PRIMARY KEY,
                activity_time TEXT,
                name TEXT NOT NULL DEFAULT '',
                source_json TEXT NOT NULL DEFAULT '{}',
                fit_path TEXT,
                fit_sha256 TEXT,
                download_status TEXT NOT NULL DEFAULT 'pending',
                download_attempts INTEGER NOT NULL DEFAULT 0,
                download_error TEXT,
                processing_status TEXT NOT NULL DEFAULT 'not_requested',
                processing_error TEXT,
                processed_fit_path TEXT,
                processed_fit_sha256 TEXT,
                garmin_product_id INTEGER,
                garmin_software_version INTEGER,
                coordinate_transform_enabled INTEGER NOT NULL DEFAULT 0,
                garminize_enabled INTEGER NOT NULL DEFAULT 0,
                coordinate_points_converted INTEGER NOT NULL DEFAULT 0,
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                source_record_id TEXT NOT NULL,
                target_region TEXT NOT NULL CHECK (target_region IN ('CN', 'GLOBAL')),
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                remote_activity_id TEXT,
                reconciliation_status TEXT,
                reconciliation_checked_at TEXT,
                reconciliation_message TEXT,
                next_retry_at TEXT,
                uploaded_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_record_id, target_region),
                FOREIGN KEY (source_record_id)
                    REFERENCES activities(source_record_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_state (
                provider TEXT PRIMARY KEY,
                encrypted_payload BLOB NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_uploads_due
                ON uploads(status, next_retry_at, target_region);
            CREATE INDEX IF NOT EXISTS idx_activities_time
                ON activities(activity_time, source_record_id);
            """
        )
        self._migrate_activity_columns()
        self._migrate_upload_columns()
        self.connection.commit()

    def _migrate_activity_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(activities)"
            ).fetchall()
        }
        migrations = {
            "processing_status": "TEXT NOT NULL DEFAULT 'not_requested'",
            "processing_error": "TEXT",
            "processed_fit_path": "TEXT",
            "processed_fit_sha256": "TEXT",
            "garmin_product_id": "INTEGER",
            "garmin_software_version": "INTEGER",
            "coordinate_transform_enabled": "INTEGER NOT NULL DEFAULT 0",
            "garminize_enabled": "INTEGER NOT NULL DEFAULT 0",
            "coordinate_points_converted": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE activities ADD COLUMN {name} {declaration}"
                )

    def _migrate_upload_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(uploads)"
            ).fetchall()
        }
        migrations = {
            "reconciliation_status": "TEXT",
            "reconciliation_checked_at": "TEXT",
            "reconciliation_message": "TEXT",
        }
        for name, declaration in migrations.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE uploads ADD COLUMN {name} {declaration}"
                )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            self.connection.execute("BEGIN")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def upsert_activity(
        self,
        record_id: str,
        activity_time: str | None,
        name: str,
        source_json: str,
        targets: tuple[str, ...],
    ) -> bool:
        now = utc_now()
        exists = self.connection.execute(
            "SELECT 1 FROM activities WHERE source_record_id = ?", (record_id,)
        ).fetchone()
        self.connection.execute(
            """
            INSERT INTO activities (
                source_record_id, activity_time, name, source_json,
                discovered_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_record_id) DO UPDATE SET
                activity_time = COALESCE(excluded.activity_time, activities.activity_time),
                name = excluded.name,
                source_json = excluded.source_json,
                updated_at = excluded.updated_at
            WHERE activities.activity_time IS NOT COALESCE(
                    excluded.activity_time, activities.activity_time
                )
               OR activities.name IS NOT excluded.name
               OR activities.source_json IS NOT excluded.source_json
            """,
            (record_id, activity_time, name, source_json, now, now),
        )
        for target in targets:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO uploads (
                    source_record_id, target_region, status, updated_at
                ) VALUES (?, ?, 'pending', ?)
                """,
                (record_id, target, now),
            )
        self.connection.commit()
        return exists is None

    def known_record_ids(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT source_record_id FROM activities"
        ).fetchall()
        return {str(row["source_record_id"]) for row in rows}

    def reconciliation_candidates(
        self,
        record_ids: list[str],
        target: str,
    ) -> list[dict[str, Any]]:
        if not record_ids:
            return []
        placeholders = ",".join("?" for _ in record_ids)
        rows = self.connection.execute(
            f"""
            SELECT
                a.*,
                u.status AS upload_status,
                u.remote_activity_id,
                u.reconciliation_status,
                u.reconciliation_checked_at,
                u.reconciliation_message
            FROM activities a
            JOIN uploads u ON u.source_record_id = a.source_record_id
            WHERE a.source_record_id IN ({placeholders})
              AND u.target_region = ?
            """,
            (*record_ids, target),
        ).fetchall()
        by_id = {str(row["source_record_id"]): dict(row) for row in rows}
        return [by_id[record_id] for record_id in record_ids if record_id in by_id]

    def activity_by_record_id(self, record_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM activities WHERE source_record_id = ?",
            (record_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_metadata(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def set_metadata(self, key: str, value: str) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self.connection.commit()

    def get_auth_state(self, provider: str) -> bytes | None:
        row = self.connection.execute(
            "SELECT encrypted_payload FROM auth_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        if row is None:
            return None
        return bytes(row["encrypted_payload"])

    def set_auth_state(self, provider: str, encrypted_payload: bytes) -> None:
        self.connection.execute(
            """
            INSERT INTO auth_state(provider, encrypted_payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                encrypted_payload = excluded.encrypted_payload,
                updated_at = excluded.updated_at
            """,
            (provider, sqlite3.Binary(encrypted_payload), utc_now()),
        )
        self.connection.commit()

    def mark_download_started(self, record_id: str) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET download_status = 'downloading',
                download_attempts = download_attempts + 1,
                download_error = NULL,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (utc_now(), record_id),
        )
        self.connection.commit()

    def mark_download_success(
        self, record_id: str, fit_path: str, fit_sha256: str
    ) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET download_status = 'success',
                fit_path = ?,
                fit_sha256 = ?,
                download_error = NULL,
                processing_status = 'not_requested',
                processing_error = NULL,
                processed_fit_path = NULL,
                processed_fit_sha256 = NULL,
                garmin_product_id = NULL,
                garmin_software_version = NULL,
                coordinate_transform_enabled = 0,
                garminize_enabled = 0,
                coordinate_points_converted = 0,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (fit_path, fit_sha256, utc_now(), record_id),
        )
        self.connection.commit()

    def mark_download_failed(self, record_id: str, error: str) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET download_status = 'failed',
                download_error = ?,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (error[:4000], utc_now(), record_id),
        )
        self.connection.commit()

    def due_activities(
        self,
        targets: tuple[str, ...],
        max_retries: int,
        retry_auth: bool,
        record_id: str | None = None,
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in targets)
        statuses = ["pending", "failed"]
        if retry_auth:
            statuses.append("auth_required")
        status_placeholders = ",".join("?" for _ in statuses)
        now = utc_now()
        record_filter = ""
        parameters: list[Any] = [*targets, *statuses, max_retries, now]
        if record_id is not None:
            record_filter = "AND a.source_record_id = ?"
            parameters.append(record_id)
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT a.*
            FROM activities a
            JOIN uploads u ON u.source_record_id = a.source_record_id
            WHERE u.target_region IN ({placeholders})
              AND u.status IN ({status_placeholders})
              AND u.attempts < ?
              AND (u.next_retry_at IS NULL OR u.next_retry_at <= ?)
              {record_filter}
            ORDER BY
              CASE WHEN a.activity_time IS NULL THEN 1 ELSE 0 END,
              a.activity_time ASC,
              a.discovered_at ASC,
              a.source_record_id ASC
            """,
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def due_targets(
        self,
        record_id: str,
        targets: tuple[str, ...],
        max_retries: int,
        retry_auth: bool,
    ) -> list[str]:
        placeholders = ",".join("?" for _ in targets)
        statuses = ["pending", "failed"]
        if retry_auth:
            statuses.append("auth_required")
        status_placeholders = ",".join("?" for _ in statuses)
        rows = self.connection.execute(
            f"""
            SELECT target_region
            FROM uploads
            WHERE source_record_id = ?
              AND target_region IN ({placeholders})
              AND status IN ({status_placeholders})
              AND attempts < ?
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY target_region
            """,
            (
                record_id,
                *targets,
                *statuses,
                max_retries,
                utc_now(),
            ),
        ).fetchall()
        return [str(row["target_region"]) for row in rows]

    def mark_upload_started(self, record_id: str, target: str) -> int:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE uploads
            SET status = 'uploading',
                attempts = attempts + 1,
                last_error = NULL,
                updated_at = ?
            WHERE source_record_id = ? AND target_region = ?
            """,
            (now, record_id, target),
        )
        self.connection.commit()
        row = self.connection.execute(
            """
            SELECT attempts FROM uploads
            WHERE source_record_id = ? AND target_region = ?
            """,
            (record_id, target),
        ).fetchone()
        return int(row["attempts"])

    def mark_processing_started(
        self,
        record_id: str,
        *,
        garminize_enabled: bool,
        coordinate_transform_enabled: bool,
        product_id: int | None,
        software_version: int | None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET processing_status = 'processing',
                processing_error = NULL,
                processed_fit_path = NULL,
                processed_fit_sha256 = NULL,
                garmin_product_id = ?,
                garmin_software_version = ?,
                coordinate_transform_enabled = ?,
                garminize_enabled = ?,
                coordinate_points_converted = 0,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (
                product_id,
                software_version,
                int(coordinate_transform_enabled),
                int(garminize_enabled),
                utc_now(),
                record_id,
            ),
        )
        self.connection.commit()

    def mark_processing_success(
        self,
        record_id: str,
        fit_path: str,
        fit_sha256: str,
        product_id: int | None,
        software_version: int | None,
        garminize_enabled: bool,
        coordinate_transform_enabled: bool,
        coordinate_points_converted: int,
    ) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET processing_status = 'success',
                processing_error = NULL,
                processed_fit_path = ?,
                processed_fit_sha256 = ?,
                garmin_product_id = ?,
                garmin_software_version = ?,
                garminize_enabled = ?,
                coordinate_transform_enabled = ?,
                coordinate_points_converted = ?,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (
                fit_path,
                fit_sha256,
                product_id,
                software_version,
                int(garminize_enabled),
                int(coordinate_transform_enabled),
                coordinate_points_converted,
                utc_now(),
                record_id,
            ),
        )
        self.connection.commit()

    def mark_processing_failed(
        self,
        record_id: str,
        error: str,
        *,
        garminize_enabled: bool,
        coordinate_transform_enabled: bool,
        product_id: int | None,
        software_version: int | None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE activities
            SET processing_status = 'failed',
                processing_error = ?,
                processed_fit_path = NULL,
                processed_fit_sha256 = NULL,
                garmin_product_id = ?,
                garmin_software_version = ?,
                garminize_enabled = ?,
                coordinate_transform_enabled = ?,
                coordinate_points_converted = 0,
                updated_at = ?
            WHERE source_record_id = ?
            """,
            (
                error[:4000],
                product_id,
                software_version,
                int(garminize_enabled),
                int(coordinate_transform_enabled),
                utc_now(),
                record_id,
            ),
        )
        self.connection.commit()

    def requeue_activity(
        self,
        record_id: str,
        targets: tuple[str, ...],
    ) -> bool:
        placeholders = ",".join("?" for _ in targets)
        cursor = self.connection.execute(
            f"""
            UPDATE uploads
            SET status = 'pending',
                attempts = 0,
                last_error = NULL,
                remote_activity_id = NULL,
                next_retry_at = NULL,
                uploaded_at = NULL,
                updated_at = ?
            WHERE source_record_id = ?
              AND target_region IN ({placeholders})
            """,
            (utc_now(), record_id, *targets),
        )
        changed = cursor.rowcount > 0
        if changed:
            self.connection.execute(
                """
                UPDATE activities
                SET processing_status = 'not_requested',
                    processing_error = NULL,
                    processed_fit_path = NULL,
                    processed_fit_sha256 = NULL,
                    garmin_product_id = NULL,
                    garmin_software_version = NULL,
                    coordinate_transform_enabled = 0,
                    garminize_enabled = 0,
                    coordinate_points_converted = 0,
                    updated_at = ?
                WHERE source_record_id = ?
                """,
                (utc_now(), record_id),
            )
        self.connection.commit()
        return changed

    def record_reconciliation(
        self,
        record_id: str,
        target: str,
        status: str,
        message: str,
        remote_activity_id: str | None = None,
        *,
        requeue_missing: bool = False,
    ) -> bool:
        if status not in {"present", "missing", "ambiguous", "unverified"}:
            raise ValueError(f"未知核对状态: {status}")
        now = utc_now()
        with self.transaction():
            cursor = self.connection.execute(
                """
                UPDATE uploads
                SET reconciliation_status = ?,
                    reconciliation_checked_at = ?,
                    reconciliation_message = ?,
                    updated_at = ?
                WHERE source_record_id = ? AND target_region = ?
                """,
                (
                    status,
                    now,
                    message[:4000],
                    now,
                    record_id,
                    target,
                ),
            )
            if status == "present":
                self.connection.execute(
                    """
                    UPDATE uploads
                    SET status = CASE
                            WHEN status IN (
                                'pending', 'failed', 'auth_required', 'uploading'
                            ) THEN 'success'
                            ELSE status
                        END,
                        remote_activity_id = COALESCE(?, remote_activity_id),
                        last_error = NULL,
                        next_retry_at = NULL,
                        uploaded_at = COALESCE(uploaded_at, ?),
                        updated_at = ?
                    WHERE source_record_id = ? AND target_region = ?
                    """,
                    (remote_activity_id, now, now, record_id, target),
                )
            elif status == "missing" and requeue_missing:
                self.connection.execute(
                    """
                    UPDATE uploads
                    SET status = 'pending',
                        attempts = 0,
                        last_error = NULL,
                        remote_activity_id = NULL,
                        next_retry_at = NULL,
                        uploaded_at = NULL,
                        updated_at = ?
                    WHERE source_record_id = ? AND target_region = ?
                    """,
                    (now, record_id, target),
                )
        return cursor.rowcount > 0

    def mark_upload_success(
        self,
        record_id: str,
        target: str,
        remote_activity_id: str | None,
        duplicate: bool = False,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE uploads
            SET status = ?,
                last_error = NULL,
                remote_activity_id = ?,
                reconciliation_status = CASE
                    WHEN reconciliation_status = 'missing' THEN 'present'
                    ELSE reconciliation_status
                END,
                reconciliation_checked_at = CASE
                    WHEN reconciliation_status = 'missing' THEN ?
                    ELSE reconciliation_checked_at
                END,
                reconciliation_message = CASE
                    WHEN reconciliation_status = 'missing'
                        THEN '确认缺失后补同步成功'
                    ELSE reconciliation_message
                END,
                next_retry_at = NULL,
                uploaded_at = ?,
                updated_at = ?
            WHERE source_record_id = ? AND target_region = ?
            """,
            (
                "duplicate" if duplicate else "success",
                remote_activity_id,
                now,
                now,
                now,
                record_id,
                target,
            ),
        )
        self.connection.commit()

    def mark_upload_failed(
        self,
        record_id: str,
        target: str,
        error: str,
        next_retry_at: str,
        auth_required: bool,
    ) -> None:
        self.connection.execute(
            """
            UPDATE uploads
            SET status = ?,
                last_error = ?,
                next_retry_at = ?,
                updated_at = ?
            WHERE source_record_id = ? AND target_region = ?
            """,
            (
                "auth_required" if auth_required else "failed",
                error[:4000],
                next_retry_at,
                utc_now(),
                record_id,
                target,
            ),
        )
        self.connection.commit()

    def reset_stale_work(self) -> None:
        now = utc_now()
        self.connection.execute(
            """
            UPDATE activities
            SET download_status = 'failed',
                download_error = '上次运行在下载过程中中断',
                updated_at = ?
            WHERE download_status = 'downloading'
            """,
            (now,),
        )
        self.connection.execute(
            """
            UPDATE uploads
            SET status = 'failed',
                last_error = '上次运行在上传过程中中断',
                next_retry_at = ?,
                updated_at = ?
            WHERE status = 'uploading'
            """,
            (now, now),
        )
        self.connection.execute(
            """
            UPDATE activities
            SET processing_status = 'failed',
                processing_error = '上次运行在 FIT 预处理过程中中断',
                updated_at = ?
            WHERE processing_status = 'processing'
            """,
            (now,),
        )
        self.connection.commit()

    def summary(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT target_region, status, COUNT(*) AS count
            FROM uploads
            GROUP BY target_region, status
            ORDER BY target_region, status
            """
        ).fetchall()
        return [dict(row) for row in rows]
