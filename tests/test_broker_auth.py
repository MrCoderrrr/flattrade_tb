"""Offline tests for the Flattrade code exchange and private token sync."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from strategy_lab.broker_auth import BrokerAuthError, BrokerTokenSync, extract_request_code


class BrokerAuthTests(unittest.TestCase):
    def test_extracts_redirect_code_without_fetching_redirect_url(self):
        self.assertEqual(extract_request_code("https://example.test/callback?request_code=ABC123&state=1"), "ABC123")
        self.assertEqual(extract_request_code("code=ABC123"), "ABC123")
        with self.assertRaises(BrokerAuthError):
            extract_request_code("https://example.test/callback?request_code=one&request_code=two")
        with self.assertRaises(BrokerAuthError):
            extract_request_code("https://example.test/callback")

    def test_exchange_saves_atomically_and_returns_no_token(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "token.txt"
            path.write_text("old-token-unchanged")
            seen = []

            def opener(request, timeout):
                seen.append((request, timeout))
                return io.BytesIO(b'{"status":"Ok","token":"new-private-token-value"}')

            sync = BrokerTokenSync(Path(folder), opener=opener)
            with patch("strategy_lab.broker_auth._credentials", return_value=("public-key", "private-secret")):
                result = sync.exchange("https://example.test/callback?request_code=ABC123")
            self.assertEqual(path.read_text(), "new-private-token-value")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(result["token_present"])
            self.assertNotIn("new-private-token-value", json.dumps(result))
            self.assertNotIn("private-secret", json.dumps(result))
            request, timeout = seen[0]
            self.assertEqual(timeout, 12)
            self.assertEqual(request.full_url, "https://authapi.flattrade.in/trade/apitoken")
            payload = json.loads(request.data)
            self.assertEqual(payload["api_secret"], hashlib.sha256(
                b"public-keyABC123private-secret").hexdigest())

    def test_rejection_leaves_previous_token_and_hides_broker_message(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "token.txt"
            path.write_text("old-token-unchanged")
            sync = BrokerTokenSync(Path(folder), opener=lambda *_args, **_kwargs:
                                   io.BytesIO(b'{"status":"Not_Ok","emsg":"broker-secret-detail"}'))
            with patch("strategy_lab.broker_auth._credentials", return_value=("public-key", "private-secret")):
                with self.assertRaises(BrokerAuthError) as caught:
                    sync.exchange("ABC123")
            self.assertEqual(path.read_text(), "old-token-unchanged")
            self.assertNotIn("broker-secret-detail", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
