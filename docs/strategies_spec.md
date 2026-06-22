# Strategies Spec (11 Tabs) — 4H Timeframe

ทุก Tab ทำงานบน 4H, margin 20 USDT × leverage 5x (notional = 100 USD), hedge mode.

---

## Tab 1 — EMA Pullback (110/190)

- **Timeframe:** 4H
- **Indicators:** EMA110 (fast), EMA190 (slow)
- **Trend:** `EMA110 > EMA190` = uptrend / `EMA110 < EMA190` = downtrend
- **Entry (Long):** pullback แตะ EMA110 แล้ว close > open (candle ยืนยัน) ในเทรนด์ขาขึ้น
- **Entry (Short):** rally แตะ EMA110 แล้ว close < open ในเทรนด์ขาลง
- **SL:** swing low/high ล่าสุด
- **TP:** RR ตามโครงสร้าง, cap โดย `MAX_SL_PCT = 8.5%`
- **max_open_positions:** 30

---

## Tab 2 — EMA6/EMA35 Crossover + ATR

- **Timeframe:** 4H
- **Indicators:** EMA6, EMA35, ATR(14)
- **Entry (Long):** EMA6 cross ขึ้นเหนือ EMA35, close > open
- **Entry (Short):** EMA6 cross ลงใต้ EMA35, close < open
- **SL:** `entry ± 2.1 × ATR`
- **TP:** `RR = 1.8` → `TP = entry ± 3.78 × ATR`
- **max_open_positions:** 30

---

## Tab 3 — SMC Order Block (EMA260)

- **Timeframe:** 4H
- **Trend filter:** EMA260
- **Structure:** Pivot left=3, right=3 → ระบุ swing high/low → หา BOS
- **Order Block lookback:** 7 แท่งก่อน breakout
- **Breakout margin:** 0.135%
- **Displacement filter:** breakout candle body ≥ `0.45 × ATR`
- **Entry:** ราคาย้อนกลับมาแตะ OB
- **SL:** OB opposite edge ± risk buffer 0.03%
- **RR:** 3.75
- **Signal expiry:** 4 แท่ง (invalidate ถ้าไม่ trigger)
- **max_open_positions:** 30

---

## Tab 4 — Premium/Discount OTE (EMA240)

- **Timeframe:** 4H
- **Trend filter:** EMA240
- **Structure:** Pivot left=3, right=3 → BOS
- **Breakout margin:** 0.125%
- **OTE zone:** Fibonacci 66%–72% retracement ของ leg ล่าสุด
- **Entry:** ราคาแตะโซน OTE (66–72%)
- **SL:** swing extreme ± risk buffer 0.03%
- **RR:** 3.75
- **Signal expiry:** 5 แท่ง
- **max_open_positions:** 30

---

## Tab 5 — RSI Divergence (H4 Balance)

- **Timeframe:** 4H
- **Indicators:** RSI(2), ATR(14)
- **Structure:** Pivot left=3, right=3 บน RSI และ price
- **Entry (Long):** Bullish divergence — price lower low, RSI higher low
- **Entry (Short):** Bearish divergence — price higher high, RSI lower high
- **SL:** `entry ± 1.7 × ATR`
- **TP:** `RR = 3.6` → `TP = entry ± 6.12 × ATR`
- **Max hold:** 48 แท่ง 4H (= 192 ชม.) แล้ว invalidate
- **max_open_positions:** 30

---

## Tab 6 — BB/KC Squeeze Breakout (Opt)

- **Timeframe:** 4H
- **Bollinger Bands:** length=30, std=1.3
- **Keltner Channel:** length=20, mult=1.6 (ใช้ `SMA(TR, 20) × 1.6`)
- **ATR:** `SMA(TR, 14)` สำหรับคำนวณ SL
- **Squeeze:** BB อยู่ใน KC (upper_bb < upper_kc AND lower_bb > lower_kc)
- **Entry (Long):** ออกจาก squeeze + close breakout เหนือ upper_bb, entry = close + 0.02%
- **Entry (Short):** ออกจาก squeeze + close breakout ใต้ lower_bb, entry = close − 0.02%
- **SL:** `entry ± 2.0 × ATR`, cap ที่ 7.5% ของ entry
- **TP:** `RR = 1.5`
- **max_open_positions:** **20** (portfolio-level cap)

