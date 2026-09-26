"""Authenticated, loopback-only controls for the opt-in strategy runtime.

Importing this module does not load credentials, contact a broker, or start a bot.
Run ``python control_dashboard.py --root .`` and enter the printed *control* token
in the browser. It is independent of any broker access token.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import hmac
import json
import math
from pathlib import Path
import secrets
import socket
import os
import signal
import time
import threading
from urllib.parse import urlparse
from typing import Any
from zoneinfo import ZoneInfo


MAX_BODY_BYTES = 4096
IST = ZoneInfo("Asia/Kolkata")
DASHBOARD_PATH = Path(__file__).resolve().parent / "strategy_lab" / "dashboard.html"


class RequestError(Exception):
    """An intentionally public, credential-free request validation message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


class ControlHTTPServer(ThreadingHTTPServer):
    """The supplied controller owns all trading and persistent state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, controller: Any, host: str = "127.0.0.1", port: int = 8080,
                 token: str | None = None, dashboard_path: Path = DASHBOARD_PATH,
                 allowed_host: str | None = None, pin: str | None = None,
                 chain: Any = None):
        if host not in {"127.0.0.1", "localhost"} and not allowed_host:
            raise ValueError("Public binding requires an explicit allowed host.")
        if token is not None and (not isinstance(token, str) or len(token) < 32
                                  or not token.isascii() or any(c.isspace() for c in token)):
            raise ValueError("The control token must contain at least 32 ASCII characters without spaces.")
        self.controller = controller
        self.control_token = token or secrets.token_urlsafe(32)
        if pin is not None and (not isinstance(pin,str) or len(pin) != 4 or not pin.isascii() or not pin.isdigit()):
            raise ValueError("Dashboard PIN must contain exactly four digits")
        self.pin = pin
        self.sessions: dict[str, tuple[float,str]] = {}
        self.login_failures: dict[str, list[float]] = {}
        self.auth_lock = threading.RLock()
        self.chain = chain
        # Resolve no user-controlled hostname, including hosts-file entries.
        self.dashboard_html = Path(dashboard_path).read_bytes()
        bind_host = "0.0.0.0" if host == "0.0.0.0" else "127.0.0.1"
        super().__init__((bind_host, port), ControlHandler)
        actual_port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}
        if allowed_host:
            self.allowed_hosts.add(f"{allowed_host}:{actual_port}")
        if actual_port == 80:
            self.allowed_hosts.update({"127.0.0.1", "localhost"})
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        self.public_origin_file = None

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Request exceptions must never dump broker objects, tokens, or bodies.
        return


class ControlHandler(BaseHTTPRequestHandler):
    server: ControlHTTPServer
    server_version = "LocalControl"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid writing token-bearing URLs, request payloads, or headers to logs.
        return

    def _send(self, status: int, body: bytes, content_type: str,
              head_only: bool = False, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status: int, value: Any, head_only: bool = False,
              cookie: str | None = None) -> None:
        try:
            body = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            body = b'{"error":"Controller returned an invalid response."}'
        self._send(status, body, "application/json; charset=utf-8", head_only, cookie)

    def _validate_source(self) -> None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0] not in self.server.allowed_hosts:
            raise RequestError("Unrecognized local host.", 403)
        public = set()
        if self.server.public_origin_file:
            try:
                origin = Path(self.server.public_origin_file).read_text().strip()
                parsed = urlparse(origin)
                if parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".trycloudflare.com") and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username:
                    public.add(origin)
            except OSError:
                pass
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins[0] not in self.server.allowed_origins | public):
            raise RequestError("Cross-origin requests are not allowed.", 403)
        # A valid Origin must identify the actual requested authority as well.
        if origins and origins[0] not in public and origins[0] != f"http://{hosts[0]}":
            raise RequestError("The request origin does not match its host.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError("Cross-site requests are not allowed.", 403)

    def _authenticate(self) -> None:
        values = self.headers.get_all("Authorization", [])
        expected = f"Bearer {self.server.control_token}".encode("ascii")
        supplied = values[0].encode("utf-8") if len(values) == 1 else b""
        if len(supplied) <= 512 and hmac.compare_digest(supplied, expected):
            return
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", "")[:512])
            session = cookie.get("desk_session")
            supplied = session.value if session else ""
        except Exception:
            supplied = ""
        now = time.monotonic()
        with self.server.auth_lock:
            valid = self.server.sessions.get(supplied)
            if valid and valid[0] > now and valid[1] == self.client_address[0]:
                return
        raise RequestError("Enter the dashboard PIN to unlock.", 401)

    def _login(self, data: dict[str, Any]) -> None:
        if self.server.pin is None:
            raise RequestError("PIN login is not configured.", 503)
        if set(data) != {"pin"} or not isinstance(data['pin'],str) or len(data['pin']) != 4:
            raise RequestError("Enter the four-digit dashboard PIN.")
        address = self.client_address[0]
        now = time.monotonic()
        with self.server.auth_lock:
            failures = [when for when in self.server.login_failures.get(address,[]) if now-when < 900]
            if len(failures) >= 5:
                raise RequestError("Too many attempts. Try again in 15 minutes.", 429)
            if not hmac.compare_digest(data['pin'],self.server.pin):
                failures.append(now)
                self.server.login_failures[address] = failures
                raise RequestError("Incorrect dashboard PIN.", 401)
            self.server.login_failures.pop(address,None)
            token = secrets.token_urlsafe(32)
            self.server.sessions[token] = (now+8*3600,address)
            for old,(expiry,_) in list(self.server.sessions.items()):
                if expiry <= now:
                    self.server.sessions.pop(old,None)
        self._json(200,{"authenticated":True},cookie=f"desk_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800")

    def _body(self) -> dict[str, Any]:
        if self.headers.get_all("Transfer-Encoding"):
            raise RequestError("Transfer-encoded requests are not supported.")
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or len(lengths[0]) > 10
                or not lengths[0].isascii() or not lengths[0].isdigit()):
            raise RequestError("A valid Content-Length is required.", 411)
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            raise RequestError("Request body is too large.", 413)
        if length == 0:
            raise RequestError("A JSON object is required.")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise RequestError("Use Content-Type application/json.", 415)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Truncated request")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError, socket.timeout):
            raise RequestError("Request body must be a valid JSON object.") from None
        if not isinstance(data, dict):
            raise RequestError("Request body must be a JSON object.")
        return data

    @staticmethod
    def _market(data: dict[str, Any]) -> str:
        market = data.get("market")
        if not isinstance(market, str) or market not in {"NIFTY", "MCX"}:
            raise RequestError("Choose market NIFTY or MCX.")
        return market

    def _start(self, data: dict[str, Any]) -> Any:
        fields = {"market", "mode", "multiplier", "capital", "confirmation", "strategy_id", "pin"}
        if set(data) - fields:
            raise RequestError("Unexpected start fields.")
        market = self._market(data)
        mode = data.get("mode")
        if not isinstance(mode, str) or mode not in {"paper", "live"}:
            raise RequestError("Choose paper or live explicitly for each session.")
        multiplier = data.get("multiplier")
        if type(multiplier) is not int or multiplier < 1 or multiplier > 100:
            raise RequestError("Multiplier must be a whole number from 1 to 100.")
        capital = data.get("capital")
        try:
            valid_capital = (type(capital) in {int, float} and math.isfinite(capital)
                             and capital >= 200000 * multiplier)
        except OverflowError:
            valid_capital = False
        if not valid_capital:
            raise RequestError("Available capital must be at least ₹200,000 per multiplier.")
        confirmation = data.get("confirmation", "")
        if not isinstance(confirmation, str) or len(confirmation) > 80:
            raise RequestError("Invalid live confirmation.")
        if mode == "live":
            pin = data.get('pin')
            if (self.server.pin is None or not isinstance(pin,str)
                    or not hmac.compare_digest(pin,self.server.pin)):
                raise RequestError('Enter the four-digit dashboard PIN for a live request.',403)
            confirmation = f"LIVE {market} {datetime.now(IST).date().isoformat()}"
        kwargs = {}
        if 'strategy_id' in data:
            from strategy_lab.catalog import resolve
            kwargs['strategy_id'] = resolve(market, data['strategy_id']).id
        return self.server.controller.start(market, mode, multiplier, capital, confirmation=confirmation, **kwargs)

    def _settings(self, data: dict[str, Any]) -> Any:
        if set(data) == {'capital'}:
            capital = data['capital']
            if type(capital) not in (int,float) or not math.isfinite(capital) or capital < 200000:
                raise RequestError('Account capital must be at least ₹200,000.')
            return self.server.controller.configure(capital=capital)
        if set(data) in ({'live_permission'}, {'live_permission','pin'}):
            value = data['live_permission']
            if type(value) is not bool:
                raise RequestError('Live permission must be true or false.')
            if value:
                pin = data.get('pin')
                if (self.server.pin is None or not isinstance(pin,str)
                        or not hmac.compare_digest(pin,self.server.pin)):
                    raise RequestError('Enter the four-digit dashboard PIN to enable live permission.',403)
            return self.server.controller.configure(live_permission=value)
        raise RequestError('Settings accepts account capital or live permission.')

    def _get(self, head_only: bool = False) -> None:
        try:
            self._validate_source()
            if self.path == "/":
                self._send(200, self.server.dashboard_html, "text/html; charset=utf-8", head_only)
                return
            self._authenticate()
            if self.path == "/api/status":
                status = self.server.controller.status()
                if not isinstance(status, dict):
                    raise RuntimeError("Invalid status")
                status = dict(status)
                status["today"] = datetime.now(IST).date().isoformat()
                self._json(200, status, head_only)
            elif self.path == "/api/chain":
                self._json(200, self.server.chain.snapshot() if self.server.chain else
                           {"ready":False,"reason":"Read-only option-chain stream is unavailable","rows":[]}, head_only)
            else:
                raise RequestError("Not found.", 404)
        except RequestError as exc:
            self._json(exc.status, {"error": str(exc)}, head_only)
        except Exception:
            self._json(500, {"error": "Unable to read controller status."}, head_only)

    def do_GET(self) -> None:
        self._get()

    def do_HEAD(self) -> None:
        self._get(head_only=True)

    def do_POST(self) -> None:
        try:
            self._validate_source()
            if self.path == "/api/login":
                self._login(self._body())
                return
            self._authenticate()
            if self.path == "/api/logout":
                cookie = SimpleCookie()
                cookie.load(self.headers.get("Cookie", "")[:512])
                supplied = cookie.get("desk_session")
                if supplied:
                    with self.server.auth_lock:
                        self.server.sessions.pop(supplied.value,None)
                self._json(200,{"authenticated":False},cookie="desk_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if self.path not in {"/api/start", "/api/stop", "/api/kill", "/api/pause", "/api/settings"}:
                raise RequestError("Not found.", 404)
            data = self._body()
            if self.path == "/api/start":
                result = self._start(data)
            elif self.path == "/api/settings":
                result = self._settings(data)
            elif self.path == "/api/pause":
                if set(data) != {"market", "paused"} or type(data["paused"]) is not bool:
                    raise RequestError("Pause requires market and a boolean paused field")
                result = self.server.controller.pause(self._market(data), data["paused"])
            elif self.path == "/api/stop":
                if set(data) != {"market"}:
                    raise RequestError("Stop requires only the market field.")
                result = self.server.controller.stop(self._market(data))
            else:
                if data:
                    raise RequestError("Emergency stop takes an empty JSON object.")
                result = self.server.controller.kill()
            self._json(200, result)
        except RequestError as exc:
            self._json(exc.status, {"error": str(exc)})
        except ValueError as exc:
            # Controller ValueErrors are its explicit public validation contract.
            self._json(409, {"error": str(exc)})
        except RuntimeError:
            # Faults can contain broker payloads or credentials and are not sent.
            self._json(409, {"error": "Controller rejected the request. Check session status and validation requirements."})
        except Exception:
            self._json(500, {"error": "Controller request failed. Check local runtime health."})

    def do_OPTIONS(self) -> None:
        try:
            self._validate_source()
            raise RequestError("Method not allowed.", 405)
        except RequestError as exc:
            self._json(exc.status, {"error": str(exc)})


def create_server(controller: Any, host: str = "127.0.0.1", port: int = 8080,
                  token: str | None = None, dashboard_path: Path = DASHBOARD_PATH,
                  allowed_host: str | None = None, pin: str | None = None,
                  chain: Any = None) -> ControlHTTPServer:
    """Create an unstarted local HTTP server; useful with an offline fake controller."""
    return ControlHTTPServer(controller, host, port, token, dashboard_path, allowed_host, pin, chain)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--pin-file", type=Path,
                        default=Path(os.environ['DASHBOARD_PIN_FILE']) if os.environ.get('DASHBOARD_PIN_FILE') else None)
    parser.add_argument("--public-origin-file", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allowed-host")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    # Runtime owns process locking. Importing the HTTP module alone has no trading
    # side effects, and every actual session still needs an authenticated start.
    from strategy_lab.runtime import Controller

    controller = Controller(args.root.resolve())
    chain = None
    server = None
    try:
        token = None
        if args.token_file:
            args.token_file.parent.mkdir(parents=True, exist_ok=True)
            if not args.token_file.exists():
                fd = os.open(args.token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(secrets.token_urlsafe(32))
            token = args.token_file.read_text().strip()
        pin = args.pin_file.read_text().strip() if args.pin_file else None
        from strategy_lab.option_chain import OptionChainView
        chain = OptionChainView(args.root.resolve())
        chain.start()
        server = create_server(controller, host=args.host, port=args.port, token=token,
                               allowed_host=args.allowed_host, pin=pin, chain=chain)
        server.public_origin_file = args.public_origin_file
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        controller.start_worker()
        print(f"Local dashboard: http://127.0.0.1:{server.server_address[1]}", flush=True)
        print(f"Control token file: {args.token_file}" if args.token_file else f"Dashboard control token (not a broker token): {server.control_token}", flush=True)
        print("Each session requires an explicit paper/live selection. No session has been started.", flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("Stopping dashboard and requesting runtime shutdown.", flush=True)
    finally:
        if server is not None:
            server.server_close()
        if chain is not None:
            chain.stop()
        controller.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
