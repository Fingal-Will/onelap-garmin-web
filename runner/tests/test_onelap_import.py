"""Activity discovery must import fresh App records before reading OTM's list."""
import unittest
from unittest.mock import Mock, patch

import requests

from onelap_garmin_sync.onelap import (
    ONELAP_IMPORT_API, ONELAP_LIST_API, ONELAP_LOGIN_API, ONELAP_REFRESH_API,
    ActivityPage, OneLapClient, OneLapHTTPError, OneLapTokens,
)
from test_onelap import FakeStore, response


def imported(inserted=2, total=20, failed=0):
    return response(200, {"code": 200, "data": {"sync": {
        "total": total, "inserted": inserted,
        "skipped": total - inserted, "failed": failed,
    }}}, url=ONELAP_IMPORT_API)


def listed(items=None, has_more=False):
    return response(200, {"code": 200, "data": {
        "list": items or [], "pagination": {
            "total": 117, "total_pages": 6, "has_more": has_more,
        },
    }})


class OneLapImportTest(unittest.TestCase):
    def client(self, responses, uid="123", **kwargs):
        session = requests.Session()
        session.post = Mock(side_effect=responses)
        store = kwargs.pop("token_store", FakeStore(OneLapTokens("access", "refresh", uid)))
        return OneLapClient(session=session, token_store=store, **kwargs), session, store

    def test_imports_before_list_and_only_once_across_pages(self):
        client, session, _ = self.client([
            imported(), listed([{"id": "new-ride"}], True), listed(),
        ])
        page = client.fetch_page(1)
        client.fetch_page(2)
        self.assertEqual(page.items, [{"id": "new-ride"}])
        self.assertEqual([call.args[0] for call in session.post.call_args_list],
                         [ONELAP_IMPORT_API, ONELAP_LIST_API, ONELAP_LIST_API])
        self.assertEqual(session.post.call_args_list[0].kwargs["json"], {"uid": "123"})
        self.assertEqual(page.total, 117)
        self.assertEqual(page.total_pages, 6)
        self.assertFalse(page.is_last)

    def test_old_cached_session_logs_in_once_and_persists_uid(self):
        for data in [
            {"token": "new-access", "refresh_token": "new-refresh", "userinfo": {"uid": 123}},
            [{"token": "new-access", "refresh_token": "new-refresh", "userinfo": {"uid": "123"}}],
        ]:
            with self.subTest(shape=type(data).__name__):
                client, session, store = self.client([
                    response(200, {"code": 200, "data": data}, url=ONELAP_LOGIN_API),
                    imported(), listed(), listed(),
                ], uid="", account="user", password="password")
                client.fetch_page(1)
                client.fetch_page(2)
                self.assertEqual([call.args[0] for call in session.post.call_args_list],
                                 [ONELAP_LOGIN_API, ONELAP_IMPORT_API, ONELAP_LIST_API, ONELAP_LIST_API])
                self.assertEqual(store.saved[-1], OneLapTokens("new-access", "new-refresh", "123"))
                self.assertEqual(session.post.call_args_list[1].kwargs["json"], {"uid": "123"})

    def test_fresh_login_does_not_login_twice_for_uid(self):
        client, session, _ = self.client([
            response(200, {"data": [{"token": "new-access", "userinfo": {"uid": "123"}}]}, url=ONELAP_LOGIN_API),
            imported(), listed(),
        ], token_store=FakeStore(), account="user", password="password")
        client.fetch_page(1)
        self.assertEqual([call.args[0] for call in session.post.call_args_list],
                         [ONELAP_LOGIN_API, ONELAP_IMPORT_API, ONELAP_LIST_API])

    def test_cached_uid_avoids_password_login(self):
        client, session, _ = self.client([imported(), listed()], account="user", password="password")
        client.fetch_page(1)
        self.assertNotIn(ONELAP_LOGIN_API, [call.args[0] for call in session.post.call_args_list])

    def test_refresh_during_import_preserves_uid_and_retries(self):
        client, session, store = self.client([
            response(401, {}, url=ONELAP_IMPORT_API),
            response(200, {"token": "renewed"}, url=ONELAP_REFRESH_API),
            imported(), listed(),
        ])
        client.fetch_page(1)
        self.assertEqual(client.tokens.uid, "123")
        self.assertEqual(store.saved[-1].uid, "123")
        self.assertEqual([call.args[0] for call in session.post.call_args_list],
                         [ONELAP_IMPORT_API, ONELAP_REFRESH_API, ONELAP_IMPORT_API, ONELAP_LIST_API])

    def test_full_import_window_retries_until_no_new_records(self):
        client, session, _ = self.client([imported(20), imported(0), listed()])
        client.fetch_page(1)
        self.assertEqual(session.post.call_count, 3)

    def test_full_import_windows_are_bounded_to_three_rounds(self):
        client, session, _ = self.client([imported(20), imported(20), imported(20), listed()])
        with self.assertLogs("onelap_garmin_sync.onelap", level="WARNING"):
            client.fetch_page(1)
        self.assertEqual(session.post.call_count, 4)

    def test_import_errors_do_not_query_list_or_leak_server_text(self):
        bad_responses = [
            response(200, {"code": 500, "message": "secret-access"}, url=ONELAP_IMPORT_API),
            response(200, {"code": 200, "data": {}}, url=ONELAP_IMPORT_API),
            response(200, {"code": 200, "data": {"sync": {"total": "20"}}}, url=ONELAP_IMPORT_API),
            imported(0, failed=1),
        ]
        malformed = response(200, {}, url=ONELAP_IMPORT_API)
        malformed._content = b"not-json secret-access"
        bad_responses.append(malformed)
        for bad_response in bad_responses:
            with self.subTest(body=bad_response.status_code):
                client, session, _ = self.client([bad_response])
                with self.assertRaises(RuntimeError) as raised:
                    client.fetch_page(1)
                self.assertNotIn("secret-access", str(raised.exception))
                self.assertEqual(session.post.call_count, 1)
                self.assertFalse(client._latest_imported)

    def test_import_http_error_is_actionable_and_stops_discovery(self):
        client, session, _ = self.client([response(500, {}, url=ONELAP_IMPORT_API)])
        with self.assertRaises(OneLapHTTPError) as raised:
            client.fetch_page(1)
        self.assertIn("导入最新骑行", str(raised.exception))
        self.assertEqual(session.post.call_count, 1)

    def test_failed_import_can_be_retried_on_same_client(self):
        client, session, _ = self.client([requests.Timeout(), imported(), listed()])
        with self.assertRaises(requests.Timeout):
            client.fetch_page(1)
        client.fetch_page(1)
        self.assertEqual(session.post.call_count, 3)

    def test_login_without_uid_fails_explicitly(self):
        client, session, _ = self.client([
            response(200, {"token": "new-access", "userinfo": {}}, url=ONELAP_LOGIN_API),
        ], uid="", account="user", password="password")
        with self.assertRaisesRegex(RuntimeError, "缺少账号 UID"):
            client.fetch_page(1)
        self.assertEqual(session.post.call_count, 1)

    def test_token_only_mode_warns_when_uid_is_unavailable(self):
        client, session, _ = self.client([listed(), listed()], uid="")
        with self.assertLogs("onelap_garmin_sync.onelap", level="WARNING") as logs:
            client.fetch_page(1)
            client.fetch_page(2)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("仅读取 OTM 已有记录", logs.output[0])
        self.assertTrue(all(call.args[0] == ONELAP_LIST_API for call in session.post.call_args_list))

    def test_business_error_in_list_is_not_treated_as_empty_success(self):
        client, _, _ = self.client([imported(), response(200, {"code": 500, "data": {"list": []}})])
        with self.assertRaisesRegex(RuntimeError, "活动列表查询失败"):
            client.fetch_page(1)

    def test_pagination_has_more_overrides_total_pages(self):
        page = ActivityPage(1, [{"id": "ride"}], 117, 6, has_more=False)
        self.assertTrue(page.is_last)

    def test_legacy_pagination_uses_requested_page_size(self):
        self.assertTrue(ActivityPage(1, [{"id": "ride"}], 115, 0, page_size=200).is_last)

    def test_iter_pages_stops_at_nested_pagination_end(self):
        client, session, _ = self.client([imported(), listed([{"id": "ride"}])])
        self.assertEqual(len(list(client.iter_pages())), 1)
        self.assertEqual(session.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
