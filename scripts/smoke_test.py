"""Quick smoke test for refactored bot package (TestClient, no live port required)."""

from __future__ import annotations

import sys

from starlette.testclient import TestClient

from bot.api.web import app


def main() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"[{mark}] {name}{suffix}")

    try:
        with TestClient(app) as client:
            r = client.get("/")
            check(
                "GET /",
                r.status_code == 200
                and "html" in (r.headers.get("content-type") or "").lower(),
                f"HTTP {r.status_code}, {len(r.content)} bytes",
            )

            r = client.get("/api/health")
            check("GET /api/health", r.status_code == 200, f"HTTP {r.status_code}")
            if r.status_code == 200:
                h = r.json()
                check(
                    "health payload",
                    isinstance(h, dict) and "last_price_ws_age_sec" in h,
                    f"keys sample={sorted(h.keys())[:8]}",
                )

            r = client.get("/api/logs")
            check("GET /api/logs", r.status_code == 200, f"HTTP {r.status_code}")

            r = client.get("/api/trades?limit=5")
            check(
                "GET /api/trades",
                r.status_code in (200, 400),
                f"HTTP {r.status_code}",
            )

            try:
                with client.websocket_connect("/ws") as ws:
                    msg = ws.receive_json()
                    has_positions = "open_positions" in msg
                    has_latest = "latest_prices" in msg
                    bad_keys = "c.latest_prices" in msg or "c.exchange_account" in msg
                    check(
                        "WS /ws first frame",
                        isinstance(msg, dict) and has_positions and has_latest and not bad_keys,
                        f"open_positions={has_positions} latest_prices={has_latest} bad_keys={bad_keys}",
                    )
            except Exception as e:
                check("WS /ws first frame", False, str(e))
    except Exception as e:
        check("TestClient startup", False, str(e))

    failed = [x for x in results if not x[1]]
    print("---")
    print(f"Smoke: {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
