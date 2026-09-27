import io
import time
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path

from strategy_lab.models import IST
from strategy_lab.option_chain import OptionChainView, parse_master, parse_master_all
from strategy_lab.nifty_flow import IndexTick


class OptionChainTests(unittest.TestCase):
    def test_watched_strikes_include_near_atm_and_both_wings(self):
        contracts = {(float(strike), kind): (str(strike), kind)
                     for strike in range(23000, 25001, 50) for kind in ('CE', 'PE')}
        strikes = OptionChainView._watched_strikes(contracts, 24000)
        self.assertIn(23000, strikes)
        self.assertIn(25000, strikes)
        self.assertIn(24000, strikes)
        self.assertLessEqual(len(strikes), 13)

    def test_original_held_strike_stays_subscribed_after_spot_moves(self):
        contracts = {(float(strike), kind):(str(strike),kind)
                     for strike in range(23000,27001,50) for kind in ('CE','PE')}
        strikes = OptionChainView._watched_strikes(contracts,26000,[24000,25000])
        self.assertIn(24000,strikes)
        self.assertIn(25000,strikes)

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
        self.assertEqual(len(parse_master_all(stream.getvalue(),date(2026,9,26))),2)

    def test_held_later_expiry_switches_subscriptions(self):
        now=datetime(2026,9,28,9,30,tzinfo=IST)
        class Stream:
            thread=None
            def start(self): pass
            def latest(self,_now):
                return {'ready':True,'reason':'','ticks':[IndexTick(now,25000)],'books':{}}
            def set_contracts(self,selected): self.selected=selected
        stream=Stream()
        view=OptionChainView(Path('.'),clock=lambda:now,stream=stream,
                             preferred_expiry=lambda:'2026-10-06',
                             pinned_strikes=lambda:[25000.])
        view.catalog_day=now.date()
        view.expiry=date(2026,9,29)
        view.all_expiries={date(2026,9,29):{(25000.,'CE'):('101','NIFTY1')},
                           date(2026,10,6):{(25000.,'CE'):('201','NIFTY2')}}
        view.contracts=view.all_expiries[view.expiry]
        view.refresh()
        self.assertEqual(view.expiry,date(2026,10,6))
        self.assertIn('NFO|201',stream.selected)
        self.assertNotIn('NFO|101',stream.selected)

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
