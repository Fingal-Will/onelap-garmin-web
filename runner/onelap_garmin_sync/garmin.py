from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class UploadResult:
    success: bool
    duplicate: bool
    auth_required: bool
    remote_activity_id: str | None
    message: str


@dataclass(frozen=True)
class ActivityListResult:
    success: bool
    auth_required: bool
    activities: list[dict[str, Any]]
    truncated: bool
    message: str


class GarminUploader:
    def __init__(
        self,
        script_path: Path,
        activity_script_path: Path | None = None,
    ):
        self.script_path = script_path
        self.activity_script_path = (
            activity_script_path
            or script_path.with_name("garmin-activities.js")
        )

    def upload(self, region: str, fit_path: Path) -> UploadResult:
        process, execution_error = self._run_tool(
            [
                "node",
                str(self.script_path),
                "--region",
                region,
                "--file",
                str(fit_path),
            ],
            self.script_path.parent.parent,
            "Garmin 上传器",
        )
        if process is None:
            return UploadResult(
                success=False,
                duplicate=False,
                auth_required=False,
                remote_activity_id=None,
                message=execution_error,
            )
        payload = self._extract_result(process.stdout)
        if payload is None:
            message = (process.stderr or process.stdout or "Garmin 上传器无输出").strip()
            return UploadResult(
                success=False,
                duplicate=False,
                auth_required=self._looks_like_auth_error(message),
                remote_activity_id=None,
                message=message[-4000:],
            )

        message = str(payload.get("message") or "")
        return UploadResult(
            success=bool(payload.get("ok")),
            duplicate=bool(payload.get("duplicate")),
            auth_required=bool(payload.get("authRequired")),
            remote_activity_id=(
                str(payload["remoteActivityId"])
                if payload.get("remoteActivityId") is not None
                else None
            ),
            message=message,
        )

    def list_activities(
        self,
        region: str,
        start_date: str,
        end_date: str,
    ) -> ActivityListResult:
        process, execution_error = self._run_tool(
            [
                "node",
                str(self.activity_script_path),
                "--region",
                region,
                "--start-date",
                start_date,
                "--end-date",
                end_date,
            ],
            self.activity_script_path.parent.parent,
            "Garmin 活动列表读取器",
        )
        if process is None:
            return ActivityListResult(
                success=False,
                auth_required=False,
                activities=[],
                truncated=False,
                message=execution_error,
            )
        payload = self._extract_typed_result(
            process.stdout,
            "garmin-activities-result",
        )
        if payload is None:
            message = (
                process.stderr
                or process.stdout
                or "Garmin 活动列表读取器无输出"
            ).strip()
            return ActivityListResult(
                success=False,
                auth_required=self._looks_like_auth_error(message),
                activities=[],
                truncated=False,
                message=message[-4000:],
            )
        activities = payload.get("activities")
        return ActivityListResult(
            success=bool(payload.get("ok")),
            auth_required=bool(payload.get("authRequired")),
            activities=(
                [item for item in activities if isinstance(item, dict)]
                if isinstance(activities, list)
                else []
            ),
            truncated=bool(payload.get("truncated")),
            message=str(payload.get("message") or ""),
        )

    @staticmethod
    def _run_tool(
        command: list[str],
        cwd: Path,
        operation: str,
    ) -> tuple[subprocess.CompletedProcess[str] | None, str]:
        try:
            process = subprocess.run(
                command,
                cwd=cwd,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return None, f"{operation}运行超时（180 秒）"
        except OSError as exc:
            return None, f"{operation}无法启动：{exc}"
        return process, ""

    @staticmethod
    def _extract_result(stdout: str) -> dict[str, Any] | None:
        return GarminUploader._extract_typed_result(
            stdout,
            "garmin-upload-result",
        )

    @staticmethod
    def _extract_typed_result(
        stdout: str,
        result_type: str,
    ) -> dict[str, Any] | None:
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("type") == result_type
            ):
                return payload
        return None

    @staticmethod
    def _looks_like_auth_error(message: str) -> bool:
        lowered = message.lower()
        return any(
            marker in lowered
            for marker in (
                "401",
                "403",
                "unauthorized",
                "forbidden",
                "oauth",
                "token expired",
                "login",
                "sign in",
                "authentication",
            )
        )