---

## Tab 7 — CCI 125 (Opt)

- **Timeframe:** 4H
- **Indicators:** CCI(30), ATR(14)
- **Threshold:** ±125
- **Entry (Long):** CCI cross ขึ้นเหนือ −125 จากโซน oversold, close > open
- **Entry (Short):** CCI cross ลงใต้ +125 จากโซน overbought, close < open
- **SL:** `entry ± 2.1 × ATR`, cap ที่ 5% ของ entry (risk_cap)
- **TP:** `RR = 1.75`
- **max_open_positions:** **20** (portfolio-level cap)

---

## Tab 8 — Three Soldiers / Three Crows 4H Combo Stable Opt

- **Timeframe:** 4H
- **Type:** 🟢 Momentum (pattern + trend + breakout)
- **Indicators:** EMA200, ADX(14), ATR(14), Volume SMA(20)
- **Pattern:** Three White Soldiers (Long) / Three Black Crows (Short) บน bars i-2, i-1, i
  - 3 candles ทิศทางเดียวกัน
  - ทุกแท่ง `body_ratio >= 0.4`
  - closes ต่อเนื่อง (Long: HH; Short: LL)
- **Trend filter:** Long ต้อง `close > EMA200`, Short ต้อง `close < EMA200`
- **Quality filter:** `ADX14 >= 20` · `volume / SMA20 >= 1.0` · `ema_dist_pct >= 0%`
- **Breakout confirm:** Long `close > max(high[i-2], high[i-1]) × 1.001` / Short `close < min(low[i-2], low[i-1]) × 0.999`
- **Entry:** open แท่งถัดไป
- **Risk:** `risk = min(ATR14 × 2.5, entry × 7%)`
- **SL:** `entry ∓ risk`
- **TP:** `RR = 1.75` → `entry ± risk × 1.75`
- **Exit:** SL / TP เท่านั้นใน live bot (ถ้าชนกันในแท่งเดียว SL ก่อน); final-candle close เป็นกติกาเฉพาะ backtest
- **max_open_positions:** 30

---

## Tab 9 — PA Impulse Move Continuation 4H Best

- **Timeframe:** 4H
- **Type:** 🟢 Momentum / Continuation
- **Indicators:** EMA200, ATR(14)
- **Signal candle:** latest closed bar `i`; entry at open of next bar
- **Impulse filter:** net move from `close[i-3]` to `close[i]` must be at least `1.8 × ATR(14)`
- **Entry (Long):** `close[i-2] < close[i-1] < close[i]` and `close[i] > EMA200`
- **Entry (Short):** `close[i-2] > close[i-1] > close[i]` and `close[i] < EMA200`
- **Risk:** `risk = min(ATR14 × 1.75, entry × 7%)`
- **SL:** `entry ∓ risk`
- **TP:** `RR = 1.25` → `entry ± risk × 1.25`
- **Exit:** SL / TP เท่านั้น
- **max_open_positions:** 30

---

## Tab 10 — Volume Range Expansion Spike 4H Opt

- **Timeframe:** 4H
- **Type:** 🟢 Momentum / Expansion
- **Indicators:** EMA150, ATR(14), Volume SMA(20)
- **Signal candle:** latest closed bar; entry at open of next bar
- **Trend filter:** Long must `close > EMA150`; Short must `close < EMA150`
- **Expansion filter:** `high - low >= ATR14 × 0.9`
- **Volume filter:** `volume >= SMA20(volume) × 1.4`
- **Body strength:** `abs(close - open) / max(high - low, epsilon) >= 0.5`
- **Entry (Long):** bullish signal candle (`close > open`) passing all filters
- **Entry (Short):** bearish signal candle (`close < open`) passing all filters
- **Risk:** `risk = min(ATR14 × 1.5, entry × 7%)`
- **SL:** `entry ∓ risk`
- **TP:** `RR = 1.5` → `entry ± risk × 1.5`
- **Exit:** SL / TP เท่านั้น (ถ้าชนกันในแท่งเดียว SL ก่อน)
- **max_open_positions:** 30

---

## Tab 11 — Volume Pressure Proxy 4H Best

