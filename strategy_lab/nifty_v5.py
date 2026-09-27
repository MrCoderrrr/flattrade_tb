"""NIFTY v5 paper research: bounded ATM short straddle with distant wings.

The flow score is an observation, not a forecast or a claim of profitability.
Every new short is admitted only with a fresh executable book and a matching
long wing. Premium stops are triggers; gaps can exceed the intended loss.
"""
from __future__ import annotations

from datetime import timedelta
from math import isfinite
import threading
from datetime import datetime

from .models import Bar, Leg, Plan, IST
from .nifty_flow import evaluate
from .strategies import _option_type, _valid_quote


class FlowObserver:
    """Independent read-only sampler; controller actions may lag broker I/O."""

    def __init__(self, feed, stream, clock=None):
        self.feed = feed
        self.stream = stream
        self.clock = clock or (lambda: datetime.now(IST))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.history = []
        self.latest = {'eligible':False,'reason':'Waiting for one-second NIFTY flow'}

    def sample(self):
        now = self.clock().astimezone(IST)
        if now.weekday() >= 5 or not '09:20' <= now.strftime('%H:%M') < '15:33':
            observation = {'eligible':False,'reason':'Outside NIFTY v5 entry window'}
        else:
            try:
                bars = self.feed.bars('NIFTY',now,1)
                ticks = self.stream.latest(now)['ticks']
                observation = flow(bars[-450:],ticks,now)
            except Exception:
                observation = {'eligible':False,'reason':'Read-only flow feed unavailable'}
        with self.lock:
            if observation.get('eligible'):
                if not self.history or self.history[-1]['timestamp'] != observation['timestamp']:
                    self.history = (self.history+[observation])[-90:]
            self.latest = observation
        return observation

    def snapshot(self, now):
        with self.lock:
            observation = dict(self.latest)
            history = list(self.history)
        if observation.get('eligible'):
            age = (now-datetime.fromisoformat(observation['timestamp'])).total_seconds()
            if not 0 <= age <= 3:
                observation = {'eligible':False,'reason':'NIFTY flow sample is stale'}
        return observation, history

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def work():
            while not self.stop_event.is_set():
                self.sample()
                self.stop_event.wait(max(.05,1-self.clock().microsecond/1_000_000))
        self.thread = threading.Thread(target=work,name='nifty-v5-flow-observer',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            self.thread = None


def five_minute_bars(bars: list[Bar], now) -> list[Bar]:
    """Aggregate only complete, contiguous current-session one-minute bars."""
    today = [b for b in bars if b.interval_minutes == 1 and
             b.timestamp.astimezone(IST).date() == now.astimezone(IST).date() and
             b.timestamp + timedelta(minutes=1) <= now]
    result = []
    for offset in range(0, len(today)-4, 5):
        group = today[offset:offset+5]
        if (group[0].timestamp.astimezone(IST).strftime('%H:%M') < '09:15' or
                any(b.timestamp-a.timestamp != timedelta(minutes=1) for a,b in zip(group,group[1:])) or
                group[0].timestamp.astimezone(IST).minute % 5 != 0):
            continue
        result.append(Bar(group[0].timestamp, group[0].open,
                          max(b.high for b in group), min(b.low for b in group),
                          group[-1].close, sum(b.volume for b in group), 5))
    return result


def flow(bars, ticks, now):
    return evaluate(bars, five_minute_bars(bars, now), ticks, now)


def stop_parameters(observation: dict, solo: bool) -> tuple[float, float]:
    """Premium stop/trail percentages, bounded even for noisy observations."""
    quality = max(0., min(1., float(observation.get('quality', .5))))
    volatility = max(.5, min(2., float(observation.get('volatility_ratio', 1.))))
    stop = min(.30, max(.12, .15 + .10*(1-quality) + .05*max(0.,volatility-1)))
    trail = min(.18, max(.05, .06 + .08*(1-quality) + .03*max(0.,volatility-1)))
    if solo:
        trail = max(.05, trail-.025)
    return round(stop,4), round(trail,4)


def build_plan(bars, quotes, now, multiplier, capital, observation):
    if (type(multiplier) is not int or multiplier < 1 or capital < 200000*multiplier or
            not observation.get('eligible')):
        return None
    spot = observation['spot']
    valid = [q for q in quotes if _valid_quote(q, 'NIFTY', now)]
    if len({(q.contract.exchange,q.contract.token) for q in valid}) != len(valid):
        return None
    for expiry in sorted({q.contract.expiry for q in valid}):
        group = [q for q in valid if q.contract.expiry == expiry]
        for lot in sorted({q.contract.lot_size for q in group}):
            chain = [q for q in group if q.contract.lot_size == lot]
            strikes = sorted({q.contract.strike for q in chain},key=lambda x:abs(x-spot))
            for strike in strikes[:3]:
                quantity = lot*multiplier
                short = [next((q for q in chain if q.contract.strike == strike and
                               _option_type(q.contract) == kind and q.bid_size >= quantity),None)
                         for kind in ('CE','PE')]
                if any(q is None for q in short):
                    continue
                wings = []
                for kind,target in (('CE',strike+1000),('PE',strike-1000)):
                    candidates = [q for q in chain if _option_type(q.contract) == kind and
                                  (q.contract.strike >= target if kind == 'CE' else q.contract.strike <= target) and
                                  q.ask_size >= quantity]
                    if not candidates:
                        break
                    wings.append(min(candidates,key=lambda q:abs(q.contract.strike-target)))
                if len(wings) != 2:
                    continue
                credit = sum(q.bid for q in short)-sum(q.ask for q in wings)
                if not isfinite(credit) or credit <= 0:
                    continue
                width = max(wings[0].contract.strike-strike,strike-wings[1].contract.strike)
                max_loss = max(0.,(width-credit)*quantity)+800*multiplier
                if max_loss > 100000*multiplier:
                    continue
                return Plan('nfv5',[Leg(q,'BUY',quantity) for q in wings]+[
                    Leg(q,'SELL',quantity) for q in short],
                    'NIFTY v5 paper ATM straddle with 1000-point protective wings',
                    4000*multiplier, 1e12, max_loss)
    return None
