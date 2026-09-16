from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ONELAP_BASE_APP_URL = "https://u.onelap.cn"
ONELAP_RECORD_PAGE_URL = f"{ONELAP_BASE_APP_URL}/recordPage"
ONELAP_LOGIN_API = f"{ONELAP_BASE_APP_URL}/api/login"
ONELAP_REFRESH_API = f"{ONELAP_BASE_APP_URL}/api/token"
ONELAP_LIST_API = f"{ONELAP_BASE_APP_URL}/api/otm/ride_record/list"
ONELAP_DETAIL_API = (
    f"{ONELAP_BASE_APP_URL}/api/otm/ride_record/analysis/{{record_id}}"
)
ONELAP_DOWNLOAD_API = (
    f"{ONELAP_BASE_APP_URL}/api/otm/ride_record/analysis/fit_content/{{fit_key}}"
)
ONELAP_SIGN_KEY = "fe9f8382418fcdeb136461cac6acae7b"
ONELAP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
ONELAP_REFRESH_MARGIN_SECONDS = 6 * 60 * 60
LOGGER = logging.getLogger(__name__)


class OneLapHTTPError(requests.HTTPError):
    """带有可操作说明且不会输出凭据的 OneLap HTTP 错误。"""


class OneLapAuthenticationError(RuntimeError):
    """所有可用认证恢复路径均失败。"""


@dataclass(frozen=True)
class OneLapTokens:
    token: str = ""
    refresh_token: str = ""


class OneLapTokenStore(Protocol):
    def load(self) -> OneLapTokens | None: ...

    def save(self, tokens: OneLapTokens) -> None: ...


def raise_for_onelap_status(
    response: requests.Response,
    operation: str,
) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        status = response.status_code
        if status == 401:
            message = (
                f"OneLap API 在{operation}时返回 401 Unauthorized。"
                "程序已尝试使用 refresh token 或账号密码恢复认证。"
                "请检查 Secrets.ONELAP_ACCOUNT / ONELAP_PASSWORD，"
                "必要时临时提供新的 ONELAP_TOKEN。"
            )
        elif status == 403:
            message = (
                f"OneLap API 在{operation}时返回 403 Forbidden。"
                "程序已尝试恢复认证；请检查 OneLap 账号密码，必要时临时提供"
                "新的 ONELAP_TOKEN。若本地可用而 GitHub Actions 持续失败，"
                "还需检查 OneLap 是否限制云端出口 IP 或触发了风控。"
            )
        else:
            message = f"OneLap API 在{operation}时返回 HTTP {status}。"
        raise OneLapHTTPError(
            message,
            response=response,
            request=response.request,
        ) from exc


def _replace_empty_with_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _replace_empty_with_none(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_empty_with_none(item) for item in value]
    return None if value == "" else value


def _normalize_sign_params(params: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, list):
            if value and isinstance(value[0], dict):
                result[key] = json.dumps(
                    value, ensure_ascii=False, separators=(",", ":")
                )
            else:
                result[key] = ",".join(str(item) for item in value)
        elif isinstance(value, dict):
            result[key] = json.dumps(
                value, ensure_ascii=False, separators=(",", ":")
            )
        else:
            result[key] = value
    return result


def generate_sign_headers(
    params: dict[str, Any],
    *,
    nonce: str | None = None,
    timestamp: str | None = None,
) -> dict[str, str]:
    actual_nonce = nonce or "".join(
        random.choice(string.ascii_letters + string.digits) for _ in range(16)
    )
    actual_timestamp = timestamp or str(int(time.time()))
    normalized = _normalize_sign_params(_replace_empty_with_none(params))
    all_params = {
        **normalized,
        "nonce": actual_nonce,
        "timestamp": actual_timestamp,
    }
    parts = [
        f"{key}={all_params[key]}"
        for key in sorted(all_params)
        if all_params[key] is not None
    ]
    text = "&".join(parts) + f"&key={ONELAP_SIGN_KEY}"
    signature = hashlib.md5(text.encode("utf-8")).hexdigest()
    return {
        "nonce": actual_nonce,
        "timestamp": actual_timestamp,
        "sign": signature,
    }


