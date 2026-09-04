import urllib.request, zipfile, io, os, pandas as pd
from datetime import datetime

def get_mcx_master():
    today = datetime.now().strftime("%Y-%m-%d")
    cache_file = f"MCX_symbols_{today}.csv"
    if not os.path.exists(cache_file):
        print("Downloading MCX master...")
        url = "https://api.shoonya.com/MCX_symbols.txt.zip"
        response = urllib.request.urlopen(url)
        with zipfile.ZipFile(io.BytesIO(response.read())) as z:
            with z.open("MCX_symbols.txt") as f:
                df = pd.read_csv(f)
                df.to_csv(cache_file, index=False)
    else:
        df = pd.read_csv(cache_file)
    return df

df = get_mcx_master()
print(df[df['Symbol'] == 'NATURALGAS'].head(3))
