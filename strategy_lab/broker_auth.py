"""Flattrade login-code exchange for the authenticated dashboard.

This module only renews the market-data token. It never submits orders, changes
strategy state, or returns the access token to the browser.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import Request, urlopen

from .models import IST


AUTH_URL = "https://auth.flattrade.in/?app_key="
TOKEN_URL = "https://authapi.flattrade.in/trade/apitoken"
CODE_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,512}\Z")


class BrokerAuthError(ValueError):
    """Safe, public error text without broker response details."""


def extract_request_code(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BrokerAuthError("Paste a Flattrade request code or redirect URL.")
    raw = value.strip()
    if "://" in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrokerAuthError("Invalid Flattrade redirect URL.")
        params = parse_qs(parsed.query, keep_blank_values=True)
        codes = params.get("request_code") or params.get("code") or []
        if len(codes) != 1:
            raise BrokerAuthError("Redirect URL must contain one request_code.")
        raw = codes[0].strip()
    elif raw.startswith(("request_code=", "code=")):
        params = parse_qs(raw, keep_blank_values=True)
        codes = params.get("request_code") or params.get("code") or []
        if len(codes) != 1:
            raise BrokerAuthError("Enter one request_code.")
        raw = codes[0].strip()
    if not CODE_PATTERN.fullmatch(raw):
        raise BrokerAuthError("Invalid Flattrade request code.")
    return raw


def _credentials():
    try:
        import creds  # Existing private server module; never fall back to keys in source.
        key = str(creds.API_KEY).strip()
        secret = str(creds.API_SECRET).strip()
        if key and secret:
            return key, secret
    except (ImportError, AttributeError):
        pass
    return None


class BrokerTokenSync:
    def __init__(self, root: Path, token_file: Path | None = None, opener=None, clock=None):
        self.token_file = Path(token_file or os.environ.get("FLATTRADE_TOKEN_FILE")
                               or Path(root) / "token.txt")
        self.opener = opener or urlopen
        self.clock = clock or (lambda: datetime.now(IST))

    def status(self) -> dict:
        credentials = _credentials()
        try:
            stat = self.token_file.stat()
            present = stat.st_size > 10
            updated = datetime.fromtimestamp(stat.st_mtime, IST) if present else None
        except OSError:
            present, updated = False, None
        return {
            "configured": credentials is not None,
            "auth_url": AUTH_URL + quote(credentials[0], safe="") if credentials else None,
            "token_present": present,
            "saved_today": bool(updated and updated.date() == self.clock().astimezone(IST).date()),
            "last_updated": updated.isoformat(timespec="minutes") if updated else None,
        }

    def exchange(self, url_or_code: str) -> dict:
        credentials = _credentials()
        if credentials is None:
            raise BrokerAuthError("Flattrade API credentials are not configured on this server.")
        code = extract_request_code(url_or_code)
        key, secret = credentials
        digest = hashlib.sha256((key + code + secret).encode("utf-8")).hexdigest()
        body = json.dumps({"api_key": key, "request_code": code,
                           "api_secret": digest}, separators=(",", ":")).encode("utf-8")
        request = Request(TOKEN_URL, data=body, headers={"Content-Type": "application/json"},
                          method="POST")
        try:
            with self.opener(request, timeout=12) as response:
                payload = response.read(8193)
                if len(payload) > 8192:
                    raise BrokerAuthError("Flattrade returned an oversized response.")
                result = json.loads(payload)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise BrokerAuthError("Flattrade token exchange failed. Generate a new code and retry.") from None
        except (ValueError, UnicodeError):
            raise BrokerAuthError("Flattrade returned an invalid token response.") from None
        token = result.get("token") if isinstance(result, dict) else None
        if (not isinstance(token, str) or not 10 < len(token) <= 4096
                or any(char.isspace() for char in token)):
            raise BrokerAuthError("Flattrade rejected the request code. Generate a new code and retry.")
        # Write once, atomically, with owner-only permissions. Do not print or return it.
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".flattrade-token-",
                                             dir=self.token_file.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.token_file)
        except OSError:
            raise BrokerAuthError("Could not save the Flattrade token on the server.") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return self.status()
