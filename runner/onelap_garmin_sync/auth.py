from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import asdict

from cryptography.fernet import Fernet, InvalidToken

from .ledger import Ledger
from .onelap import OneLapTokens

LOGGER = logging.getLogger(__name__)
ONELAP_AUTH_PROVIDER = "onelap"


class EncryptedOneLapSessionStore:
    """把 OneLap 会话加密保存在现有 SQLite 台账中。"""

    def __init__(self, ledger: Ledger, session_key: str):
        self.ledger = ledger
        self.enabled = bool(session_key)
        self._fernet = (
            Fernet(
                base64.urlsafe_b64encode(
                    hashlib.sha256(session_key.encode("utf-8")).digest()
                )
            )
            if self.enabled
            else None
        )

    def load(self) -> OneLapTokens | None:
        if self._fernet is None:
            return None
        encrypted = self.ledger.get_auth_state(ONELAP_AUTH_PROVIDER)
        if encrypted is None:
            return None
        try:
            decoded = self._fernet.decrypt(encrypted)
            payload = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "SQLite 中的 OneLap 加密会话无法读取，将重新获取认证会话"
            )
            return None
        if not isinstance(payload, dict):
            return None
        token = str(payload.get("token") or "").strip()
        refresh_token = str(payload.get("refresh_token") or "").strip()
        if not token and not refresh_token:
            return None
        return OneLapTokens(token=token, refresh_token=refresh_token)

    def save(self, tokens: OneLapTokens) -> None:
        if self._fernet is None:
            return
        payload = json.dumps(
            asdict(tokens),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.ledger.set_auth_state(
            ONELAP_AUTH_PROVIDER,
            self._fernet.encrypt(payload),
        )
