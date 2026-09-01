import sys
sys.path.append('/home/ubuntu/flattrade_tb/flattrade_tb')
from api_helper import NorenApiPy
from creds import USER_ID

api = NorenApiPy()
with open('/home/ubuntu/flattrade_tb/flattrade_tb/token.txt', 'r') as f:
    api.set_session(userid=USER_ID, password='', usertoken=f.read().strip())

res = api.searchscrip(exchange="MCX", searchtext="NATGAS CE")
if res and res.get("values"):
    for item in res["values"][:10]:
        print(item.get("tsym"))
