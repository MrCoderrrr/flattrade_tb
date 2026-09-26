"""One-second flow calculations and state transitions on observed fixtures."""
import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from strategy_lab.models import Bar, IST
from strategy_lab.nifty_flow import IndexTick, _weights, decision, decision_bands, evaluate


def fixture(direction=1):
    now=datetime(2026,9,22,9,20,31,tzinfo=IST)
    yesterday=datetime(2026,9,21,14,0,tzinfo=IST)
    today=datetime(2026,9,22,9,15,tzinfo=IST)
    prior=[Bar(yesterday+timedelta(minutes=i),25000+i*.4,25001+i*.4,24999+i*.4,25000+i*.4,0,1) for i in range(30)]
    latest=prior[-1].close
    current=[Bar(today+timedelta(minutes=i),latest+direction*i*2,latest+direction*i*2+1,
                 latest+direction*i*2-1,latest+direction*i*2,0,1) for i in range(5)]
    spot=current[-1].close
    ticks=[IndexTick(now-timedelta(seconds=30-i),spot+direction*.3*i) for i in range(31)]
    return prior+current,[],ticks,now


class FlowTests(unittest.TestCase):
    def test_signed_second_score_and_dynamic_weights(self):
        for direction in (1,-1):
            observation=evaluate(*fixture(direction))
            self.assertTrue(observation['eligible'],observation)
            self.assertGreater(observation['score']*direction,0)
            self.assertAlmostEqual(sum(observation['weights'].values()),1,places=2)
            self.assertGreaterEqual(observation['quality'],0)

    def test_situational_weights_change_continuously_and_with_volatility(self):
        chop=_weights(.2,1)
        transition=_weights(.5,1)
        trend=_weights(.9,1)
        elevated=_weights(.9,1.8)
        self.assertGreater(chop[2],trend[2])
        self.assertGreater(transition[0],trend[0])
        self.assertGreater(trend[1],chop[1])
        self.assertGreater(elevated[1],trend[1])
        self.assertAlmostEqual(sum(elevated),1)

    def test_no_future_or_stale_tick(self):
        bars,bars5,ticks,now=fixture()
        self.assertFalse(evaluate(bars,bars5,ticks,now+timedelta(seconds=4))['eligible'])
        self.assertFalse(evaluate(bars,bars5,ticks+[IndexTick(now+timedelta(seconds=1),30000)],now)['eligible'])
        self.assertFalse(evaluate(bars,bars5,list(reversed(ticks)),now)['eligible'])

    def test_missing_minute_and_missing_tick_history(self):
        bars,bars5,ticks,now=fixture()
        self.assertFalse(evaluate(bars[:-2]+bars[-1:],bars5,ticks,now)['eligible'])
        self.assertFalse(evaluate(bars,bars5,ticks[-10:],now)['eligible'])
        self.assertFalse(evaluate([replace(b,interval_minutes=5) for b in bars],bars5,ticks,now)['eligible'])

    def test_transition_hysteresis(self):
        start=datetime(2026,9,22,9,30,tzinfo=IST)
        rows=lambda values:[{'eligible':True,'score':v,
                              'timestamp':(start+timedelta(seconds=i)).isoformat(),
                              'quality':.5,'volatility_ratio':1.}
                             for i,v in enumerate(values)]
        self.assertEqual(decision(rows([20,10,-10]),'FLAT'),'OPEN_BOTH')
        self.assertEqual(decision(rows([56,58,60]),'DUAL'),'EXIT_CE')
        self.assertEqual(decision(rows([-56,-58,-60]),'DUAL'),'EXIT_PE')
        self.assertEqual(decision(rows([80]),'DUAL'),'EXIT_CE')
        self.assertEqual(decision(rows([60,20,19,18,17,15]),'SOLO_PE'),'REENTER_CE')
        self.assertEqual(decision(rows([-60,-20,-19,-18,-17,-15]),'SOLO_CE'),'REENTER_PE')
        self.assertIsNone(decision(rows([45,52]),'DUAL'))

    def test_noise_and_chop_widen_exit_but_preserve_reentry_gap(self):
        start=datetime(2026,9,22,9,30,tzinfo=IST)
        quiet=[{'eligible':True,'score':i*.1,'timestamp':start+timedelta(seconds=i),
                'quality':.8,'volatility_ratio':1.} for i in range(30)]
        noisy=[{'eligible':True,'score':(8 if i%2 else -8),
                'timestamp':start+timedelta(seconds=i),
                'quality':.15,'volatility_ratio':1.6} for i in range(30)]
        base=decision_bands(quiet)
        wide=decision_bands(noisy)
        self.assertGreater(wide['exit'],base['exit'])
        self.assertLessEqual(wide['exit'],72)
        self.assertGreaterEqual(wide['exit']-wide['reentry'],30)
        self.assertGreater(wide['urgent'],wide['exit'])

    def test_gap_and_repeated_tick_do_not_fake_confirmation(self):
        start=datetime(2026,9,22,9,30,tzinfo=IST)
        rows=[{'eligible':True,'score':60,'timestamp':start+timedelta(seconds=i)} for i in (0,1,4)]
        self.assertIsNone(decision(rows,'DUAL'))
        rows[-1]['timestamp']=rows[-2]['timestamp']
        self.assertIsNone(decision(rows,'DUAL'))

    def test_early_breakout_remains_reachable_at_low_quality(self):
        bars,bars5,ticks,now=fixture()
        result=evaluate(bars,bars5,ticks,now)
        self.assertTrue(result['eligible'])
        self.assertGreater(result['score'],decision_bands([result])['jump'])
