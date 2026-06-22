import argparse
import asyncio
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import binance_live
from config import BINANCE_TESTNET, LEVERAGE

async def run_test(execute: bool):
    sym = "BTCUSDT"
    
    print(f"=== Starting Live Execution Test ===")
    print(f"Target Symbol: {sym}")
    print(f"Testnet Mode: {BINANCE_TESTNET}")
    print(f"Execute real testnet order: {execute}")

    async with httpx.AsyncClient() as client:
        print("1. Fetching minimal exchange info...")
        await binance_live.fetch_exchange_info(client)
        
        print("2. Fetching current market ticker...")
        resp = await client.get(f"{binance_live.BASE_URL}/fapi/v1/ticker/price?symbol={sym}")
        current_price = float(resp.json()["price"]) if resp.status_code == 200 else 60000.0
        print(f"Current {sym} Price roughly: {current_price:.2f}")

        test_notional = 150.0  
        ep = current_price
        sl = ep * 0.99  
        tp = ep * 1.05  
        qty = binance_live.round_qty(sym, test_notional / ep)
        print(f"\n3. Test parameters: QTY={qty}, EP={ep}, SL={sl}, TP={tp}")

        entry_opened = False
        actual_qty = 0.0
        try:
            if not execute:
                print("\n4. Dry-running MARKET order via /fapi/v1/order/test...")
                await binance_live.test_market_order(client, sym, "BUY", qty)
                print("DRY RUN OK: no position was opened. Re-run with --execute to place a real testnet order.")
                return

            print("\n4. Setting margin type (ISOLATED)...")
            await binance_live.set_margin_type(client, sym, "ISOLATED")
            print("   Setting leverage...")
            await binance_live.set_leverage(client, sym, LEVERAGE)

            print("\n5. Placing MARKET order on testnet...")
            entry_res = await binance_live.place_market_order(client, sym, "BUY", qty)
            fill_price = float(entry_res.get("avgPrice") or ep)
            actual_qty = float(entry_res.get("executedQty") or qty)
            entry_opened = True
            print(f"ENTRY SUCCESS: Filled at {fill_price} (Qty: {actual_qty})")

            print("\n6. Placing Stop Loss and Take Profit orders...")
            await binance_live.place_stop_loss(client, sym, "SELL", sl)
            print(f"SL Placed at {sl}")
            await binance_live.place_take_profit(client, sym, "SELL", tp)
            print(f"TP Placed at {tp}")
            
            print("\nTEST COMPLETED SUCCESSFULLY!")
            print("Note: An actual test position is now open on your Testnet account.")
            
        except httpx.HTTPStatusError as e:
            print(f"\nAPI Error: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            print(f"\nUnexpected Error: {e}")
        finally:
            if execute and entry_opened:
                print("\n7. Cleanup: canceling algo orders and closing the test position...")
                try:
                    await binance_live.cancel_all_algo_orders(client, sym)
                    await binance_live.cancel_all_orders(client, sym)
                    await binance_live.place_market_order(client, sym, "SELL", actual_qty, reduce_only=True)
                    print("Cleanup OK: test position closed.")
                except Exception as e:
                    print(f"Cleanup failed. Manual testnet check required for {sym}: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Safe Binance Futures testnet order smoke test.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually place a testnet order. Default is a dry run using Binance's test endpoint.",
    )
    args = parser.parse_args()
    asyncio.run(run_test(args.execute))
