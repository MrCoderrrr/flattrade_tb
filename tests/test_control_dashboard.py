"""Offline HTTP security and control-dispatch checks. No broker is imported."""

from __future__ import annotations

from datetime import datetime
import http.client
import json
from pathlib import Path
import threading
import unittest
from zoneinfo import ZoneInfo

from control_dashboard import create_server


class FakeController:
    def __init__(self):
        self.calls = []
        self.fail = False

    def status(self):
        return {"today": "2000-01-01", "live_enabled": False,
                "account": {"daily_pnl": 0, "capital": 200000, "halted": False},
                "sessions": {}, "events": []}

    def start(self, market, mode, multiplier, capital, confirmation=""):
        if self.fail:
            raise RuntimeError("secret-broker-token-must-not-leak")
        self.calls.append(("start", market, mode, multiplier, capital, confirmation))
        return {"state": "waiting", "mode": mode}

    def stop(self, market):
        self.calls.append(("stop", market))
        return {"state": "stopped"}

    def kill(self):
        self.calls.append(("kill",))
        return {"halted": True}


class DashboardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = FakeController()
        cls.token = "test-local-control-token-" + "x" * 32
        cls.server = create_server(cls.controller, port=0, token=cls.token)
        cls.port = cls.server.server_address[1]
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)

    def setUp(self):
        self.controller.calls.clear()
        self.controller.fail = False

    def request(self, method, path, payload=None, authorized=True, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = {}
        if authorized:
            request_headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None or raw is not None:
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        body = raw if raw is not None else json.dumps(payload) if payload is not None else None
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        status, result_headers, body = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, result_headers, body

    def test_homepage_is_public_but_contains_no_session_token(self):
        status, headers, body = self.request("GET", "/", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Your trading workspace", body)
        self.assertNotIn(self.token.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_status_and_all_mutations_require_authentication(self):
        for method, path, payload in [("GET", "/api/status", None),
                                      ("POST", "/api/start", self.start_payload()),
                                      ("POST", "/api/stop", {"market": "NIFTY"}),
                                      ("POST", "/api/kill", {})]:
            with self.subTest(path=path):
                status, _, _ = self.request(method, path, payload, authorized=False)
                self.assertEqual(status, 401)
        self.assertEqual(self.controller.calls, [])

    def test_bad_token_and_query_token_cannot_authorize(self):
        self.assertEqual(self.request("GET", "/api/status", headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.request("GET", "/api/status?token=" + self.token, authorized=False)[0], 401)

    def test_dns_rebinding_and_cross_origin_requests_are_rejected(self):
        for headers in [{"Host": f"evil.example:{self.port}"},
                        {"Host": f"127.0.0.1:{self.port + 1}"},
                        {"Origin": "https://evil.example"},
                        {"Origin": "null"},
                        {"Origin": f"http://localhost:{self.port}"},
                        {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/start", self.start_payload(), headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.controller.calls, [])

    def test_duplicate_host_or_auth_headers_are_rejected(self):
        for name, value in [("Host", f"127.0.0.1:{self.port}"),
                            ("Authorization", "Bearer " + self.token)]:
            with self.subTest(header=name):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
                connection.putrequest("GET", "/api/status")
                connection.putheader("Authorization", "Bearer " + self.token)
                connection.putheader(name, value)
                connection.endheaders()
                response = connection.getresponse()
                self.assertEqual(response.status, 403 if name == "Host" else 401)
                response.read()
                connection.close()

    def test_valid_status_uses_current_server_ist_date(self):
        status, _, body = self.request("GET", "/api/status", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["today"], datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat())
        self.assertIs(result["live_enabled"], False)

    @staticmethod
    def start_payload(**overrides):
        return {"market": "NIFTY", "mode": "paper", "multiplier": 1, "capital": 200000, **overrides}

    def test_explicit_paper_start_dispatches_once(self):
        status, _, _ = self.request("POST", "/api/start", self.start_payload())
        self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls, [("start", "NIFTY", "paper", 1, 200000, "")])

    def test_invalid_start_never_reaches_runtime(self):
        payloads = [self.start_payload(mode=None), self.start_payload(mode="LIVE"),
                    self.start_payload(mode=[]), self.start_payload(market={}),
                    self.start_payload(market="BANKNIFTY"), self.start_payload(multiplier=True),
                    self.start_payload(multiplier=1.5), self.start_payload(multiplier=0),
                    self.start_payload(multiplier=1001), self.start_payload(capital=True),
                    self.start_payload(capital="200000"), self.start_payload(capital=199999),
                    self.start_payload(multiplier=2), self.start_payload(extra="unexpected"),
                    self.start_payload(confirmation=[])]
        missing_mode = self.start_payload()
        del missing_mode["mode"]
        payloads.append(missing_mode)
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.request("POST", "/api/start", payload)[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_live_requires_exact_market_and_current_date(self):
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        for confirmation in ["", "yes", "LIVE NIFTY 2000-01-01", f"LIVE MCX {today}", f"LIVE NIFTY {today} "]:
            with self.subTest(confirmation=confirmation):
                self.assertEqual(self.request("POST", "/api/start", self.start_payload(mode="live", confirmation=confirmation))[0], 400)
        self.assertEqual(self.controller.calls, [])
        status, _, _ = self.request("POST", "/api/start", self.start_payload(mode="live", confirmation=f"LIVE NIFTY {today}"))
        self.assertEqual(status, 200)
        # Validation and the live-eligibility gate remain the controller's job.
        self.assertEqual(self.controller.calls[-1], ("start", "NIFTY", "live", 1, 200000, f"LIVE NIFTY {today}"))

    def test_stop_and_shared_emergency_stop_dispatch(self):
        self.assertEqual(self.request("POST", "/api/stop", {"market": "MCX"})[0], 200)
        self.assertEqual(self.request("POST", "/api/kill", {})[0], 200)
        self.assertEqual(self.controller.calls, [("stop", "MCX"), ("kill",)])

    def test_invalid_stop_and_kill_do_not_dispatch(self):
        for path, body in [("/api/stop", {}), ("/api/stop", {"market":"NIFTY", "extra":1}),
                           ("/api/kill", {"market":"NIFTY"})]:
            self.assertEqual(self.request("POST", path, body)[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_body_limits_content_type_and_malformed_json(self):
        self.assertEqual(self.request("POST", "/api/start", raw="x" * 4097)[0], 413)
        self.assertEqual(self.request("POST", "/api/start", self.start_payload(), headers={"Content-Type":"text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/start", self.start_payload(), headers={"Transfer-Encoding":"chunked"})[0], 400)
        for raw in ["", "[]", "{", '{"mode":"paper","mode":"live"}', '{"capital":NaN}', '{"capital":Infinity}', b"\xff"]:
            with self.subTest(raw=raw):
                self.assertEqual(self.request("POST", "/api/start", raw=raw)[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_controller_exception_does_not_leak_credentials(self):
        self.controller.fail = True
        status, _, body = self.request("POST", "/api/start", self.start_payload())
        self.assertEqual(status, 409)
        self.assertNotIn(b"secret-broker", body)
        self.assertIn(b"Controller rejected", body)

    def test_non_get_controls_are_not_available_as_get(self):
        self.assertEqual(self.request("GET", "/api/kill")[0], 404)
        self.assertEqual(self.request("OPTIONS", "/api/start")[0], 405)
        self.assertEqual(self.controller.calls, [])

    def test_public_bind_is_refused_and_weak_test_tokens_are_rejected(self):
        for host in ["0.0.0.0", "::", "192.168.1.2", "evil.example"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_server(self.controller, host=host, port=0)
        with self.assertRaises(ValueError):
            create_server(self.controller, port=0, token="short")

    def test_import_module_does_not_eagerly_import_runtime(self):
        # AST inspection avoids coupling to test ordering or another test suite
        # that has legitimately imported the runtime earlier in this process.
        import ast
        source = Path(__file__).resolve().parents[1] / "control_dashboard.py"
        tree = ast.parse(source.read_text())
        top_level_imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(node.module == "strategy_lab.runtime" for node in top_level_imports))


if __name__ == "__main__":
    unittest.main()
