import sys
from api_helper import NorenApiPy
from creds import API_KEY, API_SECRET, USER_ID
import hashlib
import requests

def get_token(request_code):
    raw_token_str = f"{str(API_KEY).strip()}{request_code}{str(API_SECRET).strip()}"
    token_hash = hashlib.sha256(raw_token_str.encode('utf-8')).hexdigest()
    
    url = "https://authapi.flattrade.in/trade/apitoken"
    payload = {
        "api_key": str(API_KEY).strip(),
        "request_code": request_code,
        "api_secret": token_hash
    }
    
    auth_resp = requests.post(url, json=payload)
    if auth_resp.status_code == 200:
        return auth_resp.json().get("token")
    return None

print(f"Auth Link: https://auth.flattrade.in/?app_key={API_KEY}")
request_code = input("Paste request_code: ").strip()

token = get_token(request_code)
if not token:
    print("Auth failed!")
    sys.exit()

api = NorenApiPy()
api.set_session(userid=USER_ID, password='', usertoken=token)

print("\n--- STEP 3: GET LIMITS ---")
print(api.get_limits())

print("\n--- STEP 4: SEARCH SCRIP ---")
# Try searching for NIFTY 24050 PE
search_res = api.searchscrip(exchange='NFO', searchtext='NIFTY 24050 PE')
import json
print(json.dumps(search_res, indent=2))
