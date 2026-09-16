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
                tokens = OneLapTokens("secret-access", "secret-refresh")
                store.save(tokens)

                encrypted = ledger.get_auth_state("onelap")
                self.assertIsNotNone(encrypted)
                self.assertNotIn(b"secret-access", encrypted)
                self.assertNotIn(b"secret-refresh", encrypted)
                self.assertEqual(store.load(), tokens)
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
