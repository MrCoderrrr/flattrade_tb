import io
import time
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path

from strategy_lab.models import IST
from strategy_lab.option_chain import OptionChainView, parse_master


class OptionChainTests(unittest.TestCase):
    def test_watched_strikes_include_near_atm_and_both_wings(self):
        contracts = {(float(strike), kind): (str(strike), kind)
                     for strike in range(23000, 25001, 50) for kind in ('CE', 'PE')}
        strikes = OptionChainView._watched_strikes(contracts, 24000)
        self.assertIn(23000, strikes)
        self.assertIn(25000, strikes)
        self.assertIn(24000, strikes)
        self.assertLessEqual(len(strikes), 13)

    def test_off_hours_does_not_open_broker_stream(self):
        class Stream:
            thread = None
            started = False
            def start(self): self.started = True
        stream = Stream()
        view = OptionChainView(Path('.'), clock=lambda:datetime(2026,9,26,12,tzinfo=IST), stream=stream)
        view.refresh()
        self.assertFalse(stream.started)
        self.assertIn('outside',view.reason)

    def test_missing_current_master_never_reuses_old_tokens(self):
        class Stream:
            thread = None
            def start(self): pass
            def latest(self, now): raise AssertionError('Stale token map must not be used')
        view = OptionChainView(Path('.'),clock=lambda:datetime(2026,9,28,9,20,tzinfo=IST),stream=Stream())
        view.catalog_day = date(2026,9,25)
        view.last_master_attempt = time.monotonic()
        view.refresh()
        self.assertIn('current nfo contract master',view.reason.lower())

    def test_nearest_future_expiry_and_nifty_only(self):
        content = ('Symbol,OptionType,Expiry,StrikePrice,Token,TradingSymbol\n'
                   'NIFTY,CE,01-Oct-2026,25000,123,NIFTYCE1\n'
                   'NIFTY,PE,01-Oct-2026,25000,124,NIFTYPE1\n'
                   'BANKNIFTY,CE,01-Oct-2026,25000,125,OTHER\n'
                   'NIFTY,CE,08-Oct-2026,25000,126,NIFTYCE2\n')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('NFO_symbols.txt',content)
        expiry,contracts=parse_master(stream.getvalue(),date(2026,9,26))
        self.assertEqual(expiry,date(2026,10,1))
        self.assertEqual(contracts[(25000.,'CE')],('123','NIFTYCE1'))
        self.assertEqual(len(contracts),2)

    def test_expiry_day_contracts_remain_observable_for_research(self):
        content = ('Symbol,OptionType,Expiry,StrikePrice,Token,TradingSymbol\n'
                   'NIFTY,CE,29-Sep-2026,24000,123,NIFTYCE0\n'
                   'NIFTY,PE,29-Sep-2026,24000,124,NIFTYPE0\n'
                   'NIFTY,CE,06-Oct-2026,24000,125,NIFTYCE1\n')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:
            archive.writestr('NFO_symbols.txt',content)
        expiry,contracts=parse_master(stream.getvalue(),date(2026,9,29))
        self.assertEqual(expiry,date(2026,9,29))
        self.assertEqual(len(contracts),2)
