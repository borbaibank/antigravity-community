import asyncio
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from binance_live import get_account

async def test():
    client = httpx.AsyncClient()
    try:
        acc = await get_account(client)
        print("Total keys in acc:", list(acc.keys()))
        assets = acc.get("assets", [])
        print(f"Total assets: {len(assets)}")
        
        usdt_assets = [a for a in assets if a["asset"] == "USDT"]
        print("USDT assets:", usdt_assets)
        
        # Print first few assets to see what's there
        print("First 3 assets:", assets[:3])
    except Exception as e:
        print("Error:", e)
    finally:
        await client.aclose()

asyncio.run(test())
