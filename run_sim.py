import pandas as pd
df_s = pd.read_csv("/Users/vanshilpatel/Desktop/ai trading tradingview/tradingbot/data/backtest/spot_1m/nifty_spot_1m_2026-08-19.csv")
df_o = pd.read_csv("/Users/vanshilpatel/Desktop/ai trading tradingview/tradingbot/data/backtest/options_1m/nifty_options_1m_2026-08-19.csv")
print("Spot:", df_s.columns.tolist())
print(df_s.head(2))
print("Options:", df_o.columns.tolist())
print(df_o.head(2))
