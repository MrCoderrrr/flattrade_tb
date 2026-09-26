"""Read-only, in-memory NIFTY option-chain view for the dashboard.

The browser polls once per second. Broker quotes arrive only when the exchange
publishes changes; an unchanged row retains its real age and is never presented
as a new tick. No order method or broker credential is exposed here.
"""
from __future__ import annotations

import csv
import io
import os
import threading
import zipfile
from datetime import datetime
import time
from pathlib import Path
from urllib.request import urlopen

from .models import IST
from .nifty_stream import NiftyStream


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_master(raw: bytes, today):
    """Nearest live expiry's NIFTY CE/PE tokens, keyed by strike and type."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = next((n for n in archive.namelist() if n.endswith('NFO_symbols.txt')), None)
        if name is None:
            raise ValueError('NFO contract master missing')
        with archive.open(name) as handle:
            rows = list(csv.DictReader(io.TextIOWrapper(handle, encoding='utf-8-sig')))
    eligible = []
    for row in rows:
        if row.get('Symbol') != 'NIFTY' or row.get('OptionType') not in {'CE','PE'}:
            continue
        try:
            expiry = datetime.strptime(row['Expiry'],'%d-%b-%Y').date()
            strike = float(row['StrikePrice'])
            token = str(row['Token'])
            if expiry > today and strike > 0 and token.isdigit():
                eligible.append((expiry,strike,row['OptionType'],token,row['TradingSymbol']))
        except (KeyError,ValueError):
            continue
    if not eligible:
        raise ValueError('No future NIFTY options in contract master')
    expiry = min(row[0] for row in eligible)
    return expiry,{(strike,kind):(token,symbol) for day,strike,kind,token,symbol in eligible if day==expiry}


class OptionChainView:
    MASTER_URL = 'https://api.shoonya.com/NFO_symbols.txt.zip'

    def __init__(self, root: Path, clock=None, stream=None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(IST))
        token_path = Path(os.environ.get('FLATTRADE_TOKEN_FILE',str(self.root/'token.txt')))
        self.stream = stream or NiftyStream(token_path,os.environ.get('FLATTRADE_USER_ID',''))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.catalog_day = None
        self.expiry = None
        self.contracts = {}
        self.reason = 'Waiting for current contract master and broker stream'
        self.selected = set()
        self.last_master_attempt = 0.

    def _load_master(self, now):
        if self.catalog_day == now.date():
            return
        if time.monotonic()-self.last_master_attempt < 300:
            return
        self.last_master_attempt = time.monotonic()
        with urlopen(self.MASTER_URL,timeout=12) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError('Contract master exceeded download limit')
        expiry,contracts = parse_master(raw,now.date())
        with self.lock:
            self.catalog_day = now.date()
            self.expiry = expiry
            self.contracts = contracts
            self.reason = ''

    def refresh(self):
        now = self.clock().astimezone(IST)
        if now.weekday() >= 5 or not '09:15' <= now.strftime('%H:%M') < '15:40':
            with self.lock:
                self.reason = 'NIFTY option market is outside its live session'
            if self.stream.thread and self.stream.thread.is_alive():
                self.stream.stop()
            return
        self.stream.start()
        try:
            self._load_master(now)
        except Exception:
            with self.lock:
                self.reason = 'Current NFO contract master is unavailable'
            return
        snap = self.stream.latest(now)
        if not snap['ready']:
            with self.lock:
                self.reason = snap['reason'] or 'Waiting for fresh NIFTY index tick'
            return
        spot = snap['ticks'][-1].price
        with self.lock:
            strikes = sorted({strike for strike,_ in self.contracts},key=lambda x:abs(x-spot))[:11]
            selected = {f"NFO|{self.contracts[(strike,kind)][0]}" for strike in strikes
                        for kind in ('CE','PE') if (strike,kind) in self.contracts}
            if selected != self.selected:
                self.stream.set_contracts(selected)
                self.selected = selected
            self.reason = ''

    def snapshot(self):
        now = self.clock().astimezone(IST)
        snap = self.stream.latest(now)
        with self.lock:
            expiry = self.expiry.isoformat() if self.expiry else None
            contracts = dict(self.contracts)
            reason = self.reason
        spot = snap['ticks'][-1].price if snap['ticks'] else None
        strikes = sorted({strike for strike,_ in contracts},key=lambda x:abs(x-(spot or 0)))[:11] if spot else []
        rows = []
        for strike in sorted(strikes):
            row = {'strike':strike}
            for kind in ('CE','PE'):
                contract = contracts.get((strike,kind))
                book = snap['books'].get(f'NFO|{contract[0]}') if contract else None
                fields = book['fields'] if book else {}
                stamp = book['book_received_at'] or book['price_received_at'] if book else None
                age = (now-stamp).total_seconds() if stamp else None
                row[kind.lower()] = {'symbol':contract[1] if contract else None,
                    'bid':_number(fields.get('bp1')),'ask':_number(fields.get('sp1')),
                    'last':_number(fields.get('lp')),'bid_size':_number(fields.get('bq1')),
                    'ask_size':_number(fields.get('sq1')),'oi':_number(fields.get('oi')),
                    'age_seconds':round(age,1) if age is not None else None,
                    'stale':age is None or age>10}
            rows.append(row)
        return {'ready':bool(snap['ready'] and rows),'reason':reason or snap['reason'],
                'spot':spot,'expiry':expiry,'as_of':now.isoformat(),
                'source':'Flattrade WebSocket; bid/ask ages are receive-time ages','rows':rows}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                self.refresh()
                self.stop_event.wait(1)
        self.thread = threading.Thread(target=run,name='read-only-option-chain',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        self.stream.stop()
