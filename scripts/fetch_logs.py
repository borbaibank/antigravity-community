"""Fetch last 20 log lines from the running bot dashboard.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe scripts/fetch_logs.py
"""
import asyncio
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

async def get_logs():
    async with httpx.AsyncClient() as client:
        token = os.getenv("DASHBOARD_PASSCODE", "")
        headers = {'X-Passcode': token} if token else {}
        r = await client.get('http://localhost:8000/api/logs', headers=headers)
        logs = r.json().get('logs', [])
        for log in logs[-20:]:  # Last 20 lines
            print(log)

asyncio.run(get_logs())
