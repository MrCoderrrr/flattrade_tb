import io
import unittest
import zipfile
from datetime import date

from strategy_lab.option_chain import parse_master


class OptionChainTests(unittest.TestCase):
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
