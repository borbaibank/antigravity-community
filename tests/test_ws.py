import asyncio
import websockets
import json
import os

async def test_ws():
    token = os.getenv("DASHBOARD_PASSCODE", "")
    suffix = f"?token={token}" if token else ""
    uri = f"ws://localhost:8000/ws{suffix}"
    async with websockets.connect(uri) as ws:
        msg = await ws.recv()
        data = json.loads(msg)
        print("Live Mode:", data.get("live_mode"))
        print("Exchange Account:", json.dumps(data.get("exchange_account", {}), indent=2))
        
asyncio.run(test_ws())
