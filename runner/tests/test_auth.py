import tempfile
import unittest
from pathlib import Path

from onelap_garmin_sync.auth import EncryptedOneLapSessionStore
from onelap_garmin_sync.ledger import Ledger
from onelap_garmin_sync.onelap import OneLapTokens


class AuthStoreTest(unittest.TestCase):
    def test_encrypted_session_round_trip_does_not_store_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Ledger(Path(temporary) / "sync.db")
            try:
                store = EncryptedOneLapSessionStore(ledger, "session-key")
                tokens = OneLapTokens("secret-access", "secret-refresh", "secret-uid")
                store.save(tokens)

                encrypted = ledger.get_auth_state("onelap")
                self.assertIsNotNone(encrypted)
                self.assertNotIn(b"secret-access", encrypted)
                self.assertNotIn(b"secret-refresh", encrypted)
                self.assertNotIn(b"secret-uid", encrypted)
                self.assertEqual(store.load(), tokens)
            finally:
                ledger.close()

    def test_old_encrypted_session_without_uid_is_still_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Ledger(Path(temporary) / "sync.db")
            try:
                store = EncryptedOneLapSessionStore(ledger, "session-key")
                # Exact schema written before UID persistence was introduced.
                ledger.set_auth_state("onelap", store._fernet.encrypt(
                    b'{"token":"old-access","refresh_token":"old-refresh"}'
                ))
                self.assertEqual(store.load(), OneLapTokens("old-access", "old-refresh", ""))
            finally:
                ledger.close()

    def test_wrong_key_treats_old_session_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Ledger(Path(temporary) / "sync.db")
            try:
                EncryptedOneLapSessionStore(ledger, "old-key").save(
                    OneLapTokens("access", "refresh")
                )
                self.assertIsNone(
                    EncryptedOneLapSessionStore(ledger, "new-key").load()
                )
            finally:
                ledger.close()

    def test_empty_key_disables_persistence_without_rejecting_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Ledger(Path(temporary) / "sync.db")
            try:
                store = EncryptedOneLapSessionStore(ledger, "")
                store.save(OneLapTokens("access", "refresh"))
                self.assertIsNone(store.load())
                self.assertIsNone(ledger.get_auth_state("onelap"))
            finally:
                ledger.close()


if __name__ == "__main__":
    unittest.main()
