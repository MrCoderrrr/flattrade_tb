import os, sys, json
sys.path.append('/home/ubuntu/flattrade_tb')
from flattrade_tb.api_helper import NorenApiPy
from flattrade_tb.creds import USER_ID

api = NorenApiPy()
with open('/home/ubuntu/flattrade_tb/flattrade_tb/token.txt', 'r') as f:
    api.set_session(userid=USER_ID, password='', usertoken=f.read().strip())

orders = api.get_order_book()
for o in orders[:20]:
    if o.get('status') == 'REJECTED':
        print(f"REJECTED: {o.get('trantype')} {o.get('tsym')} @ {o.get('prc')} - Reason: {o.get('rejreason')}")
    elif o.get('status') == 'OPEN':
        print(f"OPEN: {o.get('trantype')} {o.get('tsym')} @ {o.get('prc')}")
