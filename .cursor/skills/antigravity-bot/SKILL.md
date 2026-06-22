---
name: antigravity-bot
description: >-
  Operates the Antigravity multi-strategy Binance USDT-M Futures bot (FastAPI
  server.py, 16 strategy tabs, hedge mode, paper_state.json). Use when working
  in this repo on trading logic, live orders, SL/TP, sync, dashboard, risk
  controls, WebSocket price feed, circuit breaker, or debugging Tab1–Tab16.
---

# Antigravity Trading Bot

Multi-strategy crypto futures bot: **Binance USDT-M Futures (hedge mode)**, entry `server.py` (port **8000**), signals in `strategies.py`, live REST in `binance_live.py`.

## First step

Read [context.md](../../../context.md) for project overview, then [.agents/AGENTS.md](../../../.agents/AGENTS.md) for architecture, tab map, invariants, and Change Log rules.

## Documentation checklist (same work session)

| Change type | Update |
|-------------|--------|
| Any code / config / dashboard / trading | **`.agents/AGENTS.md`** — Change Log entry (required) |
| Big picture (tab map, architecture, defaults, schema, invariants, main APIs) | **`context.md`** too |
| Small bug fix / minor UI / param tweak | `AGENTS.md` only — skip `context.md` |

Before finishing: confirm Change Log written; if big-picture → `context.md` synced. See `.cursor/rules/antigravity-docs.mdc`.

## Stack (quick)

| Path | Role |
|------|------|
| `server.py` | FastAPI, scheduler, state, sync, APIs |
| `strategies.py` | Pure signal evaluators per Tab |
| `binance_live.py` | Signed orders, algo SL/TP, positions |
| `config.py` | Env + tab constants (`TAB_TIMEFRAMES`, caps) |
| `static/index.html` | Dashboard |
| `paper_state.json` | Runtime state (do not commit) |
| `.env` | Secrets (do not commit) |

Ignored from index: `.env`, `.venv/`, `paper_state.json`, logs — use terminal/read tools if needed.

## Run & verify

```powershell
cd E:\antigravity
.\.venv\Scripts\python.exe server.py
```

```powershell
.\.venv\Scripts\python.exe -m py_compile server.py binance_live.py strategies.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py"
```

Dashboard: `http://localhost:8000`

- Use **`.venv`** only (not `venv/`).
- Single instance: `server.lock` + mutex; check port 8000 before debugging stale behavior.

## Critical invariants (never break)

### Hedge mode + `position_side`

- Every live order/close/cancel sends `positionSide` (`LONG` / `SHORT`).
- Reconcile at `(symbol, position_side)`, not symbol alone.
- State position key: `{symbol}_{tab}` (e.g. `BTCUSDT_Tab11`); multiple tabs can share the same hedge leg.
- `handle_order_update()` matches fills via `positionSide` + `sl_order_id` / `tp_order_id`.

### Setup keys vs position keys

- **`used_setups`:** `{sym}_{TabN}_{signal_ts}` — blocks re-entry on the same signal candle.
- **`open_positions`:** `{sym}_{TabN}` — one open position per symbol+tab.
- In `evaluate_candle_signals`, use `can_collect()` / `setup_key` pattern; do not use `{sym}_TabN` alone in `used_setups`.

### State & environment

- `save_state()` is atomic; `_state_write_guard_allows_save()` rejects abnormal shrinks.
- `ORDER_ENV` (orders) and `PRICE_FEED_ENV` (klines/WS) are independent — mismatch causes “wrong” prices vs exchange UI.
- WS price URL must use Binance `/market/stream` with `!miniTicker@arr` + `!markPrice@arr@1s` (see `PRICE_FEED_WS_URL` in `config.py`).
- Algo order limit ~100 — recovery must not spam SL/TP on `-4045`.

## Signal → entry flow

