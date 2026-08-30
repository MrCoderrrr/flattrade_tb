import hashlib
import requests
import sys
import os
from creds import API_KEY, API_SECRET

def get_token(request_code):
    raw_token_str = f"{str(API_KEY).strip()}{request_code}{str(API_SECRET).strip()}"
    token_hash = hashlib.sha256(raw_token_str.encode('utf-8')).hexdigest()
    
    url = "https://authapi.flattrade.in/trade/apitoken"
    payload = {
        "api_key": str(API_KEY).strip(),
        "request_code": request_code,
        "api_secret": token_hash
    }
    
    print("Exchanging request_code for access_token via Flattrade API...")
    auth_resp = requests.post(url, json=payload)
    if auth_resp.status_code == 200:
        access_token = auth_resp.json().get("token")
        if not access_token:
            print("Failed to get token:", auth_resp.text)
            return None
        return access_token
    else:
        print(f"Error authenticating: {auth_resp.text}")
        return None

if __name__ == "__main__":
    print("\n" + "="*50)
    print("FLATTRADE MANUAL LOGIN SCRIPT")
    print("="*50)
    print(f"Auth Link: https://auth.flattrade.in/?app_key={str(API_KEY).strip()}")
    
    request_code = input("\nPaste request_code: ").strip()
    if not request_code:
        sys.exit("No code provided.")
        
    token = get_token(request_code)
    if token:
        with open("token.txt", "w") as f:
            f.write(token)
        print("\n[SUCCESS] Token saved to token.txt!")
        print("Your background server_bot.py will now automatically use this token.")
    else:
        print("\n[FAILED] Could not generate token.")
