from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .garmin import UploadResult
from .ledger import Ledger
from .reconcile import (
    START_TOLERANCE_SECONDS,
    OneLapCandidate,
    ReconciliationMatch,
    reconcile_activities,
)
from .strava_reconcile import strava_activity


class StravaError(Exception):
    def __init__(self, message: str, auth_required: bool = False):
        super().__init__(message)
        self.auth_required = auth_required


class StravaUploader:
    """OAuth 上传；轮换凭据加密存储，异步任务跨运行继续查询。"""

    API = "https://www.strava.com/api/v3"
    TOKEN_URL = "https://www.strava.com/oauth/token"
    LIST_PAGE_SIZE = 200
    MAX_LIST_PAGES = 100

    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        settings.validate_for_strava()
        if not settings.strava_session_key:
            raise ValueError("Strava 会话加密需要 STRAVA_SESSION_KEY")
        self.settings = settings
        self.ledger = ledger
        self.session = session or requests.Session()
        self.sleep = sleep
        self.fernet = Fernet(
            base64.urlsafe_b64encode(
                hashlib.sha256(settings.strava_session_key.encode()).digest()
            )
        )
        # 修改初始凭据后使用新会话，允许重新授权或更换账号。
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    settings.strava_client_id,
                    settings.strava_client_secret,
                    settings.strava_refresh_token,
                ]
            ).encode()
        ).hexdigest()
        self.provider = f"strava:{fingerprint}"
        self.tokens: dict[str, Any] | None = None

    def _token(self, force: bool = False) -> str:
        if self.tokens is None:
            encrypted = self.ledger.get_auth_state(self.provider)
            if encrypted:
                try:
                    self.tokens = json.loads(self.fernet.decrypt(encrypted))
                    if not isinstance(self.tokens, dict):
                        raise ValueError("invalid session")
                except (InvalidToken, ValueError, TypeError):
                    raise StravaError(
                        "Strava 会话无法解密，请恢复 STRAVA_SESSION_KEY，或重新授权并更新 STRAVA_REFRESH_TOKEN",
                        True,
                    ) from None
            else:
                self.tokens = {}
        if (
            not force
            and self.tokens.get("access_token")
            and float(self.tokens.get("expires_at", 0)) > time.time() + 60
        ):
            return str(self.tokens["access_token"])
        payload = self._request(
            "POST",
            self.TOKEN_URL,
            token_request=True,
            data={
                "client_id": self.settings.strava_client_id,
                "client_secret": self.settings.strava_client_secret,
                "refresh_token": self.tokens.get("refresh_token")
                or self.settings.strava_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if (
            not payload.get("access_token")
            or not payload.get("refresh_token")
            or not isinstance(payload.get("expires_at"), (int, float))
        ):
            raise StravaError("Strava OAuth 响应缺少有效的会话字段", True)
        payload = {
            key: payload[key] for key in ("access_token", "refresh_token", "expires_at")
        }
        # 必须在上传前落库；刷新可能使旧 refresh token 失效。
        self.ledger.set_auth_state(
            self.provider, self.fernet.encrypt(json.dumps(payload).encode())
        )
        self.tokens = payload
        return str(payload["access_token"])

    def _request(
        self,
        method: str,
        url: str,
        *,
        token_request: bool = False,
        list_response: bool = False,
        **kwargs,
    ) -> Any:
        try:
            response = self.session.request(
                method, url, timeout=(15, 90), allow_redirects=False, **kwargs
            )
        except requests.RequestException:
            raise StravaError("Strava 网络请求失败或超时，稍后重试") from None
        if response.status_code == 429:
            raise StravaError("Strava API 达到请求限额，稍后按台账重试")
        if response.status_code in (401, 403) or (
            token_request and response.status_code == 400
        ):
            raise StravaError(
                "Strava 授权失效或权限不足，请重新授权并允许 "
                "activity:read_all 和 activity:write，更新凭据后重试", True
            )
        try:
            payload = response.json()
        except ValueError:
            raise StravaError(
                f"Strava 返回非 JSON 响应（HTTP {response.status_code}）"
            ) from None
        if list_response:
            if not 200 <= response.status_code < 300:
                raise StravaError(f"Strava 活动列表读取失败（HTTP {response.status_code}）")
            if not isinstance(payload, list) or any(
                not isinstance(item, dict) for item in payload
            ):
                raise StravaError("Strava 活动列表结构无效，无法确认缺失")
            return payload
        if not isinstance(payload, dict):
            raise StravaError("Strava 返回无效响应结构")
        # 某些重复上传在提交阶段即返回 400 + error。
        if response.status_code == 400 and self._duplicate(payload):
            return payload
        if not 200 <= response.status_code < 300:
            raise StravaError(f"Strava 请求失败（HTTP {response.status_code}）")
        return payload

    def has_pending_upload(self, record_id: str) -> bool:
        return self._id(
            self.ledger.get_metadata(f"{self.provider}:upload:{record_id}")
        ) is not None

    def _invalidate_token(self) -> None:
        if self.tokens is not None:
            self.tokens["expires_at"] = 0
            self.ledger.set_auth_state(
                self.provider, self.fernet.encrypt(json.dumps(self.tokens).encode())
            )

    def reconcile(
        self, candidates: list[OneLapCandidate]
    ) -> list[ReconciliationMatch]:
        """完整读取候选时间窗，再做批量一对一匹配；失败时不返回缺失。"""
        starts = [
            candidate.start_time_local
            for candidate in candidates
            if candidate.start_time_local is not None
        ]
        if not starts:
            return [
                ReconciliationMatch(
                    candidate.record_id,
                    "unverified",
                    None,
                    "OneLap 活动缺少有效开始时间，无法核对 Strava",
                )
                for candidate in candidates
            ]
        # 额外一秒覆盖 API 的开区间边界。
        padding = timedelta(seconds=START_TOLERANCE_SECONDS + 1)
        after = int((min(starts) - padding).replace(tzinfo=timezone.utc).timestamp())
        before = int((max(starts) + padding).replace(tzinfo=timezone.utc).timestamp())
        activities = {}
        try:
            headers = {"Authorization": f"Bearer {self._token()}"}
            for page in range(1, self.MAX_LIST_PAGES + 1):
                items = self._request(
                    "GET",
                    f"{self.API}/athlete/activities",
                    headers=headers,
                    list_response=True,
                    params={
                        "after": after,
                        "before": before,
                        "page": page,
                        "per_page": self.LIST_PAGE_SIZE,
                    },
                )
                if not items:
                    break
                for item in items:
                    normalized = strava_activity(item)
                    # 分页重复记录去重；无效 ID 仍保留，阻止误判缺失。
                    key = normalized.activity_id or f"invalid:{page}:{len(activities)}"
                    if key in activities and activities[key] != normalized:
                        raise StravaError("Strava 分页记录发生变化，稍后重新核对")
                    activities[key] = normalized
            else:
                raise StravaError(
                    "Strava 活动列表达到分页上限，无法确认完整，请缩小同步范围"
                )
        except StravaError as exc:
            if exc.auth_required:
                self._invalidate_token()
            raise
        return [
            ReconciliationMatch(
                result.record_id,
                result.status,
                result.remote_activity_id,
                result.message.replace("Garmin", "Strava"),
            )
            for result in reconcile_activities(candidates, activities.values())
        ]

    @staticmethod
    def _duplicate(payload: dict[str, Any]) -> bool:
        return bool(re.search(r"\bduplicate\b", str(payload.get("error") or ""), re.I))

    @staticmethod
    def _id(value: Any) -> str | None:
        text = str(value) if value is not None else ""
        return text if text.isascii() and text.isdigit() and int(text) > 0 else None

    def upload(self, record_id: str, fit_path: Path) -> UploadResult:
        pending_key = f"{self.provider}:upload:{record_id}"
        try:
            access_token = self._token()
            headers = {"Authorization": f"Bearer {access_token}"}
            upload_id = self._id(self.ledger.get_metadata(pending_key))
            if upload_id:
                payload = self._request(
                    "GET", f"{self.API}/uploads/{upload_id}", headers=headers
                )
            else:
                with fit_path.open("rb") as fit:
                    payload = self._request(
                        "POST",
                        f"{self.API}/uploads",
                        headers=headers,
                        data={
                            "data_type": "fit",
                            "external_id": f"onelap-{record_id}.fit",
                        },
                        files={
                            "file": (
                                f"onelap-{record_id}.fit",
                                fit,
                                "application/octet-stream",
                            )
                        },
                    )
                upload_id = self._id(payload.get("id_str") or payload.get("id"))
                if upload_id:
                    self.ledger.set_metadata(pending_key, upload_id)
            for poll in range(4):
                if self._duplicate(payload):
                    self.ledger.set_metadata(pending_key, "")
                    match = re.search(
                        r"activity\s+(\d+)", str(payload.get("error")), re.I
                    )
                    return UploadResult(
                        True,
                        True,
                        False,
                        match[1] if match else None,
                        "Strava 已存在该活动，按成功记账",
                    )
                if payload.get("error"):
                    # 确定的解析失败可以重新提交；不输出远端原始内容或凭据。
                    self.ledger.set_metadata(pending_key, "")
                    return UploadResult(
                        False,
                        False,
                        False,
                        None,
                        "Strava 无法解析活动文件，请检查 FIT 文件后重试",
                    )
                activity_id = self._id(payload.get("activity_id"))
                if activity_id:
                    self.ledger.set_metadata(pending_key, "")
                    return UploadResult(
                        True, False, False, activity_id, "Strava 上传完成"
                    )
                if not upload_id:
                    raise StravaError("Strava 未返回有效上传任务 ID")
                if poll < 3:
                    self.sleep(2)
                    payload = self._request(
                        "GET", f"{self.API}/uploads/{upload_id}", headers=headers
                    )
            return UploadResult(
                False,
                False,
                False,
                None,
                "Strava 仍在处理活动，下次运行继续查询同一上传任务",
            )
        except StravaError as exc:
            # 401 后下次尝试强制刷新，避免重复使用已撤销的 access token。
            if exc.auth_required:
                self._invalidate_token()
            return UploadResult(False, False, exc.auth_required, None, str(exc))
