from api_helper import NorenApiPy
from creds import USER_ID
import datetime

api = NorenApiPy()
with open("token.txt", "r") as f: token = f.read().strip()
api.set_session(userid=USER_ID, password="", usertoken=token)

res = api.searchscrip(exchange="MCX", searchtext="NATURALGAS")
print(f"Total results: {len(res) if res else 'None'}")
if res:
    for item in res:
        tsym = str(item.get("tsym", "")).upper()
        if "NATURALGAS" in tsym and "MINI" not in tsym and "FUT" in tsym:
            print("MATCH:", item.get("tsym"), item.get("exd"), item.get("token"))
