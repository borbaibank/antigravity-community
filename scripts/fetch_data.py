"""Fetch top-50 USDT-M 4h OHLCV into cache/ (backtest data prep).

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/fetch_data.py
"""
import os
import sys
import time
from datetime import datetime, timedelta

import ccxt
import pandas as pd
from tqdm import tqdm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts._paths import CACHE_DIR

def fetch_data():
    print("Initializing Binance Futures...")
    exchange = ccxt.binance({'options': {'defaultType': 'future'}})
    markets = exchange.load_markets()
    tickers = exchange.fetch_tickers()
    
    # 1.5 years = 547.5 days
    cutoff_time = (datetime.now() - timedelta(days=547.5)).timestamp() * 1000
    
    eligible_markets = []
    
    for symbol, market in markets.items():
        if market['quote'] != 'USDT' or not market.get('active', True):
            continue
            
        info = market.get('info', {})
        onboard_date = info.get('onboardDate')
        
        if not onboard_date:
            continue
            
        onboard_date = int(onboard_date)
        if onboard_date <= cutoff_time:
            quote_vol = tickers.get(symbol, {}).get('quoteVolume', 0)
            eligible_markets.append({
                'symbol': symbol,
                'onboardDate': onboard_date,
                'quoteVolume': float(quote_vol)
            })
            
    # Sort by quoteVolume and pick top 50
    eligible_markets.sort(key=lambda x: x['quoteVolume'], reverse=True)
    top_50 = eligible_markets[:50]
    
    print(f"Found {len(eligible_markets)} eligible markets. Selecting Top 50 by 24h Quote Volume.")
    
    os.makedirs(CACHE_DIR, exist_ok=True)
        
    start_since = int(cutoff_time)
    
    total_candles = 0
    for m in tqdm(top_50, desc="Fetching OHLCV (4h)"):
        symbol = m['symbol']
        all_ohlcv = []
        current_since = start_since
        fail_count = 0
        
        retry_delay = 2
        while True:
            time.sleep(0.05)  # baseline rate limit protection
            try:
                ohlcv = exchange.fetch_ohlcv(symbol, '4h', since=current_since, limit=1500)
                if not ohlcv:
                    break

                if all_ohlcv and ohlcv[-1][0] <= all_ohlcv[-1][0]:
                    break

                all_ohlcv.extend(ohlcv)
                current_since = ohlcv[-1][0] + 1
                retry_delay = 2  # reset backoff on success

                if current_since > int(time.time() * 1000):
                    break

            except Exception as e:
                err = str(e)
                fail_count += 1
                if '429' in err or 'Too Many Requests' in err:
                    print(f"Rate limited on {symbol}, backing off {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # exponential, cap at 60s
                    fail_count -= 1  # rate limit is not a hard failure
                else:
                    print(f"Error fetching {symbol}: {e}")
                    time.sleep(retry_delay)
                if fail_count > 3:
                    break
                
        if all_ohlcv:
            df = pd.DataFrame(all_ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            
            # Keep only exact 4H timestamps (cleaning potential noisy data)
            # drop duplicates
            df.drop_duplicates(subset=['timestamp'], inplace=True)
            df.set_index('timestamp', inplace=True)
            
            total_candles += len(df)
            safe_symbol = symbol.replace('/', '_').replace(':', '_')
            df.to_parquet(os.path.join(CACHE_DIR, f"{safe_symbol}.parquet"))

    print(f"Data fetching complete! Cache saved. Total candles fetched: {total_candles}")

if __name__ == '__main__':
    fetch_data()
