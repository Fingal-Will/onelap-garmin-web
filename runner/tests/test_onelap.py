import base64
import hashlib
import json
import time
import unittest
from unittest.mock import Mock

import requests

from onelap_garmin_sync.onelap import (
    ONELAP_LIST_API,
    ONELAP_LOGIN_API,
    ONELAP_REFRESH_API,
    ONELAP_SIGN_KEY,
    OneLapAuthenticationError,
    OneLapClient,
    OneLapHTTPError,
    OneLapTokens,
    generate_sign_headers,
    md5_password,
    token_expiry,
)


def response(status, body, method="POST", url=ONELAP_LIST_API):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode("utf-8")
    result.request = requests.Request(method, url).prepare()
    return result


def jwt_with_exp(expiry):
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
        .decode()
        .rstrip("=")
    )
    return f"{header}.{payload}.signature"


class FakeStore:
    def __init__(self, stored=None):
        self.stored = stored
        self.saved = []

    def load(self):
        return self.stored

    def save(self, tokens):
        self.stored = tokens
        self.saved.append(tokens)


class OneLapTest(unittest.TestCase):
    def test_sign_headers_are_deterministic_with_fixed_inputs(self):
        headers = generate_sign_headers(
            {"page": 1, "limit": 20},
            nonce="abc",
            timestamp="123",
        )
        expected_text = f"limit=20&nonce=abc&page=1&timestamp=123&key={ONELAP_SIGN_KEY}"
        self.assertEqual(
            headers,
            {
                "nonce": "abc",
                "timestamp": "123",
                "sign": hashlib.md5(expected_text.encode()).hexdigest(),
            },
        )

    def test_password_md5_matches_web_login_format(self):
        self.assertEqual(
            md5_password("password"),
            hashlib.md5(b"password").hexdigest(),
        )
        self.assertNotEqual(md5_password(" password "), md5_password("password"))

    def test_token_expiry_reads_jwt_and_ignores_opaque_token(self):
        expiry = time.time() + 3600
        self.assertAlmostEqual(token_expiry(jwt_with_exp(expiry)), expiry)
        self.assertIsNone(token_expiry("opaque-token"))

    def test_fetch_page_uses_raw_token_and_signed_post(self):
        session = requests.Session()
        session.post = Mock(
            return_value=response(
                200,
                {"data": {"list": [], "total": 0, "pages": 0}},
            )
        )

        client = OneLapClient(token="raw-token", session=session)
        page = client.fetch_page(1)

        self.assertEqual(client.session.headers["Authorization"], "raw-token")
        self.assertEqual(page.total, 0)
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"], {"page": 1, "limit": 20})
        self.assertIn("sign", kwargs["headers"])

    def test_manual_access_token_does_not_require_refresh_token(self):
        session = requests.Session()
        session.post = Mock(
            return_value=response(
                200,
                {"data": {"list": [], "total": 0, "pages": 0}},
            )
        )
        store = FakeStore()

        OneLapClient(
            token="access-only",
            refresh_token="",
            token_store=store,
            session=session,
        ).fetch_page(1)

        self.assertEqual(store.saved, [OneLapTokens("access-only", "")])

    def test_login_hashes_plaintext_password_and_saves_both_tokens(self):
        session = requests.Session()
        session.post = Mock(
            side_effect=[
                response(
                    200,
                    {
                        "code": 200,
                        "data": [
                            {
                                "token": "login-access",
                                "refresh_token": "login-refresh",
                                "userinfo": {},
                            }
                        ],
                        "error": "success",
                    },
                    url=ONELAP_LOGIN_API,
                ),
                response(
                    200,
                    {"data": {"list": [], "total": 0, "pages": 0}},
                ),
            ]
        )
        store = FakeStore()

        client = OneLapClient(
            account="user",
            password="plain password",
            token_store=store,
            session=session,
        )
        client.fetch_page(1)

        login_call = session.post.call_args_list[0]
        self.assertEqual(login_call.args[0], ONELAP_LOGIN_API)
        self.assertEqual(
            login_call.kwargs["json"],
            {
                "account": "user",
                "password": hashlib.md5(b"plain password").hexdigest(),
            },
        )
        self.assertNotIn("plain password", str(store.saved))
        self.assertEqual(
            store.saved,
            [OneLapTokens("login-access", "login-refresh")],
        )

    def test_401_refreshes_once_and_retries_original_request(self):
        session = requests.Session()
        session.post = Mock(
            side_effect=[
                response(401, {"message": "expired"}),
                response(
                    200,
                    {
                        "token": "new-access",
                        "refresh_token": "new-refresh",
                    },
                    url=ONELAP_REFRESH_API,
                ),
                response(
                    200,
                    {"data": {"list": [], "total": 0, "pages": 0}},
                ),
            ]
        )
        store = FakeStore(OneLapTokens("old-access", "old-refresh"))
        client = OneLapClient(token_store=store, session=session)

        client.fetch_page(1)

        self.assertEqual(
            [call.args[0] for call in session.post.call_args_list],
            [ONELAP_LIST_API, ONELAP_REFRESH_API, ONELAP_LIST_API],
        )
        self.assertEqual(
            store.saved,
            [OneLapTokens("new-access", "new-refresh")],
        )
        self.assertEqual(
            client.session.headers["Authorization"],
            "new-access",
        )

    def test_token_with_less_than_six_hours_left_refreshes_before_request(self):
        session = requests.Session()
        session.post = Mock(
            side_effect=[
                response(
                    200,
                    {"token": "new-access"},
                    url=ONELAP_REFRESH_API,
                ),
                response(
                    200,
                    {"data": {"list": [], "total": 0, "pages": 0}},
                ),
            ]
        )
        store = FakeStore(
            OneLapTokens(
                jwt_with_exp(time.time() + 60 * 60),
                "old-refresh",
            )
        )
        client = OneLapClient(token_store=store, session=session)

        client.fetch_page(1)

        self.assertEqual(
            [call.args[0] for call in session.post.call_args_list],
            [ONELAP_REFRESH_API, ONELAP_LIST_API],
        )
        self.assertEqual(
            store.saved,
            [OneLapTokens("new-access", "old-refresh")],
        )

    def test_refresh_failure_falls_back_to_account_password_login(self):
        session = requests.Session()
        session.post = Mock(
            side_effect=[
                response(401, {"message": "expired"}),
                response(401, {"message": "refresh expired"}, url=ONELAP_REFRESH_API),
                response(
                    200,
                    {
                        "data": {
                            "token": "login-access",
                            "refresh_token": "login-refresh",
                        }
                    },
                    url=ONELAP_LOGIN_API,
                ),
                response(
                    200,
                    {"data": {"list": [], "total": 0, "pages": 0}},
                ),
            ]
        )
        store = FakeStore(OneLapTokens("old-access", "old-refresh"))
        client = OneLapClient(
            account="user",
            password="password",
            token_store=store,
            session=session,
        )

        client.fetch_page(1)

        self.assertEqual(
            [call.args[0] for call in session.post.call_args_list],
            [
                ONELAP_LIST_API,
                ONELAP_REFRESH_API,
                ONELAP_LOGIN_API,
                ONELAP_LIST_API,
            ],
        )
        self.assertEqual(
            store.saved,
            [OneLapTokens("login-access", "login-refresh")],
        )

    def test_refresh_token_alone_is_allowed_until_authentication_is_needed(self):
        client = OneLapClient(refresh_token="refresh-only")
        with self.assertRaises(OneLapAuthenticationError):
            client.ensure_authenticated()

    def test_fetch_page_converts_403_to_actionable_error_without_secret(self):
        forbidden = response(
            403,
            {"message": "forbidden: raw-token"},
        )
        session = requests.Session()
        session.post = Mock(return_value=forbidden)
        client = OneLapClient(token="raw-token", session=session)

        with self.assertRaises(OneLapHTTPError) as raised:
            client.fetch_page(1)

        message = str(raised.exception)
        self.assertIn("403 Forbidden", message)
        self.assertIn("ONELAP_TOKEN", message)
        self.assertNotIn("raw-token", message)


if __name__ == "__main__":
    unittest.main()
