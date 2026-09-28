import csv
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from strategy_lab.analytics import AnalyticsStore, regime_from_bars
from strategy_lab.models import IST
from strategy_lab.multi_runtime import MultiController
from test_fixtures import Feed
from test_fixtures import nifty_fixture


class RegimeTests(unittest.TestCase):
    def test_session_regime_uses_only_prior_baseline(self):
        baseline = [(0.03 + i*.001, 40+i) for i in range(20)]
        trending = [(str(i),100+i,101+i,99+i,100+i,None,'test') for i in range(30)]
        result = regime_from_bars(trending, baseline, 20)
        self.assertEqual(result['regime'], 'trending')
        self.assertEqual(result['quality'], 'complete')
        self.assertEqual(regime_from_bars(trending[:5],baseline,20)['quality'], 'partial')
        self.assertEqual(regime_from_bars(trending,baseline[:10],20)['regime'], 'collecting_baseline')

    def test_imported_market_data_does_not_invent_strategy_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)/'data'/'market_history'/'nifty_1y'
            spot = base/'nifty_spot_1m'
            vix = base/'india_vix_1m'
            spot.mkdir(parents=True)
            vix.mkdir(parents=True)
            for directory,prefix,price in [(spot,'nifty_spot_1m',25000),
                                           (vix,'india_vix_1m',15)]:
                with (directory/f'{prefix}_2026-09-22.csv').open('w',newline='') as handle:
                    writer=csv.writer(handle)
                    writer.writerow(['Timestamp','Open','High','Low','Close','Volume'])
                    from datetime import datetime
                    start=datetime(2026,9,22,9,15)
                    for i in range(260):
                        stamp=(start+timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')
                        writer.writerow([stamp,price+i*.1,price+i*.1+1,
                                         price+i*.1-1,price+i*.1,0])
            store=AnalyticsStore(Path(temp),None)
            try:
                self.assertEqual(store.backfill_nifty_history(),1)
                self.assertEqual(store.summary()['coverage'][0]['days'],1)
                self.assertEqual(store.summary()['leaders'],{})
                self.assertEqual(store.summary()['daily'],[])
                self.assertEqual(store.detail('nfv1','2026-09')['daily'],[])
            finally:
                store.close()

    def test_leader_requires_ten_shared_condition_days(self):
        with tempfile.TemporaryDirectory() as temp:
            store=AnalyticsStore(Path(temp),None,
                clock=lambda:datetime(2026,9,28,3,0,tzinfo=IST))
            try:
                for i in range(10):
                    day=f'2026-09-{i+1:02d}'
                    for sid,pct in [('nfv1',.4),('nfv3',.2)]:
                        store.db.execute('''INSERT INTO strategy_days VALUES
                            (?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (sid,day,'NIFTY','paper',pct*2000,200000,pct,1,50,10,
                             'choppy','complete'))
                store.db.commit()
                summary=store.summary()
                self.assertEqual(summary['leaders']['NIFTY:choppy'],'nfv1')
                self.assertEqual(summary['comparison_days']['NIFTY:choppy'],10)
                store.db.execute("DELETE FROM strategy_days WHERE strategy_id='nfv3' AND day='2026-09-10'")
                store.db.commit()
                self.assertNotIn('NIFTY:choppy',store.summary()['leaders'])
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
