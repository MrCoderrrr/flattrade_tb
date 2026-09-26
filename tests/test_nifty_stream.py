"""Broker delta-feed handling; no real WebSocket or order calls."""
import json
import tempfile
import unittest
from datetime import datetime,timedelta
from pathlib import Path

from strategy_lab.models import IST
from strategy_lab.nifty_stream import NiftyStream


class FakeSocket:
    def __init__(self):self.sent=[];self.closed=False
    def send(self,value):self.sent.append(json.loads(value))
    def close(self):self.closed=True


class StreamTests(unittest.TestCase):
    def test_auth_subscription_snapshot_delta_and_reconnect(self):
        now=datetime(2026,9,22,9,20,tzinfo=IST)
        with tempfile.TemporaryDirectory() as tmp:
            token=Path(tmp)/'token';token.write_text('example-secret')
            stream=NiftyStream(token,'user123',clock=lambda:now)
            stream.set_contracts(['NFO|123'])
            ws=FakeSocket();stream.on_open(ws)
            self.assertEqual(ws.sent[0]['t'],'a')
            self.assertFalse(stream.latest(now)['authenticated'])
            stream.on_message(ws,json.dumps({'t':'ck','s':'OK'}))
            self.assertEqual(ws.sent[1]['k'],'NFO|123#NSE|26000')
            stream.on_message(ws,json.dumps({'t':'tk','e':'NSE','tk':'26000','lp':'25000'}))
            stream.on_message(ws,json.dumps({'t':'tk','e':'NFO','tk':'123','bp1':'100','sp1':'101','bq1':'75','sq1':'75'}))
            now+=timedelta(seconds=1)
            stream.on_message(ws,json.dumps({'t':'tf','e':'NSE','tk':'26000','lp':'25001'}))
            stream.on_message(ws,json.dumps({'t':'tf','e':'NFO','tk':'123','sp1':'102'}))
            latest=stream.latest(now)
            self.assertTrue(latest['ready'])
            self.assertEqual(latest['books']['NFO|123']['fields']['bp1'],'100')
            self.assertEqual(latest['books']['NFO|123']['fields']['sp1'],'102')
            self.assertEqual(len(latest['ticks']),2)
            now+=timedelta(seconds=4)
            self.assertFalse(stream.latest(now)['ready'])
            stream.on_close(ws)
            self.assertFalse(stream.latest(now)['books'])

    def test_partial_delta_does_not_make_book_or_tick(self):
        now=datetime(2026,9,22,9,20,tzinfo=IST)
        with tempfile.TemporaryDirectory() as tmp:
            token=Path(tmp)/'token';token.write_text('example-secret')
            stream=NiftyStream(token,'user123',clock=lambda:now)
            ws=FakeSocket();stream.on_open(ws)
            stream.on_message(ws,json.dumps({'t':'ck','s':'OK'}))
            stream.on_message(ws,json.dumps({'t':'tf','e':'NSE','tk':'26000','lp':'25000'}))
            self.assertFalse(stream.latest(now)['ticks'])
            stream.on_message(ws,json.dumps({'t':'tk','e':'NSE','tk':'26000','lp':'25000'}))
            self.assertEqual(len(stream.latest(now)['ticks']),1)
            stream.on_message(ws,json.dumps({'t':'tf','e':'NSE','tk':'26000','v':'100'}))
            self.assertEqual(len(stream.latest(now)['ticks']),1)
