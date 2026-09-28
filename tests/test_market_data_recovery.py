import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from strategy_lab.market_data import FeedError, FlattradeReadOnly
from strategy_lab.models import Bar, Contract, IST


class MarketDataRecoveryTests(unittest.TestCase):
    def test_mcx_atm_versions_request_only_one_pair(self):
        with tempfile.TemporaryDirectory() as root:
            feed = FlattradeReadOnly(Path(root))
            now = datetime(2026, 9, 28, 16, 40, tzinfo=IST)
            feed.bars = lambda *_: [Bar(now, 300, 301, 299, 300, 100, 1)]
            contracts = [Contract(f'NATURALGAS23OCT26{kind}{strike}', f'{kind}{strike}',
                                  'MCX', date(2026, 10, 23), strike, kind, 1250, .05)
                         for strike in (295, 300, 305) for kind in ('CE', 'PE')]
            feed.contracts = lambda *_: contracts
            requested = []
            feed.quotes = lambda rows, *_args, **_kwargs: requested.extend(rows) or []
            for strategy_id in ('mcxv1', 'mcxv3'):
                feed.snapshot('MCX', now, strategy_id=strategy_id)
                self.assertEqual({c.symbol for c in requested},
                                 {'NATURALGAS23OCT26CE300', 'NATURALGAS23OCT26PE300'})
                requested.clear()

    def test_bad_entry_contract_does_not_discard_other_fresh_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            feed = FlattradeReadOnly(Path(root))
            bad = Contract('NIFTY06OCT26C22900', '1', 'NFO', date(2026,10,6), 22900, 'CE', 65, .05)
            good = Contract('NIFTY06OCT26P22900', '2', 'NFO', date(2026,10,6), 22900, 'PE', 65, .05)
            now = datetime(2026,9,28,10,0,tzinfo=IST)

            def get_quote(endpoint, **fields):
                if fields['token'] == '1':
                    raise FeedError('Flattrade GetQuotes returned HTTP 400')
                return {'tsym':good.symbol,'ls':'65','bp1':'100.00','sp1':'100.05',
                        'lp':'100.00','bq1':'65','sq1':'65','ft':str(int(now.timestamp()))}

            feed._call = get_quote
            self.assertEqual([q.contract.symbol for q in feed.quotes([bad,good],now,strict=False)],
                             [good.symbol])
            with self.assertRaises(FeedError):
                feed.quotes([bad,good],now,strict=True)



if __name__ == '__main__':
    unittest.main()