- **Timeframe:** 4H
- **Type:** 🟢 Momentum / Breakout
- **Indicators:** EMA200, ATR(14), volume pressure proxy
- **Signal candle:** latest closed bar; entry at open of next bar
- **Pressure window:** 8 candles
- **Buy-pressure proxy:** `up_vol_ratio = sum(volume of green candles) / sum(total volume)` over the pressure window
- **Breakout window:** previous 3 candles before the signal candle
- **Entry (Long):** `up_vol_ratio >= 0.55`, `close > highest_high(previous 3)`, and `close > EMA200`
- **Entry (Short):** `up_vol_ratio <= 0.42`, `close < lowest_low(previous 3)`, and `close < EMA200`
- **Risk:** `risk = min(ATR14 × 2.0, entry × 7%)`
- **SL:** `entry ∓ risk`
- **TP:** `RR = 1.25` → `entry ± risk × 1.25`
- **Exit:** SL / TP เท่านั้นใน live bot (ถ้าชนกันในแท่งเดียว SL ก่อน); final-candle close เป็นกติกาเฉพาะ backtest/simulator
- **max_open_positions:** 30

---

## Tab 17 — Momentum Vol Pressure 1H

- **Timeframe:** 1H
- **Type:** 🟢 Momentum / Breakout
- **Signal logic:** same as Tab11 (volume pressure proxy: up-vol ratio, 3-bar breakout, EMA200)
- **Momentum universe (server):** Top 500 by 24h quote volume → filter `|priceChangePercent| ≥ 2.5%` and last closed 1H volume ≥ `1.25 × SMA20(1H volume)` → rank by `momentum_score = |Δ%| × min(vol_ratio, 3.0)` → top 50 symbols
- **Priority scan:** candidates sorted by momentum score descending before entry batch
- **Risk:** `risk = min(ATR14 × 2.0, entry × 7%)`
- **TP:** `RR = 1.25`
- **max_open_positions:** 40 (fixed via `TAB17_MAX_POS`)
- **Exit:** SL / TP only

---

## Tab 18 — Volume Pressure Breakout 1H

- **Timeframe:** 1H
- **Type:** 🟢 Momentum / Breakout
- **Indicators:** EMA40, ATR(14), buy-pressure ratio
- **Signal candle:** latest closed bar; entry at open of next bar
- **Pressure window:** 8 candles
- **Buy-pressure ratio:** `up_vol_ratio = sum(volume of green candles) / sum(total volume)` over the pressure window
- **Breakout window:** previous 3 candles before the signal candle (exclude signal bar)
- **Entry (Long):** `up_vol_ratio >= 0.585`, `close > highest_high(previous 3)`, and `close > EMA40`
- **Entry (Short):** `up_vol_ratio <= 0.428`, `close < lowest_low(previous 3)`, and `close < EMA40`
- **Risk:** `risk = min(ATR14 × 1.8875, entry × 7%)`
- **SL:** `entry ∓ risk`
- **TP:** `RR = 0.85` → `entry ± risk × 0.85`
- **Exit:** SL / TP only in live bot
- **max_open_positions:** 30 (global default)

---

## Global Settings

| Setting | Value |
|---|---|
| Notional size | 100 USD (margin 20 × lev 5x) |
| Max SL cap (global hard cap @ server) | 8.5% — applied in `_execute_entry_unsafe` as final safety net |
| Per-strategy SL cap | Tab1–Tab5: 8.5% (global) · **Tab6 BB/KC: 7.5%** · **Tab7 CCI: 5.0%** · **Tab8 3S/3C: 7.0%** · **Tab9 PA Impulse: 7.0%** · **Tab10 Vol Spike: 7.0%** · **Tab11 Vol Pressure: 7.0%** · **Tab18 Vol Pressure BO: 7.0%** |
| Default max positions per tab | 30 (Tab6/Tab7 = 20) |
| Entry quality filter (LIVE) | \|funding\| ≤ 1%, spread ≤ 1% |
| Circuit breaker | daily loss ≥ 1000 USD |
| Low margin alert | available < 200 USDT |
| Schedule | ทุก 4H ที่นาทีที่ 1 (UTC: 00:01, 04:01, 08:01, …) |
