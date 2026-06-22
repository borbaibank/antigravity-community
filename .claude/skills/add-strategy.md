---
name: add-strategy
description: Checklist for adding a new strategy Tab to the bot. Use when the user asks to add a new trading strategy, a new Tab, or references "Tab8/Tab9/etc". Walks through every file that must be updated so nothing is missed.
---

# Add New Strategy Tab — Complete Checklist

ทุกครั้งที่เพิ่ม Tab ใหม่ (เช่น Tab8) ต้องแก้ไฟล์ตามลำดับนี้ **ทั้งหมด** มิฉะนั้น bot จะทำงานไม่ครบ:

---

## 1. `config.py` — ค่าคงที่และ registry

- เพิ่มบล็อก constants ของ Tab ใหม่ (timeframe, indicator params, SL mult, RR, per-tab caps ถ้ามี):
  ```python
  # --- Tab 8: <Strategy Name> ---
  TAB8_<INDICATOR>_LEN = ...
  TAB8_ATR_LEN         = 14
  TAB8_SL_ATR_MULT     = 2.0
  TAB8_RR              = 1.5
  TAB8_TP_ATR_MULT     = TAB8_SL_ATR_MULT * TAB8_RR
  TAB8_SL_CAP_PCT      = 0.085   # optional per-strategy cap (ถ้าเข้มกว่า global)
  TAB8_MAX_POS         = 30      # optional portfolio cap (ถ้าต่างจาก MAX_POSITIONS_PER_TAB)
  ```
- **เพิ่มใน `TAB_TIMEFRAMES` dict** — ไม่งั้น `TABS` จะไม่มี key นี้ → ทุก loop ที่ iterate `TABS` จะข้าม

---

## 2. `strategies.py` — ตัวประเมินสัญญาณ

- เขียนฟังก์ชัน `evaluate_tab8_<name>(df) -> dict | None`:
  - import ค่า config มาจาก `config`
  - ใช้ `ep = float(df['open'].iloc[-1])` (entry = open แท่งถัดไป)
  - apply `MAX_SL_PCT` cap ใน strategy (defensive) ตัวอย่าง:
    ```python
    sl = max(sl, ep * (1 - MAX_SL_PCT))   # Long
    ```
  - คืน `{'side': 'Long'|'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'TabN_setup'}`
- **ชื่อฟังก์ชันต้องตรงกับที่ `server.py` เรียก** — ถ้าไม่ตรงจะ AttributeError เงียบ (เคยเกิดกับ Tab2 → Feature 31)
- ถ้าต้องการ invalidation (exit ก่อน SL/TP): เพิ่มเคสใน `check_invalidations(pos, df, tab_name)`

---

## 3. `docs/strategies_spec.md` — spec ของทั้งระบบ

- เพิ่ม section ของ Tab ใหม่พร้อม:
  - Timeframe, indicator, entry rule, SL/TP formula, RR, invalidation, max_positions
- อัปเดตตาราง **Per-strategy SL cap** ใน Global Settings ถ้า cap ไม่ใช่ 8.5%

---

## 4. `server.py` — integration ทุกจุด

4.1 **Import config** (บรรทัด ~21-30):
```python
from config import (
    ..., TAB8_MAX_POS,   # ถ้ามี dedicated cap
)
```

4.2 **Gate ใน `evaluate_candle_signals`** (ดู pattern ของ tab1-tab7 แถว `server.py:820+`):
```python
tab8_key = f"{sym}_Tab8_{signal_ts}"
if (_tab_on("Tab8")
        and f"{sym}_Tab8" not in state["open_positions"]
        and tab_counts["Tab8"] < MAX_POSITIONS_PER_TAB    # หรือ TAB8_MAX_POS
        and tab8_key not in state["used_setups"]):
    sig = strategies.evaluate_tab8_<name>(df.copy())
    if sig:
        await execute_entry(sym, sig, "Tab8")
        state["used_setups"].append(tab8_key)
        tab_counts["Tab8"] += 1
```

4.3 **`_execute_entry_unsafe` dedicated cap** (ถ้า Tab8 ต้องใช้ TAB8_MAX_POS):
```python
tab_cap = {"Tab6": TAB6_MAX_POS, "Tab7": TAB7_MAX_POS, "Tab8": TAB8_MAX_POS}.get(tab_name, MAX_POSITIONS_PER_TAB)
```

4.4 `MAX_SL_PCT` server-side hard cap จะ apply อัตโนมัติ (ใน `_execute_entry_unsafe`) — **ไม่ต้องแก้**

4.5 Sweep/Repair/Purge ทำงาน per-position ตาม algoId — **ไม่ต้องแก้** (รองรับ multi-tab อัตโนมัติ)

---

## 5. `static/index.html` — Dashboard UI

ต้องแก้ **5 จุด**:

5.1 **ปุ่ม filter** (~`static/index.html:522`):
```html
<button class="tab-btn" data-filter="Tab8" onclick="setFilter('Tab8')">🔷 Tab 8: <Name></button>
```

