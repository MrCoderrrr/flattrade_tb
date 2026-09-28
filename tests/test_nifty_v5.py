"""Paper-only v5 state transitions with observed-second fixtures."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from strategy_lab.models import Bar, IST
from strategy_lab.nifty_v5 import build_plan, five_minute_bars, ready_to_open, stop_parameters
from strategy_lab.runtime import Controller
from test_fixtures import Feed
from test_fixtures import quote


def fixture():
    now = datetime(2026,9,22,9,20,31,tzinfo=IST)
    yesterday = datetime(2026,9,21,14,0,tzinfo=IST)
    today = datetime(2026,9,22,9,15,tzinfo=IST)
    bars = [Bar(yesterday+timedelta(minutes=i),25000,25001,24999,25000,0,1)
            for i in range(30)]
    bars += [Bar(today+timedelta(minutes=i),25000,25001,24999,25000,0,1)
             for i in range(5)]
    quotes = [quote('NIFTY',25000,kind,100,100.05,now) for kind in ('CE','PE')]
    quotes += [quote('NIFTY',26000,'CE',1,1.05,now),
               quote('NIFTY',24000,'PE',1,1.05,now)]
    return now,bars,quotes


class V5Tests(unittest.TestCase):
    def test_complete_five_minute_bars_and_protected_plan(self):
        now,bars,quotes = fixture()
        self.assertEqual(len(five_minute_bars(bars,now)),1)
        observation = {'eligible':True,'spot':25000}
        plan = build_plan(bars,quotes,now,1,200000,observation)
        self.assertIsNotNone(plan)
        self.assertEqual([x.side for x in plan.legs],['BUY','BUY','SELL','SELL'])
        self.assertEqual({x.quote.contract.strike for x in plan.legs if x.side=='BUY'},
                         {24000,26000})
        self.assertLess(plan.max_loss,100000)
        self.assertIsNone(build_plan(bars,quotes[:2],now,1,200000,observation))

    def test_dynamic_stops_have_hard_ceiling(self):
        calm = stop_parameters({'quality':.9,'volatility_ratio':1},True)
        choppy = stop_parameters({'quality':.1,'volatility_ratio':2},True)
        self.assertGreater(choppy[0],calm[0])
        self.assertGreater(choppy[1],calm[1])
        self.assertLessEqual(choppy[0],.30)

    def test_trending_observed_seconds_can_open_initial_straddle(self):
        now,_,_ = fixture()
        rows = [{'eligible':True,'score':80.,'timestamp':(now+timedelta(seconds=i)).isoformat()}
                for i in range(3)]
        self.assertTrue(ready_to_open(rows))
        rows[-1]['timestamp'] = (now+timedelta(seconds=5)).isoformat()
        self.assertFalse(ready_to_open(rows))

    def test_balanced_to_solo_to_reentry_keeps_original_wings(self):
        now,bars,quotes = fixture()
        with tempfile.TemporaryDirectory() as directory:
            feed = Feed(bars,quotes)
            controller = Controller(Path(directory),feed=feed,clock=lambda:now)
            controller.attach_nifty_stream(type('Stream',(),{'latest':lambda self,_now:{'ticks':[]}})())
            score = [0.]
            def observation(_bars,_ticks,stamp):
                return {'eligible':True,'score':score[0], 'timestamp':stamp.isoformat(),
                        'spot':25000.,'quality':.5,'volatility_ratio':1.,
                        'reason':'synthetic observed-second fixture'}
            try:
                controller.start('NIFTY','paper',1,200000,strategy_id='nfv5')
                with patch('strategy_lab.nifty_v5.flow',side_effect=observation):
                    for _ in range(3):
                        feed.quotes = [replace(q,timestamp=now) for q in quotes]
                        controller.tick()
                        now += timedelta(seconds=1)
                    session = controller.status()['sessions']['NIFTY']
                    self.assertEqual(session['v5_state'],'DUAL')
                    self.assertEqual(len(session['positions']),4)
                    self.assertNotIn('flow_history',session)
                    self.assertGreaterEqual(len(controller.v5_flow_history),3)
                    self.assertEqual(controller.nifty_pin_strikes(),[26000,24000,25000,25000,25000,25000])
                    score[0] = 80.
                    for _ in range(3):
                        feed.quotes = [replace(q,timestamp=now) for q in quotes]
                        controller.tick()
                        now += timedelta(seconds=1)
                    session = controller.status()['sessions']['NIFTY']
                    self.assertEqual(session['v5_state'],'SOLO_PE')
                    self.assertEqual([p['contract']['option_type'] for p in session['positions'] if p['side']=='SELL'],['PE'])
                    score[0] = 0.
                    for _ in range(6):
                        feed.quotes = [replace(q,timestamp=now) for q in quotes]
                        controller.tick()
                        now += timedelta(seconds=1)
                    session = controller.status()['sessions']['NIFTY']
                    self.assertEqual(session['v5_state'],'DUAL')
                    self.assertEqual({p['contract']['strike'] for p in session['positions'] if p['side']=='BUY'},
                                     {24000,26000})
                    self.assertEqual(session['entries'],2)
                    self.assertTrue(all(t['mode']=='paper' and t['simulated'] for t in session['trades']))
            finally:
                controller.shutdown()


if __name__=='__main__':
    unittest.main()
