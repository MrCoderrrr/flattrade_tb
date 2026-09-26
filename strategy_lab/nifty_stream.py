"""Read-only Flattrade WebSocket collector for one-second NIFTY research.

`tk` messages seed complete instrument snapshots; `tf` messages update only
changed fields. Receive times are retained separately from optional broker
timestamps. This module has no order API and does not turn a missing tick into
a synthetic price.
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from .models import IST
from .nifty_flow import IndexTick


class NiftyStream:
    URL = "wss://piconnect.flattrade.in/PiConnectWSAPI/"
    INDEX = "NSE|26000"

    def __init__(self, token_file: Path, user_id: str, clock=None):
        self.token_file = Path(token_file)
        self.user_id = user_id.strip()
        self.clock = clock or (lambda: datetime.now(IST))
        self.lock = threading.RLock()
        self.ticks: deque[IndexTick] = deque(maxlen=1200)
        self.books: dict[str, dict] = {}
        self.wanted: set[str] = {self.INDEX}
        self.authenticated = False
        self.connected = False
        self.websocket = None
        self.thread = None
        self.stop_event = threading.Event()
        self.error = "Not connected"

    def set_contracts(self, tokens):
        keys = set()
        for token in tokens:
            if not isinstance(token, str) or not token.startswith("NFO|") or not token[4:].isdigit():
                raise ValueError("NIFTY option subscriptions must be NFO|numeric-token")
            keys.add(token)
        with self.lock:
            added = keys - self.wanted
            removed = (self.wanted - keys) - {self.INDEX}
            self.wanted = keys | {self.INDEX}
            if self.authenticated and self.websocket:
                if removed:
                    self.websocket.send(json.dumps({"t":"u","k":"#".join(sorted(removed))}))
                if added:
                    self.websocket.send(json.dumps({"t":"t","k":"#".join(sorted(added))}))
            for key in removed:
                self.books.pop(key,None)

    def on_open(self, ws):
        token = self.token_file.read_text().strip() if self.token_file.is_file() else ""
        if not self.user_id or not token:
            self.error = "Broker user ID or login token missing"
            ws.close()
            return
        with self.lock:
            self.websocket = ws
            self.connected = True
            self.authenticated = False
            self.books.clear()
            self.ticks.clear()
        ws.send(json.dumps({"t":"a","uid":self.user_id,"actid":self.user_id,
                            "accesstoken":token,"source":"API"}))

    def on_message(self, ws, payload):
        try:
            row = json.loads(payload)
        except (TypeError, ValueError):
            self.error = "Malformed broker WebSocket frame"
            return
        if not isinstance(row, dict):
            return
        kind = row.get('t')
        with self.lock:
            if kind in ('ck','ak'):
                if row.get('s') != 'OK':
                    self.authenticated = False
                    self.error = "Broker WebSocket authentication failed"
                    return
                self.authenticated = True
                self.error = ""
                ws.send(json.dumps({"t":"t","k":"#".join(sorted(self.wanted))}))
                return
            if kind not in ('tk','tf') or not self.authenticated:
                return
            exchange,token = row.get('e'),row.get('tk')
            if not isinstance(exchange,str) or not isinstance(token,(str,int)):
                return
            key=f"{exchange}|{token}"
            if key not in self.wanted:
                return
            if kind=='tk':
                self.books[key] = {'fields':{},'book_received_at':None,'price_received_at':None,
                                   'broker_feed_time':None,'source':'websocket_receive_time'}
            cached = self.books.get(key)
            if cached is None:
                # Deltas before a complete snapshot cannot construct a book.
                return
            timestamp=self.clock().astimezone(IST)
            if any(field in row for field in ('bp1','sp1','bq1','sq1')):
                cached['book_received_at']=timestamp
            if 'lp' in row:
                cached['price_received_at']=timestamp
            if 'ft' in row:
                cached['broker_feed_time']=row['ft']
            cached['fields'].update({k:v for k,v in row.items() if k not in ('t','e','tk')})
            if key==self.INDEX and 'lp' in row:
                try:
                    price=float(row['lp'])
                    if price>0 and price<1_000_000 and (not self.ticks or timestamp>self.ticks[-1].timestamp):
                        self.ticks.append(IndexTick(timestamp,price))
                except (TypeError,ValueError):
                    pass

    def on_close(self, _ws, *_args):
        with self.lock:
            self.connected=False
            self.authenticated=False
            self.books.clear()
            self.ticks.clear()
            self.websocket=None
            self.error="Broker WebSocket disconnected"

    def latest(self, now=None):
        now=(now or self.clock()).astimezone(IST)
        with self.lock:
            ticks=list(self.ticks)
            books={key:{**item,'fields':dict(item['fields'])} for key,item in self.books.items()}
            ready=self.authenticated and bool(ticks) and 0 <= (now-ticks[-1].timestamp).total_seconds() <= 3
            return {'connected':self.connected,'authenticated':self.authenticated,
                    'ready':ready,'reason':self.error,'ticks':ticks,'books':books}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def work():
            try:
                import websocket  # Optional dependency, loaded only for the stream.
            except ImportError:
                self.error='websocket-client is required for one-second data'
                return
            while not self.stop_event.is_set():
                ws=websocket.WebSocketApp(self.URL,on_open=self.on_open,on_message=self.on_message,
                                          on_close=self.on_close,on_error=lambda _ws,_error:setattr(self,'error','Broker WebSocket error'))
                try:
                    ws.run_forever(ping_interval=20,ping_timeout=8)
                except Exception:
                    self.error='Broker WebSocket connection failed'
                self.on_close(ws)
                self.stop_event.wait(5)
        self.thread=threading.Thread(target=work,name='nifty-readonly-stream',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            ws=self.websocket
        if ws:
            ws.close()
        if self.thread:
            self.thread.join(timeout=10)