5.2 **`strategyDescriptions`** (~`:699`):
```js
'Tab8': '🔷 <strong><Name></strong> (Timeframe: <b>4H</b>) · <span style="color:#3fb950">🟢 Momentum</span><br/>...คำอธิบาย...<br/><i>*SL=..., TP=..., Max X ไม้</i>',
```
- เลือก badge ตามประเภท: 🟢 Momentum/Trend (#3fb950), 🔴 Reversal (#f85149), 🟡 Sideway (#d29922)

5.3 **`STRATEGY_LABELS`** (~`:885`):
```js
'Tab8': '<short label>',
```

5.4 **`TAB_LIST`** (~`:900`): เพิ่ม `'Tab8'` ใน array

5.5 **Loop arrays** (~`:1103, :1118, :1189`) — ทุก `['Tab1'...'Tab7']` ต้องเพิ่ม `'Tab8'`:
```js
for(let tab of ['Tab1','Tab2','Tab3','Tab4','Tab5','Tab6','Tab7','Tab8']) { ... }
let stratMap = {'Tab1':0, ..., 'Tab8':0};
```

**หาให้ครบ:** `grep "Tab1','Tab2','Tab3','Tab4','Tab5','Tab6','Tab7'" static/index.html`

---

## 6. On/Off switch — ไม่ต้องแก้

ทำงานอัตโนมัติผ่าน `state["tab_enabled"]` และ endpoint `POST /api/tab-enabled` เมื่อ:
- เพิ่ม Tab ใหม่ใน `TABS` (ผ่าน TAB_TIMEFRAMES)
- เพิ่ม `'Tab8'` ใน `TAB_LIST` ของ dashboard

---

## 7. `.agents/AGENTS.md` — บันทึก feature

- append entry ใหม่ท้ายไฟล์:
```markdown
### Feature N: เพิ่ม Tab8 <Strategy Name> (YYYY-MM-DD)

ไฟล์ที่แก้: config.py, strategies.py, server.py, static/index.html, docs/strategies_spec.md

**Strategy:** อธิบายสั้น ๆ (1-2 บรรทัด)
**Timeframe:** 4H
**SL/TP:** SL=... TP=...
**Max positions:** N
**Type:** Momentum / Reversal / Sideway
```

---

## 8. Verification checklist (ก่อน commit)

- [ ] `python -c "import server; print('OK')"` — syntax ok, import ครบ
- [ ] Bot restart แล้วเห็น log `[Tab8 LIVE ENTRY]` หรือ pending signal
- [ ] Dashboard refresh: filter Tab8 ใช้งานได้, description แสดงถูก, toggle on/off มีผล
- [ ] ทดสอบ signal หนึ่ง setup — ดูว่า SL/TP วางบน Binance, state บันทึก `Tab8` → ปิดได้ปกติ
- [ ] ทดลอง **open 2 Tab พร้อมกันบน symbol เดียว** (Tab X + Tab8) → ต้องมี SL/TP แยกกัน algoId คนละตัว (Feature 34 logic)

---

## 9. Gotchas ที่เคยเจอ

- **Function name mismatch** (Feature 31) — เรียก `evaluate_tab2_ema_cross` แต่ไฟล์มี `evaluate_tab2_ema_1h` → Tab2 ไม่ทำงานเงียบ ๆ
- **Tiny-price coins** (Feature 33) — ถ้า strategy trigger เหรียญราคา < 1e-4 ต้องเชื่อ `format_price` (มีอยู่แล้ว) ไม่ต้องแก้
- **Multi-tab shared hedge key** (Feature 34) — ในโหมด hedge, state key ต้องเป็น `{sym}_Tab{N}` (ไม่ใช่แค่ sym) เพื่อแยก tab. `_execute_entry` ทำให้อยู่แล้ว
- **TAB_TIMEFRAMES key** — ถ้าลืมเพิ่ม, `TABS = list(TAB_TIMEFRAMES.keys())` จะไม่มี Tab ใหม่ → balances, tab_counts, tab_enabled ขาด key นี้ → KeyError

### 9.1 ⚠️ CRITICAL — `used_setups` key ต้องมี `signal_ts`

Gate ของแต่ละ tab ต้องสร้าง key แบบ **`{sym}_Tab{N}_{signal_ts}`** (3 ส่วน) — ไม่ใช่ `{sym}_Tab{N}` (2 ส่วน)

**ทำไมสำคัญ:** `used_setups` เก็บ key ของ setup ที่เคยเข้าไปแล้ว เพื่อกัน re-enter ซ้ำบน candle เดียวกัน. ถ้า key ไม่มี timestamp → เข้า setup ครั้งแรก, โดน SL, **จะเข้าอีกครั้งไม่ได้อีกเลยตลอดชีวิตบอท** แม้ candle ใหม่ให้สัญญาณใหม่

**Pattern ที่ถูกต้อง** (ดู [server.py:849](/e:/antigravity/server.py)):
```python
signal_ts = int(df["timestamp"].iloc[-2].timestamp() * 1000)  # ms ของ signal candle
tab8_key = f"{sym}_Tab8_{signal_ts}"
if (... and tab8_key not in state["used_setups"]):
    sig = strategies.evaluate_tab8_xxx(df.copy())
    if sig:
        await execute_entry(sym, sig, "Tab8")
        state["used_setups"].append(tab8_key)
```

**Pattern ที่ผิด** (อย่าทำ):
```python
tab8_key = f"{sym}_Tab8"           # ❌ ไม่มี signal_ts
if (tab8_key not in state["used_setups"]):
```

**ตรวจสอบ:** ก่อน commit ใช้ `grep "_Tab{N}_{" server.py` — ถ้าไม่พบ → ผิด pattern

**หมายเหตุ:** `state["open_positions"]` ใช้ key แบบ `{sym}_Tab{N}` (ไม่มี timestamp) — นั่นคือ **position key** คนละเรื่องกับ **setup key**. อย่าสับสน:
- `open_positions["BTCUSDT_Tab8"]` — track ตำแหน่งที่เปิดอยู่ (1 pos ต่อ sym-tab)
- `used_setups[...]` มี `"BTCUSDT_Tab8_1777000000000"` — track ว่า candle 1777000000000 setup นี้ถูกใช้ไปแล้ว
