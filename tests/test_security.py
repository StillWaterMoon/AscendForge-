import tempfile
import unittest
from pathlib import Path

from ascendforge.db import Database
from ascendforge.security import (
    CredentialStore,
    SecretBox,
    create_session,
    csrf_valid,
    hash_password,
    load_master_key,
    lookup_session,
    revoke_session,
    setup_token_valid,
    verify_password,
)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="af-sec-"))
        self.db = Database(self.tmp / "test.db")
        self.db.init()
        self.box = SecretBox(load_master_key(self.tmp / "key", ""))

    def test_password_roundtrip(self):
        encoded = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong password", encoded))
        self.assertNotIn("correct horse", encoded)
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_credentials_encrypted_at_rest(self):
        store = CredentialStore(self.db, self.box)
        payload = {"api_key": "sk-super-secret-value", "base_url": "https://x/v1", "model": "m"}
        store.put("model", payload)
        raw = self.db.query_one("SELECT ciphertext FROM credentials WHERE kind='model'")["ciphertext"]
        self.assertNotIn("sk-super-secret-value", raw)
        self.assertEqual(store.get("model")["api_key"], "sk-super-secret-value")

    def test_sessions(self):
        self.db.execute(
            "INSERT INTO users(id, username, password_hash, created_at) VALUES('u1','admin','x',1)"
        )
        raw, csrf = create_session(self.db, "u1", 3600)
        session = lookup_session(self.db, raw)
        self.assertIsNotNone(session)
        self.assertTrue(csrf_valid(session, csrf))
        self.assertFalse(csrf_valid(session, "wrong"))
        revoke_session(self.db, session["id"])
        self.assertIsNone(lookup_session(self.db, raw))

    def test_setup_token(self):
        self.assertTrue(setup_token_valid("abc", "abc"))
        self.assertFalse(setup_token_valid("abc", "abd"))
        self.assertFalse(setup_token_valid("", ""))


if __name__ == "__main__":
    unittest.main()