1. `scheduler_loop` → `evaluate_candle_signals(interval)` for `1h` / `4h`.
2. Per symbol: klines → tab evaluator in `strategies.py`.
3. Gates: tab enabled, no open `{sym}_{tab}`, under max positions, setup not in `used_setups`, `_entry_size_allowed()` (min notional/qty).
4. `execute_entry` → live: market entry + algo SL/TP via `binance_live`.

**Startup:** Tab18 enabled (`STARTUP_ENABLED_TABS` in `config.py`).

## Tab map (16 tabs)

| Tab | TF | Evaluator |
|-----|-----|-----------|
| Tab1 | 4h | `evaluate_tab1_ema4h` |
| Tab2 | 4h | `evaluate_tab2_ema_1h` |
| Tab3 | 4h | `evaluate_tab3_smc260` |
| Tab4 | 4h | `evaluate_tab4_ote` |
| Tab5 | 1h | `evaluate_tab5_rsi_divergence_1h` |
| Tab6 | 4h | `evaluate_tab6_squeeze_1h` |
| Tab7 | 4h | `evaluate_tab7_cci_1h` |
| Tab8 | 1h | `evaluate_tab8_three_soldiers_crows` |
| Tab9 | 1h | `evaluate_tab9_impulse_move_continuation` |
| Tab10 | 1h | `evaluate_tab10_vol_range_expansion_spike` |
| Tab11 | 1h | `evaluate_tab11_volume_pressure_proxy` |
| Tab12 | 1h | `evaluate_tab12_volume_spike_breakout` |
| Tab13–16 | 4h | 4h retags of Tab9–12 |

Params: `config.py`, narrative spec: `docs/strategies_spec.md`.

## Task routing

| Task | Where to work |
|------|----------------|
| New / change strategy Tab | Skill **antigravity-add-strategy** + `docs/strategies_spec.md` |
| Live order, SL/TP, naked position | `binance_live.py`, `sync_live_positions`, `scripts/fix_naked_positions.py` |
| Hedge ownership bugs | `tests/test_server_hedge.py` — run after changes |
| Dashboard UI | `static/index.html` — prefer `TAB_LIST` over hardcoded tab arrays |
| Risk: max positions / notional | `paper_state`, `_effective_max_positions`, `_effective_notional_size`, APIs `POST /api/max-positions`, `POST /api/notional-size` |
| Reset tab balance | `scripts/reset_tab.py`, `scripts/reset_tab1.py` |

## Key APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Dashboard |
| WS | `/ws` | Live updates |
| GET | `/api/health` | Health |
| POST | `/api/tab-enabled` | Toggle tab |
| POST | `/api/max-positions` | Cap per tab |
| POST | `/api/notional-size` | USD notional per trade |
| POST | `/api/close-all` | Close all |
| POST | `/api/close-all-long` | Close LONG legs only |
| POST | `/api/close-all-short` | Close SHORT legs only |

## Env flags (`.env`)

| Variable | Meaning |
|----------|---------|
| `LIVE_MODE` | `true` = real orders |
| `ORDER_ENV` | `testnet` \| `mainnet` |
| `PRICE_FEED_ENV` | `testnet` \| `mainnet` |
| `BINANCE_API_*` | Credentials |
| `CIRCUIT_BREAKER_DAILY_LOSS` | Daily loss limit (USD) |
| `DASHBOARD_PASSCODE` | Optional auth |

## Debugging checklist

1. One server on port 8000? `ORDER_ENV` / `PRICE_FEED_ENV` match the UI you compare?
2. Tab enabled in state / dashboard?
3. Logs: `[Filter] Skip` (min notional), `[Entry Gate]`, `WS stale`, `[Live Sync]`.
4. For silent tab: function name in `server.py` must match `strategies.evaluate_tabN_*`.
5. Hedge: run `tests/test_server_hedge.py` after order/sync edits.

## More detail

- Full handoff + changelog: [.agents/AGENTS.md](../../../.agents/AGENTS.md)
- Add Tab workflow: [../antigravity-add-strategy/SKILL.md](../antigravity-add-strategy/SKILL.md)
