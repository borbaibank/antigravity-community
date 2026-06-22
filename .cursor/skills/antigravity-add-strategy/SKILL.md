---
name: antigravity-add-strategy
description: >-
  Checklist for adding or changing a strategy Tab on the Antigravity Binance
  futures bot. Use when adding Tab17+, a new evaluate_tabN function, updating
  TAB_TIMEFRAMES, or wiring signals in evaluate_candle_signals and the
  dashboard.
---

# Add / Change Strategy Tab

Complete checklist — skipping a step causes silent failures (missing tab in `TABS`, wrong function name, permanent `used_setups` lock).

Read [.agents/AGENTS.md](../../../.agents/AGENTS.md) and append a Change Log entry when done.

## 1. `config.py`

- Add constants block (`TABn_*`, SL/TP mults, optional `TABn_MAX_POS`, `TABn_SL_CAP_PCT`).
- Add entry to **`TAB_TIMEFRAMES`** — required or `TABS` omits the tab (KeyError / missing balances).

```python
TAB_TIMEFRAMES = {
    ...
    "Tab17": "4h",  # or "1h"
}
```

## 2. `strategies.py`

Implement `evaluate_tabN_<name>(df) -> dict | None`:

- Import params from `config`.
- Entry price: `ep = float(df['open'].iloc[-1])` (next-bar open).
- Apply `MAX_SL_PCT` defensively on SL distance.
- Return: `{'side': 'Long'|'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'TabN_...'}`.
- Optional invalidation: extend `check_invalidations(pos, df, tab_name)`.

**Function name must exactly match** the call in `server.py` (silent failure if mismatched — see Tab2 / `evaluate_tab2_ema_1h`).

## 3. `docs/strategies_spec.md`

- New section: timeframe, rules, SL/TP, RR, max positions, invalidation.
- Update global SL cap table if per-tab cap differs.

## 4. `server.py`

### 4.1 Import new constants from `config` if needed

### 4.2 Wire in `evaluate_candle_signals(interval)`

Follow the existing **`can_collect` / `collect_candidate`** pattern (do not hand-roll gates):

```python
if can_collect("Tab17", sym, signal_ts):
    collect_candidate("Tab17", sym, signal_ts, strategies.evaluate_tab17_xxx(df.copy()))
```

Place under the correct `if interval == "4h":` or `1h` branch.

`signal_ts` is already set as:

```python
signal_ts = int(df["timestamp"].iloc[-2])
```

Setup key becomes `f"{sym}_Tab17_{signal_ts}"` inside `can_collect` — **never** gate `used_setups` with `{sym}_Tab17` only (blocks forever after first SL).

### 4.3 Dedicated position cap (optional)

If tab uses its own max: extend `_tab_max_positions(tab_name)` / tab_cap dict in entry path (see Tab6/Tab7 pattern).

### 4.4 Do not change

- Server-side `MAX_SL_PCT` in `_execute_entry_unsafe` (automatic).
- Sweep/repair/purge (per-position algo ids).

## 5. `static/index.html`

1. Filter button (`data-filter="Tab17"`).
2. `strategyDescriptions['Tab17']`.
3. `STRATEGY_LABELS['Tab17']`.
4. Add `'Tab17'` to **`TAB_LIST`** (preferred — drives loops).
5. Grep for hardcoded tab arrays missing Tab17:  
   `grep -E "Tab1.*Tab16" static/index.html`

## 6. Tab on/off

No server change — `state["tab_enabled"]` + `POST /api/tab-enabled` once tab is in `TABS`.

## 7. Tests

- `python -m py_compile server.py strategies.py config.py`
- Add unit test in `tests/test_volume_strategies.py` or new file if logic is non-trivial.
- `python -m unittest discover -s tests -t .` if touching orders/position keys.

## 8. Verify on running bot

- [ ] Import: `python -c "import server; print('OK')"`
- [ ] Restart server; tab appears on dashboard; toggle works.
- [ ] Signal produces `[Tab17 LIVE ENTRY]` or paper entry log.
- [ ] SL/TP on exchange (testnet first); state has `BTCUSDT_Tab17` with `position_side`, `sl_order_id`, `tp_order_id`.
- [ ] Two tabs same symbol → separate algo SL/TP ids.

## Gotchas

| Issue | Prevention |
|-------|------------|
| Function name mismatch | Grep `evaluate_tabN` in `server.py` vs `strategies.py` |
| Missing `TAB_TIMEFRAMES` key | Tab absent from all `TABS` loops |
| Wrong `used_setups` key | Always `{sym}_{TabN}_{signal_ts}` via `can_collect` |
| Confused keys | Position: `{sym}_{TabN}`; setup: `{sym}_{TabN}_{signal_ts}` |
| Tiny-price symbols | Rely on `format_price` in live path |
| Multi-tab same hedge leg | State keys must include tab suffix |

## Retag pattern (Tab13–16)

To clone a 1h tab to 4h: new evaluator + `TAB_TIMEFRAMES` + wire under `4h` branch + dashboard label; keep logic DRY or duplicate with `_4h` suffix as existing tabs do.
