"""Independent paper runtimes behind one dashboard.

Each strategy owns a separate ledger, control lock, market-data worker and risk
state. The original market-keyed ledger remains available for existing sessions
and history. No live order executor is introduced by this supervisor.
"""
from __future__ import annotations

import copy
from pathlib import Path

from .catalog import SPECS, resolve
from .models import IST
from .market_data import FlattradeReadOnly
from .runtime import Controller, LIVE_REASON, MARKETS


class MultiController:
    def __init__(self, root: Path, feed=None, clock=None):
        self.root = Path(root)
        # All paper workers share one read-only quote cache and broker budget.
        feed = feed or FlattradeReadOnly(self.root, auto_refresh=True)
        self.legacy = Controller(self.root, feed=feed, clock=clock)
        base = self.root / "data" / "strategy_lab" / "instances"
        self.children = {
            sid: Controller(self.root, feed=feed, clock=clock,
                            storage_directory=base / sid)
            for sid in SPECS
        }
        self.feed = self.children['nfv5'].feed
        for child in [self.legacy, *self.children.values()]:
            # Idle instances have no worker yet. Archive yesterday's result now
            # so an idle strategy cannot inflate today's aggregate P&L.
            with child.lock:
                if not any(s['positions'] for s in child.data['sessions'].values()):
                    child._roll_day(child.clock().astimezone(IST))
                    child._save()
        capital = self.legacy.status()['account']['configured_capital']
        for child in self.children.values():
            state = child.status()
            if (state['account']['configured_capital'] != capital and
                    not state['schedules'] and
                    not any(s.get('date') or s['positions'] for s in state['sessions'].values())):
                child.configure(capital=capital)
        from .analytics import AnalyticsStore
        self.analytics = AnalyticsStore(self.root, self, clock=clock)

    def _legacy_owner(self, strategy_id):
        state = self.legacy.status()
        return any(
            (session.get('strategy_id') == strategy_id and
             (session.get('date') or session['positions'] or market in state['schedules'])) or
            state['schedules'].get(market, {}).get('strategy_id') == strategy_id
            for market, session in state['sessions'].items()
        )

    def _target(self, market, strategy_id=None):
        if strategy_id is not None:
            spec = resolve(market, strategy_id)
            child = self.children[spec.id]
            own = child.status()
            session = own['sessions'][market]
            if (own['schedules'].get(market) or session['positions'] or
                    session['state'] not in ('STOPPED', 'SESSION_COMPLETE') or
                    session.get('date')):
                return child
            if self._legacy_owner(spec.id):
                return self.legacy
            return child
        active = []
        for sid, child in self.children.items():
            if SPECS[sid].market != market:
                continue
            state = child.status()
            s = state['sessions'][market]
            if state['schedules'].get(market) or s['positions'] or s['state'] not in ('STOPPED', 'SESSION_COMPLETE'):
                active.append(child)
        old = self.legacy.status()
        s = old['sessions'][market]
        if old['schedules'].get(market) or s['positions'] or s['state'] not in ('STOPPED', 'SESSION_COMPLETE'):
            active.append(self.legacy)
        if len(active) > 1:
            raise ValueError('Specify strategy_id to control one of the active paper strategies')
        return active[0] if active else self.legacy

    def start(self, market, mode, multiplier, capital, confirmation='', strategy_id=None):
        spec = resolve(market, strategy_id)
        if self._legacy_owner(spec.id):
            old = self.legacy.status()
            s = old['sessions'][market]
            if (old['schedules'].get(market) or s['positions'] or
                    s['state'] not in ('STOPPED', 'SESSION_COMPLETE') or
                    s.get('date') == self.legacy.clock().astimezone(IST).date().isoformat()):
                raise ValueError('This strategy already has a legacy session today; stop it before starting another')
        shared_capital = self.legacy.status()['account']['configured_capital']
        if capital != shared_capital:
            raise ValueError('Use the account capital saved in Settings')
        child = self.children[spec.id]
        own = child.status()
        if own['account']['configured_capital'] != shared_capital:
            child.configure(capital=shared_capital)
        child.start(market, mode, multiplier, capital, confirmation=confirmation,
                    strategy_id=spec.id)
        child.start_worker()
        return self.status()

    def stop(self, market, strategy_id=None):
        self._target(market, strategy_id).stop(market)
        return self.status()

    def pause(self, market, paused=True, strategy_id=None):
        self._target(market, strategy_id).pause(market, paused)
        return self.status()

    def kill(self):
        for child in self.children.values():
            child.kill()
        self.legacy.kill()
        return self.status()

    def configure(self, *, capital=None, live_permission=None):
        states = [self.legacy.status()] + [c.status() for c in self.children.values()]
        if capital is not None:
            now = self.legacy.clock().astimezone(IST).date().isoformat()
            if any(st['schedules'] or any(s.get('date') == now or s['positions']
                                            for s in st['sessions'].values()) for st in states):
                raise ValueError('Account capital is fixed after any strategy starts or is scheduled')
        self.legacy.configure(capital=capital, live_permission=live_permission)
        for child in self.children.values():
            child.configure(capital=capital, live_permission=live_permission)
        return self.status()

    def status(self):
        base = self.legacy.status()
        result = copy.deepcopy(base)
        result['sessions'] = {}
        result['schedules'] = {}
        result['strategy_history'] = list(base['strategy_history'])
        events = list(base['events'])
        totals = [base['account']]
        for sid, child in self.children.items():
            state = child.status()
            market = SPECS[sid].market
            session = state['sessions'][market]
            if session.get('date') or session['positions']:
                result['sessions'][sid] = session
            if market in state['schedules']:
                result['schedules'][sid] = state['schedules'][market]
            result['strategy_history'].extend(state['strategy_history'])
            events.extend(state['events'])
            totals.append(state['account'])
        for market, session in base['sessions'].items():
            sid = session.get('strategy_id')
            if not sid:
                continue
            if session.get('date') or session['positions']:
                result['sessions'][sid + ':legacy' if sid in result['sessions'] else sid] = session
            if market in base['schedules']:
                planned = base['schedules'][market]
                result['schedules'][planned['strategy_id']] = planned
        result['events'] = sorted(events, key=lambda event: event['timestamp'])[-150:]
        account = result['account']
        today = result['today']
        for field in ('daily_pnl', 'lifetime_pnl'):
            account[field] = round(sum(float(a.get(field, 0)) for a in totals
                                       if field != 'daily_pnl' or a.get('date') == today), 4)
        account['halted'] = all(bool(a.get('halted')) for a in totals)
        result['paper_capital_basis'] = sum(
            float(s['capital']) for s in result['sessions'].values()
            if s.get('date') == today and s.get('mode') == 'paper'
        ) or float(account['configured_capital'])
        result['live_enabled'] = False
        result['live_reason'] = LIVE_REASON
        result['paper_capital_note'] = 'Paper strategies simulate the declared capital independently; simultaneous returns are not deployable account returns.'
        return result

    def attach_nifty_stream(self, stream):
        self.children['nfv5'].attach_nifty_stream(stream)
        self.legacy.attach_nifty_stream(stream)

    def attach_nifty_observer(self, observer):
        self.children['nfv5'].attach_nifty_observer(observer)
        self.legacy.attach_nifty_observer(observer)

    def nifty_pin_strikes(self):
        strikes = self.legacy.nifty_pin_strikes()
        for sid, child in self.children.items():
            if SPECS[sid].market == 'NIFTY':
                strikes.extend(child.nifty_pin_strikes())
        return sorted(set(strikes))

    def nifty_position_expiry(self):
        # The option-chain recorder must follow any active NIFTY strategy,
        # including v1/v3 when v5 has not opened a position yet.
        for sid in ('nfv5', 'nfv1', 'nfv3'):
            expiry = self.children[sid].nifty_position_expiry()
            if expiry:
                return expiry
        return self.legacy.nifty_position_expiry()

    def start_worker(self):
        self.analytics.start()
        old = self.legacy.status()
        if old['schedules'] or any(s['positions'] or s['state'] not in ('STOPPED', 'SESSION_COMPLETE')
                                   for s in old['sessions'].values()):
            self.legacy.start_worker()
        for sid, child in self.children.items():
            state = child.status()
            market = SPECS[sid].market
            s = state['sessions'][market]
            if state['schedules'] or s['positions'] or s['state'] not in ('STOPPED', 'SESSION_COMPLETE'):
                child.start_worker()

    def shutdown(self):
        self.analytics.close()
        for child in self.children.values():
            child.shutdown()
        self.legacy.shutdown()