def md5_password(password: str) -> str:
    """按 OneLap 网页端行为对原始密码进行 UTF-8 MD5。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def token_expiry(token: str) -> float | None:
    """从 JWT payload 读取 exp；非 JWT 或格式异常时返回 None。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        expiry = float(payload["exp"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return expiry if expiry > 0 else None


def record_id_of(activity: dict[str, Any]) -> str:
    return str(
        activity.get("_id")
        or activity.get("id")
        or activity.get("record_id")
        or ""
    ).strip()


def activity_time_of(activity: dict[str, Any]) -> str | None:
    candidates = (
        activity.get("activity_time"),
        activity.get("start_riding_time"),
        activity.get("startTime"),
        activity.get("created_at"),
        activity.get("updated_at"),
        activity.get("date"),
    )
    for value in candidates:
        parsed = _parse_datetime(value)
        if parsed:
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        timestamp = value / 1000 if value > 10**11 else value
        parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        return parsed if parsed.year >= 2000 else None
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(
                tzinfo=datetime.now().astimezone().tzinfo
            )
            return parsed if parsed.year >= 2000 else None
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return parsed if parsed.year >= 2000 else None
    except ValueError:
        return None


def _extract_fit_key(detail_data: Any, activity: dict[str, Any]) -> str:
    candidates: list[tuple[int, str]] = []

    def add(value: Any, priority: int) -> None:
        if value is not None and str(value).strip():
            candidates.append((priority, str(value).strip()))

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key).lower()
                if lowered in {"fiturl", "fit_url"}:
                    add(item, 0)
                elif lowered in {"fit", "fitkey", "filekey", "file_key"}:
                    add(item, 1)
                elif lowered in {"url", "path"}:
                    add(item, 2)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(activity)
    walk(detail_data)
    seen: set[str] = set()
    for _, value in sorted(candidates, key=lambda item: item[0]):
        if value not in seen:
            return value
        seen.add(value)
    return ""


def _fit_download_candidates(fit_url: str) -> list[str]:
    candidates: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if value and value not in candidates:
            candidates.append(value)

    add(fit_url)
    add(unquote(fit_url))
    if fit_url.startswith(("http://", "https://")):
        parsed = urlparse(fit_url)
        add(parsed.path)
        if parsed.path:
            add(parsed.path.rsplit("/", 1)[-1])
    elif "/" in fit_url:
        add(fit_url.rsplit("/", 1)[-1])
    return candidates


@dataclass(frozen=True)
class ActivityPage:
    page: int
    items: list[dict[str, Any]]
    total: int
    total_pages: int

    @property
    def is_last(self) -> bool:
        if not self.items:
            return True
        if self.total_pages:
            return self.page >= self.total_pages
        if self.total:
            return self.page * 20 >= self.total
        return False


class OneLapClient:
    def __init__(
        self,
        token: str = "",
        refresh_token: str = "",
        account: str = "",
        password: str = "",
        token_store: OneLapTokenStore | None = None,
        session: requests.Session | None = None,
    ):
        self.session = session or self._build_session()
        self.account = account
        self.password = password
        self.token_store = token_store
        self.manual_tokens = OneLapTokens(
            token=token.strip(),
            refresh_token=refresh_token.strip(),
        )
        stored = token_store.load() if token_store is not None else None
        if stored is not None:
            self.tokens = OneLapTokens(
                token=stored.token or self.manual_tokens.token,
                refresh_token=(
                    stored.refresh_token or self.manual_tokens.refresh_token
                ),
            )
            self._pending_persist = self.tokens != stored
        else:
            self.tokens = self.manual_tokens
            self._pending_persist = bool(
                self.tokens.token or self.tokens.refresh_token
            )
        self.session.headers.update(
            {
                "User-Agent": ONELAP_USER_AGENT,
                "Origin": ONELAP_BASE_APP_URL,
                "Referer": ONELAP_RECORD_PAGE_URL,
            }
        )
        self._apply_authorization_header()

    @staticmethod
    def _build_session() -> requests.Session:
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        return session

    def _apply_authorization_header(self) -> None:
        if self.tokens.token:
            self.session.headers["Authorization"] = self.tokens.token
        else:
            self.session.headers.pop("Authorization", None)

    def _save_tokens(self) -> None:
        if self.token_store is not None:
            self.token_store.save(self.tokens)
        self._pending_persist = False

    def _adopt_tokens(
        self,
        token: str,
        refresh_token: str,
        *,
        persist: bool,
    ) -> None:
        self.tokens = OneLapTokens(
            token=token.strip(),
            refresh_token=refresh_token.strip(),
        )
        self._apply_authorization_header()
        self._pending_persist = not persist
        if persist:
            self._save_tokens()

    @staticmethod
    def _auth_payload(data: Any) -> dict[str, Any]:
        if isinstance(data, dict) and data.get("token"):
            return data
        if isinstance(data, dict):
            nested = data.get("data")
            if isinstance(nested, dict) and nested.get("token"):
                return nested
            # OneLap 当前登录接口会把唯一的会话对象放在 data 数组中。
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, dict) and item.get("token"):
                        return item
        raise OneLapAuthenticationError(
            "OneLap 认证响应中没有返回新的 access token。"
        )

    @staticmethod
    def _auth_response_json(
        response: requests.Response,
        operation: str,
    ) -> dict[str, Any]:
        if response.status_code >= 400:
            status = response.status_code
            if status == 429:
                detail = "请求过于频繁，可能触发 OneLap 限流或风控"
            elif status in (401, 403):
                detail = "凭据被拒绝或登录受到风控限制"
            else:
                detail = f"HTTP {status}"
            raise OneLapAuthenticationError(
                f"OneLap {operation}失败：{detail}。"
            )
        try:
            data = response.json()
        except (ValueError, requests.JSONDecodeError) as exc:
            raise OneLapAuthenticationError(
                f"OneLap {operation}没有返回有效 JSON。"
            ) from exc
        return OneLapClient._auth_payload(data)

    @staticmethod
    def _close_response(response: requests.Response) -> None:
        """关闭真实网络响应；兼容测试中未设置 raw 的轻量 Response。"""
        if getattr(response, "raw", None) is not None:
            response.close()

    def _refresh(self) -> None:
        if not self.tokens.token or not self.tokens.refresh_token:
            raise OneLapAuthenticationError(
                "当前会话缺少可用于刷新的 access token 或 refresh token。"
            )
        response = self.session.post(
            ONELAP_REFRESH_API,
            json={
                "token": self.tokens.refresh_token,
                "from": "web",
                "to": "web",
            },
            headers={"Authorization": self.tokens.token},
            timeout=30,
        )
        try:
            payload = self._auth_response_json(response, "刷新会话")
        finally:
            self._close_response(response)
        new_token = str(payload.get("token") or "").strip()
        new_refresh = str(
            payload.get("refresh_token") or self.tokens.refresh_token
        ).strip()
        self._adopt_tokens(new_token, new_refresh, persist=True)
        LOGGER.info("OneLap 会话刷新成功")

    def _login(self) -> None:
        if not self.account or not self.password:
            raise OneLapAuthenticationError(
                "未配置可用的 OneLap 账号和密码。"
            )
        response = self.session.post(
            ONELAP_LOGIN_API,
            json={
                "account": self.account,
                "password": md5_password(self.password),
            },
            # 登录不能携带已经失效的默认 Authorization。
            headers={"Authorization": None},
            timeout=30,
        )
        try:
            payload = self._auth_response_json(response, "账号密码登录")
        finally:
            self._close_response(response)
        self._adopt_tokens(
            str(payload.get("token") or ""),
            str(payload.get("refresh_token") or ""),
            persist=True,
        )
        LOGGER.info("OneLap 账号密码登录成功，已取得新会话")

    def _try_refresh(self) -> bool:
        if not self.tokens.token or not self.tokens.refresh_token:
            return False
        try:
            self._refresh()
            return True
        except (OneLapAuthenticationError, requests.RequestException) as exc:
            LOGGER.warning("OneLap refresh token 刷新失败：%s", exc)
            return False

    def _try_login(self) -> bool:
        if not self.account or not self.password:
            return False
        try:
            self._login()
            return True
        except (OneLapAuthenticationError, requests.RequestException) as exc:
            LOGGER.warning("OneLap 账号密码登录失败：%s", exc)
            return False

    def _manual_token_available(self) -> bool:
        token = self.manual_tokens.token
        if not token or token == self.tokens.token:
            return False
        expiry = token_expiry(token)
        return expiry is None or expiry > time.time()

    def _adopt_manual_tokens(self) -> bool:
        if not self._manual_token_available():
            return False
        self._adopt_tokens(
            self.manual_tokens.token,
            self.manual_tokens.refresh_token,
            persist=False,
        )
        LOGGER.info("已采用手工提供的 OneLap access token")
        return True

    def _recover_after_rejection(self) -> bool:
        if self._try_refresh():
            return True
        if self._adopt_manual_tokens():
            return True
        return self._try_login()

    def ensure_authenticated(self) -> None:
        if not self.tokens.token:
            if self._adopt_manual_tokens() or self._try_login():
                return
            raise OneLapAuthenticationError(
                "无法建立 OneLap 会话：没有可用的 access token，"
                "也无法通过账号密码登录。"
            )

        expiry = token_expiry(self.tokens.token)
        if expiry is None:
            return
        remaining = expiry - time.time()
        if remaining > ONELAP_REFRESH_MARGIN_SECONDS:
            return
        if self._try_refresh() or self._adopt_manual_tokens() or self._try_login():
            return
        if remaining > 0:
            LOGGER.warning(
                "OneLap access token 即将过期，但目前无法自动续期；"
                "本次继续使用尚未过期的 token"
            )
            return
        raise OneLapAuthenticationError(
            "OneLap access token 已过期，且 refresh token、手工 token "
            "和账号密码登录均无法恢复认证。"
        )

    def _request(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        headers_factory: Callable[[], dict[str, str]] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        self.ensure_authenticated()

        def send() -> requests.Response:
            headers = headers_factory() if headers_factory is not None else None
            return getattr(self.session, method.lower())(
                url,
                headers=headers,
                **kwargs,
            )

        response = send()
        if response.status_code in (401, 403) and self._recover_after_rejection():
            self._close_response(response)
            response = send()
        raise_for_onelap_status(response, operation)
        if self._pending_persist:
            self._save_tokens()
        return response

    def fetch_page(self, page: int, page_size: int = 20) -> ActivityPage:
        payload = {"page": page, "limit": page_size}
        response = self._request(
            "POST",
            ONELAP_LIST_API,
            "读取活动列表",
            json=payload,
            headers_factory=lambda: generate_sign_headers(payload),
            timeout=30,
        )
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("OneLap 活动列表返回的不是 JSON 对象")
        page_data = data.get("data")
        if not isinstance(page_data, dict):
            message = data.get("message") or data.get("msg") or "认证可能已失效"
            raise RuntimeError(f"OneLap 活动列表返回格式异常：{message}")
        items = page_data.get("list") or []
        if not isinstance(items, list):
            raise RuntimeError("OneLap 活动列表返回格式异常")
        return ActivityPage(
            page=page,
            items=[item for item in items if isinstance(item, dict)],
            total=int(page_data.get("total") or 0),
            total_pages=int(page_data.get("pages") or 0),
        )

    def iter_pages(
        self, start_page: int = 1, max_pages: int = 0
    ) -> Iterator[ActivityPage]:
        page_number = max(1, start_page)
        fetched = 0
        while max_pages == 0 or fetched < max_pages:
            page = self.fetch_page(page_number)
            yield page
            fetched += 1
            if page.is_last:
                break
            page_number += 1
            time.sleep(0.2)

    def download_fit(self, activity: dict[str, Any], output_dir: Path) -> Path:
        record_id = record_id_of(activity)
        if not record_id:
            raise ValueError("OneLap 活动缺少 record_id")

        safe_record_id = re.sub(r"[^A-Za-z0-9._-]+", "_", record_id)
        output_path = output_dir / f"{safe_record_id}.fit"
        part_path = output_path.with_suffix(".fit.part")
        if output_path.exists() and self.validate_fit(output_path):
            return output_path

        detail_response = self._request(
            "GET",
            ONELAP_DETAIL_API.format(record_id=record_id),
            f"读取活动 {record_id} 详情",
            timeout=30,
        )
        fit_url = _extract_fit_key(detail_response.json(), activity)
        if not fit_url:
            raise RuntimeError(f"OneLap 活动 {record_id} 没有可下载的 FIT 地址")

        last_error: Exception | None = None
        for candidate in _fit_download_candidates(fit_url):
            fit_key = base64.b64encode(candidate.encode("utf-8")).decode("ascii")
            response: requests.Response | None = None
            try:
                response = self._request(
                    "GET",
                    ONELAP_DOWNLOAD_API.format(fit_key=fit_key),
                    f"下载活动 {record_id} 的 FIT 文件",
                    timeout=60,
                    stream=True,
                )
                if part_path.exists():
                    part_path.unlink()
                with part_path.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            file_handle.write(chunk)
                if not self.validate_fit(part_path):
                    raise RuntimeError("下载结果不是有效 FIT 文件")
                part_path.replace(output_path)
                return output_path
            except OneLapHTTPError:
                if part_path.exists():
                    part_path.unlink()
                raise
            except Exception as exc:
                last_error = exc
                if part_path.exists():
                    part_path.unlink()
            finally:
                if response is not None:
                    self._close_response(response)
        raise RuntimeError(f"OneLap 活动 {record_id} 下载失败: {last_error}")

    @staticmethod
    def validate_fit(path: Path) -> bool:
        try:
            if path.stat().st_size < 12:
                return False
            with path.open("rb") as file_handle:
                header = file_handle.read(12)
            return header[8:12] == b".FIT"
        except OSError:
            return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
