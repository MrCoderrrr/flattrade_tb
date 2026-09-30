"""Back up and reset paper-strategy P&L without touching market data or credentials.

Run only while the strategy dashboard service is stopped. Open simulated legs
are discarded, and strategies already switched ON are scheduled again.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from strategy_lab.catalog import resolve
from strategy_lab.models import IST
from strategy_lab.runtime import MARKETS, blank_session, next_session_date


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--confirm-discard-paper-legs', action='store_true', required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    data = root / 'data' / 'strategy_lab'
    backup = args.backup_dir.resolve()
    if backup.exists():
        raise RuntimeError('Choose a new backup directory; existing backups are never overwritten')
    ledgers = [data / 'ledger.sqlite3', *sorted((data / 'instances').glob('*/ledger.sqlite3'))]
    analytics = data / 'analytics.sqlite3'
    files = [p for p in [*ledgers, analytics] if p.is_file()]
    if not ledgers[0].is_file() or not analytics.is_file():
        raise RuntimeError('Paper controller or analytics ledger is missing')
    now = datetime.now(IST)
    with ExitStack() as stack:
        for path in ledgers:
            if not path.is_file():
                continue
            lock = stack.enter_context((path.parent / 'controller.lock').open('a+'))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f'Controller still owns {path}; stop the service') from None

        states = {}
        for path in ledgers:
            if not path.is_file():
                continue
            with sqlite3.connect(path) as db:
                row = db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else None
            if state is not None:
                for session in state['sessions'].values():
                    if session.get('mode') not in (None, 'paper'):
                        raise RuntimeError(f'Non-paper session in {path}; reset refused')
                if any(a.get('mode') != 'paper' for a in
                       state.get('strategy_authorizations', {}).values()):
                    raise RuntimeError(f'Non-paper authorization in {path}; reset refused')
            states[path] = state

        backup.mkdir(parents=True, mode=0o700)
        for path in files:
            target = backup / path.relative_to(data)
            target.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(path) as source, sqlite3.connect(target) as copy:
                source.backup(copy)

        for path, state in states.items():
            if state is None:
                continue
            authorizations = state.get('strategy_authorizations', {})
            state['sessions'] = {market: blank_session() for market in MARKETS}
            state['schedules'] = {
                market: {**authorization,
                         'scheduled_for': next_session_date(
                             now, resolve(market, authorization['strategy_id'])).isoformat()}
                for market, authorization in authorizations.items()
            }
            state['history'] = []
            state['events'] = []
            account = state['account']
            account.update(date=None, daily_pnl=0.0, lifetime_pnl=0.0,
                           peak_pnl=0.0, drawdown=0.0, halted=False)
            account.pop('daily_loss_fraction', None)
            with sqlite3.connect(path) as db:
                with db:
                    db.execute('UPDATE state SET payload=? WHERE id=1',
                               (json.dumps(state, allow_nan=False),))
                    db.execute('DELETE FROM journal')
                    db.execute('DELETE FROM strategy_daily')

        with sqlite3.connect(analytics) as db:
            with db:
                for table in ('strategy_ticks', 'strategy_trades', 'strategy_days'):
                    db.execute(f'DELETE FROM {table}')
            db.execute('VACUUM')
    print(f'Paper P&L and simulated legs reset; backup: {backup}')
    print(f'Authorized paper strategies remain ON and scheduled: {now.isoformat()}')


if __name__ == '__main__':
    main()
