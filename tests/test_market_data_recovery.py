import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from strategy_lab.market_data import FeedError, FlattradeReadOnly
from strategy_lab.models import Bar, Contract, IST


class MarketDataRecoveryTests(unittest.TestCase):
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

    def test_ineligible_v2_signal_does_not_request_option_quotes(self):
        with tempfile.TemporaryDirectory() as root:
            feed = FlattradeReadOnly(Path(root))
            now = datetime(2026,9,28,9,20,tzinfo=IST)
            feed.bars = lambda *args: [Bar(now,23000,23001,22999,23000,100,5)]
            feed.contracts = lambda *args: self.fail('ineligible signal fetched option chain')
            bars, quotes = feed.snapshot('NIFTY',now,strategy_id='nfv2')
            self.assertEqual(len(bars),1)
            self.assertEqual(quotes,[])


if __name__ == '__main__':
    unittest.main()
