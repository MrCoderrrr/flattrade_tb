import hashlib
import time
from urllib.parse import parse_qs, urlparse
import pyotp
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from NorenRestApiPy.NorenApi import NorenApi

import creds

class FlattradeAPI(NorenApi):
    def __init__(self):
        super().__init__(
            host='https://piconnect.flattrade.in/PiConnectAPI/',
            websocket='wss://piconnect.flattrade.in/PiConnectWSAPI/'
        )

api = FlattradeAPI()

def get_visible_inputs(driver):
    """Finds all visible and interactable input fields on the screen."""
    return [
        elem for elem in driver.find_elements(By.TAG_NAME, "input") 
        if elem.is_displayed() and elem.is_enabled()
    ]

def fetch_request_code():
    api_key = str(creds.API_KEY).strip()
    auth_url = f"https://auth.flattrade.in/?app_key={api_key}"
    
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )
    driver.get(auth_url)
    
    wait = WebDriverWait(driver, 20)

    print("[1/4] Waiting for login page to render...")
    visible_inputs = wait.until(
        lambda d: get_visible_inputs(d) if len(get_visible_inputs(d)) >= 3 else False
    )

    print("[2/4] Typing User ID, Password, and TOTP...")
    user_field = visible_inputs[0]
    pwd_field = visible_inputs[1]
    totp_field = visible_inputs[2]

    user_field.clear()
    user_field.send_keys(str(creds.USER_ID).strip())

    pwd_field.clear()
    pwd_field.send_keys(str(creds.PASSWORD).strip())

    print("[3/4] Generating and entering TOTP...")
    clean_key = str(creds.TOTP_KEY).strip().replace(" ", "")
    totp_code = pyotp.TOTP(clean_key).now()
    print(f"      Generated TOTP Code: {totp_code}")

    totp_field.clear()
    totp_field.send_keys(totp_code)
    
    submit_btn = wait.until(
        EC.element_to_be_clickable((By.XPATH, "//button[.//span[contains(text(), 'Log In')]]"))
    )
    submit_btn.click()

    print("[4/4] Extracting authorization code from redirect URL...")
    try:
        wait.until(EC.url_contains("code="))
    except Exception as e:
        print("Timeout waiting for code. Current URL:", driver.current_url)
        driver.save_screenshot("error.png")
        with open("page_source.html", "w") as f:
            f.write(driver.page_source)
        driver.quit()
        raise e

    redirected_url = driver.current_url
    parsed_url = urlparse(redirected_url)
    request_code = parse_qs(parsed_url.query)['code'][0]
    
    driver.quit()
    return request_code

def authenticate_session():
    request_code = fetch_request_code()
    print(f"\n[SUCCESS] Request Code Obtained: {request_code}")
    
    # Generate SHA-256 Hash
    raw_token_str = f"{str(creds.API_KEY).strip()}{request_code}{str(creds.API_SECRET).strip()}"
    token_hash = hashlib.sha256(raw_token_str.encode('utf-8')).hexdigest()

    import requests
    url = "https://authapi.flattrade.in/trade/apitoken"
    payload = {
        "api_key": str(creds.API_KEY).strip(),
        "request_code": request_code,
        "api_secret": token_hash
    }
    
    auth_resp = requests.post(url, json=payload)
    if auth_resp.status_code == 200:
        access_token = auth_resp.json().get("token")
        if not access_token:
            print("Failed to get token:", auth_resp.text)
            return
    else:
        print(f"Error authenticating: {auth_resp.text}")
        return

    # Login to Flattrade API
    res = api.set_session(
        userid=str(creds.USER_ID).strip(),
        password='',
        usertoken=access_token
    )
    print("\n--- FLATTRADE SESSION ACTIVE ---")
    print("Login Status:", res)
    
    account_limits = api.get_limits()
    print("Account Cash Limits:", account_limits)

if __name__ == "__main__":
    authenticate_session()