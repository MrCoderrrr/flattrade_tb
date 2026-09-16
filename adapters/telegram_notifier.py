"""Non-blocking Telegram status delivery for the paper runtime."""
from __future__ import annotations
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, interval_seconds: float = 10.0):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "8850507396:AAFwFm2_WxPdSM52JcCpJUj8V1rz9x3G-kE")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "6307066850")
        self.interval_seconds = interval_seconds
        self.last_sent: datetime | None = None
        if not self.token or not self.chat_id:
            log.warning("Telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    def send(self, text: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self.token or not self.chat_id:
            return False
        if self.last_sent and (now - self.last_sent).total_seconds() < self.interval_seconds:
            return False
        payload = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                ok = json.loads(response.read().decode()).get("ok", False)
            if ok:
                self.last_sent = now
            return bool(ok)
        except Exception as exc:
            log.warning("Telegram update failed: %s", exc)
            return False
