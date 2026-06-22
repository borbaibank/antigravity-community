import numpy as np
import pandas as pd
from datetime import datetime, timezone
from config import (
    MAX_SL_PCT,
    TAB1_EMA_FAST, TAB1_EMA_SLOW,
    TAB2_EMA_FAST, TAB2_EMA_SLOW, TAB2_ATR_LEN,
    TAB2_SL_ATR_MULT, TAB2_TP_ATR_MULT,
    SMC4_PIVOT_LEFT, SMC4_PIVOT_RIGHT, SMC4_TREND_EMA,
    SMC4_OB_LOOKBACK, SMC4_SIGNAL_EXPIRY, SMC4_BREAKOUT_MARGIN,
    SMC4_RISK_BUFFER, SMC4_RISK_REWARD, SMC4_BREAKOUT_DISPLACEMENT_ATR_MULT,
    SMC5_PIVOT_LEFT, SMC5_PIVOT_RIGHT, SMC5_TREND_EMA,
    SMC5_SIGNAL_EXPIRY, SMC5_BREAKOUT_MARGIN,
    SMC5_RISK_BUFFER, SMC5_RISK_REWARD,
    SMC5_OTE_LOW, SMC5_OTE_HIGH,
    TAB5_RSI_LEN, TAB5_PIVOT_LEFT, TAB5_PIVOT_RIGHT,
    TAB5_ATR_LEN, TAB5_SL_ATR_MULT, TAB5_TP_ATR_MULT, TAB5_MAX_HOLD_BARS,
    TAB6_BB_LEN, TAB6_BB_STD, TAB6_KC_LEN, TAB6_KC_MULT,
    TAB6_ATR_LEN, TAB6_SL_ATR_MULT, TAB6_SL_CAP_PCT, TAB6_RR,
    TAB7_CCI_LEN, TAB7_CCI_THRESHOLD, TAB7_ATR_LEN,
    TAB7_SL_ATR_MULT, TAB7_RISK_CAP_PCT, TAB7_RR,
    TAB8_EMA_LEN, TAB8_ADX_LEN, TAB8_ADX_MIN,
    TAB8_VOLUME_SMA_LEN, TAB8_VOLUME_RATIO_MIN, TAB8_EMA_DIST_MIN_PCT,
    TAB8_BODY_MIN_RATIO, TAB8_BREAKOUT_BUFFER,
    TAB8_ATR_LEN, TAB8_SL_ATR_MULT, TAB8_RISK_CAP_PCT, TAB8_RR,
    TAB9_EMA_LEN, TAB9_ATR_LEN, TAB9_NET_ATR_MULT,
    TAB9_SL_ATR_MULT, TAB9_RISK_CAP_PCT, TAB9_RR,
    TAB10_EMA_LEN, TAB10_ATR_LEN, TAB10_VOLUME_SMA_LEN,
    TAB10_VOLUME_MULT, TAB10_RANGE_ATR_MULT, TAB10_BODY_MIN_RATIO,
    TAB10_SL_ATR_MULT, TAB10_RISK_CAP_PCT, TAB10_RR,
)

# ----- TAB 2: EMA6/EMA35 CROSSOVER + ATR SL/TP (runs on 4H in production) -----
def _calc_atr(df, length, min_periods=None):
    """ATR using the rolling mean of true range, matching our backtests."""
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(length, min_periods=min_periods or length).mean()

def evaluate_tab2_ema_1h(df):
    """Tab 2: EMA6/EMA35 Crossover + ATR SL/TP.

    Note: Function name is legacy; production scheduler runs this on 4H candles.

    Signal: EMA6 crosses above/below EMA35 on the latest closed candle.
    Entry at open of the next candle.
    SL = 2.1×ATR(14), TP = 3.78×ATR(14)  →  RR 1:1.8
    """
    min_bars = TAB2_EMA_SLOW + TAB2_ATR_LEN + 5
    if len(df) < min_bars:
        return None

    df = df.copy()
    df['_ema_fast'] = df['close'].ewm(span=TAB2_EMA_FAST, adjust=False).mean()
    df['_ema_slow'] = df['close'].ewm(span=TAB2_EMA_SLOW, adjust=False).mean()
    df['_atr']      = _calc_atr(df, TAB2_ATR_LEN)

    # Use last two closed candles; entry = open of the next (unfinished) bar
    prev = df.iloc[-3]   # candle before signal candle
    last = df.iloc[-2]   # signal candle (latest closed)
    ep   = float(df['open'].iloc[-1])

    atr = float(last['_atr'])
    if not np.isfinite(atr) or atr <= 0:
        return None

    long_cross  = (prev['_ema_fast'] <= prev['_ema_slow']) and (last['_ema_fast'] > last['_ema_slow'])
    short_cross = (prev['_ema_fast'] >= prev['_ema_slow']) and (last['_ema_fast'] < last['_ema_slow'])

    if long_cross:
        sl = ep - TAB2_SL_ATR_MULT * atr
        sl = max(sl, ep * (1 - MAX_SL_PCT))
        tp = ep + (ep - sl) * (TAB2_TP_ATR_MULT / TAB2_SL_ATR_MULT)
        if sl >= ep:
            return None
        return {'side': 'Long',  'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab2_EMAcross'}

    if short_cross:
        sl = ep + TAB2_SL_ATR_MULT * atr
        sl = min(sl, ep * (1 + MAX_SL_PCT))
        tp = ep - (sl - ep) * (TAB2_TP_ATR_MULT / TAB2_SL_ATR_MULT)
        if sl <= ep:
            return None
        return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab2_EMAcross'}

    return None

# ----- EVALUATE ALL CONFIGURABLE TABS -----
def evaluate_tab1_ema_pullback_1h(df):
    """Tab 1: EMA Pullback {TAB1_EMA_FAST}/{TAB1_EMA_SLOW} (1H).

    Entry: signal candle touches fast EMA zone (pullback_buffer=0.3%), closes on correct side,
           confirms candle direction. Entry at open of next bar.
    SL: structure_stop = min/max(signal_candle, fast EMA ± ema_break_buffer=0.2%)
        capped at MAX_SL_PCT=8.5%.
    TP: RR 1:4.0
    Exit: close breaks fast EMA by ema_break_buffer=0.2%
    """
    min_bars = TAB1_EMA_SLOW + 5
    if len(df) < min_bars: return None
    df[f'EMA_FAST'] = df['close'].ewm(span=TAB1_EMA_FAST, adjust=False).mean()
    df[f'EMA_SLOW'] = df['close'].ewm(span=TAB1_EMA_SLOW, adjust=False).mean()

    row = df.iloc[-2]  # latest closed signal candle
    ep  = float(df['open'].iloc[-1])

    pullback_buffer  = 0.003
    ema_break_buffer = 0.002

    ema_fast = float(row['EMA_FAST'])
    ema_slow = float(row['EMA_SLOW'])

    if ema_fast > ema_slow:
        # LONG: low touches fast EMA zone, close >= fast EMA, green candle
        if (row['low'] <= ema_fast * (1 + pullback_buffer)
                and row['close'] >= ema_fast
                and row['close'] > row['open']):
            structure_stop = min(float(row['low']), ema_fast * (1 - ema_break_buffer))
            capped_stop    = ep * (1 - MAX_SL_PCT)
            sl = max(structure_stop, capped_stop)
            if sl >= ep: return None
            return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': ep + (ep - sl) * 4.0,
                    'reason': 'Tab1_Setup'}
    else:
        # SHORT: high touches fast EMA zone, close <= fast EMA, red candle
        if (row['high'] >= ema_fast * (1 - pullback_buffer)
                and row['close'] <= ema_fast
                and row['close'] < row['open']):
            structure_stop = max(float(row['high']), ema_fast * (1 + ema_break_buffer))
            capped_stop    = ep * (1 + MAX_SL_PCT)
            sl = min(structure_stop, capped_stop)
            if sl <= ep: return None
            return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': ep - (sl - ep) * 4.0, 'reason': 'Tab1_Setup'}
    return None


# ----- TAB 4 / TAB 5: SMC HELPERS -----
def _find_pivots(highs, lows, left, right):
    """Return confirmed pivot highs/lows. Ties are allowed, matching backtests."""
    n = len(highs)
    pivot_highs = []
    pivot_lows = []
    # A pivot at position i is confirmed once i+right bars exist
    for i in range(left, n - right):
        win_h = highs[i - left : i + right + 1]
        win_l = lows[i - left : i + right + 1]
        if highs[i] == np.max(win_h):
            pivot_highs.append((i, highs[i]))
        if lows[i] == np.min(win_l):
            pivot_lows.append((i, lows[i]))
    return pivot_highs, pivot_lows


def evaluate_tab3_smc_ob_1h(df):
    """Tab 3: SMC Order Block EMA{SMC4_TREND_EMA} (1H).

    Two-phase stateless scan:
      Phase 1 – scan last SIGNAL_EXPIRY candles for a BOS:
                 close > last_swing_high * (1 + breakout_margin)
                 AND close > last_swing_high + ATR * displacement_mult  (strength filter)
                 AND close > EMA{SMC4_TREND_EMA}
      Phase 2 – latest closed candle (cidx) retests OB zone and confirms direction.
    Entry at open of next bar.
    """
    min_bars = SMC4_TREND_EMA + SMC4_PIVOT_RIGHT + SMC4_SIGNAL_EXPIRY + 10
    if len(df) < min_bars:
        return None

    df['_ema'] = df['close'].ewm(span=SMC4_TREND_EMA, adjust=False).mean()
    # SMC OB optimized backtest used a rolling TR mean with early values allowed.
    df['_atr'] = _calc_atr(df, 14, min_periods=1)

    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    opens  = df['open'].values
    ema    = df['_ema'].values
    atr    = df['_atr'].values

    pivot_highs, pivot_lows = _find_pivots(highs, lows, SMC4_PIVOT_LEFT, SMC4_PIVOT_RIGHT)

    cidx = len(df) - 2   # latest closed candle
    ep   = opens[-1]     # entry = open of next bar

    # --- LONG ---
    # Phase 2: confirm candle must be green (spec: close > open only, no EMA required)
    if closes[cidx] > opens[cidx] and pivot_highs:
        for k in range(1, SMC4_SIGNAL_EXPIRY + 1):
            bo_idx = cidx - k
            if bo_idx < 0:
                break

            valid_phs = [(i, p) for i, p in pivot_highs if i + SMC4_PIVOT_RIGHT <= bo_idx]
            valid_pls = [(i, p) for i, p in pivot_lows if i + SMC4_PIVOT_RIGHT <= bo_idx]
            if not valid_phs or not valid_pls:
                continue
            _, last_sh = valid_phs[-1]
            _, last_sl = valid_pls[-1]

            # Phase 1: BOS margin + EMA trend + bar_range >= ATR * displacement_mult
            if closes[bo_idx] <= last_sh * (1 + SMC4_BREAKOUT_MARGIN):
                continue
            if closes[bo_idx] <= ema[bo_idx]:
                continue
            bar_range_bo = highs[bo_idx] - lows[bo_idx]
            if bar_range_bo < atr[bo_idx] * SMC4_BREAKOUT_DISPLACEMENT_ATR_MULT:
                continue

            # OB = last bearish candle within ob_lookback bars before bo_idx
            ob_low = ob_high = None
            for j in range(bo_idx - 1, max(-1, bo_idx - SMC4_OB_LOOKBACK - 1), -1):
                if closes[j] < opens[j]:
                    ob_low, ob_high = lows[j], highs[j]
                    break
            if ob_low is None:
                continue

            # Phase 2: cidx touches OB zone
            if lows[cidx] <= ob_high and highs[cidx] >= ob_low:
                sl = ob_low * (1 - SMC4_RISK_BUFFER)
                sl = max(sl, ep * (1 - MAX_SL_PCT))
                if sl >= ep:
                    continue
                risk = ep - sl
                tp = ep + risk * SMC4_RISK_REWARD
                return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': tp,
                        'exit_swing_low': last_sl, 'exit_swing_high': last_sh,
                        'reason': 'Tab3_SMC260_OB'}

    # --- SHORT ---
    # Phase 2: confirm candle must be red (spec: close < open only, no EMA required)
    if closes[cidx] < opens[cidx] and pivot_lows:
        for k in range(1, SMC4_SIGNAL_EXPIRY + 1):
            bo_idx = cidx - k
            if bo_idx < 0:
                break

            valid_pls = [(i, p) for i, p in pivot_lows if i + SMC4_PIVOT_RIGHT <= bo_idx]
            valid_phs = [(i, p) for i, p in pivot_highs if i + SMC4_PIVOT_RIGHT <= bo_idx]
            if not valid_pls or not valid_phs:
                continue
            _, last_sl = valid_pls[-1]
            _, last_sh = valid_phs[-1]

            # Phase 1: BOS margin + EMA trend + bar_range >= ATR * displacement_mult
            if closes[bo_idx] >= last_sl * (1 - SMC4_BREAKOUT_MARGIN):
                continue
            if closes[bo_idx] >= ema[bo_idx]:
                continue
            bar_range_bo = highs[bo_idx] - lows[bo_idx]
            if bar_range_bo < atr[bo_idx] * SMC4_BREAKOUT_DISPLACEMENT_ATR_MULT:
                continue

            # OB = last bullish candle within ob_lookback bars before bo_idx
            ob_low = ob_high = None
            for j in range(bo_idx - 1, max(-1, bo_idx - SMC4_OB_LOOKBACK - 1), -1):
                if closes[j] > opens[j]:
                    ob_low, ob_high = lows[j], highs[j]
                    break
            if ob_low is None:
                continue

            # Phase 2: cidx touches OB zone
            if lows[cidx] <= ob_high and highs[cidx] >= ob_low:
                sl = ob_high * (1 + SMC4_RISK_BUFFER)
                sl = min(sl, ep * (1 + MAX_SL_PCT))
                if sl <= ep:
                    continue
                risk = sl - ep
                tp = ep - risk * SMC4_RISK_REWARD
                return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp,
                        'exit_swing_low': last_sl, 'exit_swing_high': last_sh,
                        'reason': 'Tab3_SMC260_OB'}

    return None


def evaluate_tab4_ote_1h(df):
    """Tab 4: Premium/Discount OTE EMA{SMC5_TREND_EMA} (1H).

    Two-phase system:
      Phase 1 – scan last SIGNAL_EXPIRY candles for a BOS (break of swing high/low
                 with margin) while above/below EMA{SMC5_TREND_EMA}.
      Phase 2 – latest closed candle (cidx) retraces into the OTE zone ({SMC5_OTE_LOW*100:.0f}–{SMC5_OTE_HIGH*100:.0f}% fib)
                 and prints a confirmation candle.
    Entry at open of next bar (cidx+1 = df.iloc[-1]).
    CHoCH invalidation mirrors Tab3.
    """
    min_bars = SMC5_TREND_EMA + SMC5_PIVOT_RIGHT + SMC5_SIGNAL_EXPIRY + 10
    if len(df) < min_bars:
        return None

    df = df.copy()
    df['_ema'] = df['close'].ewm(span=SMC5_TREND_EMA, adjust=False).mean()

    highs  = df['high'].values
    lows   = df['low'].values
    closes = df['close'].values
    opens  = df['open'].values
    ema    = df['_ema'].values

    # Non-strict pivots (ties allowed — matches backtest add_indicators)
    pivot_highs, pivot_lows = [], []
    for i in range(SMC5_PIVOT_LEFT, len(df) - SMC5_PIVOT_RIGHT):
        win_h = highs[i - SMC5_PIVOT_LEFT : i + SMC5_PIVOT_RIGHT + 1]
        win_l = lows[i  - SMC5_PIVOT_LEFT : i + SMC5_PIVOT_RIGHT + 1]
        if highs[i] == win_h.max():
            pivot_highs.append((i, highs[i]))
        if lows[i] == win_l.min():
            pivot_lows.append((i, lows[i]))

    cidx = len(df) - 2   # latest closed candle
    ep   = opens[-1]     # entry = open of next bar

    # --- LONG ---
    # Phase 2: cidx must be bullish. EMA is required at the break candle only.
    if closes[cidx] > opens[cidx] and pivot_highs and pivot_lows:
        for k in range(1, SMC5_SIGNAL_EXPIRY + 1):
            bo_idx = cidx - k
            if bo_idx < 0:
                break

            valid_phs = [(i, p) for i, p in pivot_highs if i + SMC5_PIVOT_RIGHT <= bo_idx]
            valid_pls = [(i, p) for i, p in pivot_lows  if i + SMC5_PIVOT_RIGHT <= bo_idx]
            if not valid_phs or not valid_pls:
                continue

            _, last_sh = valid_phs[-1]
            _, last_sl = valid_pls[-1]

            if closes[bo_idx] <= last_sh * (1 + SMC5_BREAKOUT_MARGIN):
                continue
            if closes[bo_idx] <= ema[bo_idx]:
                continue

            # OTE zone: 67–73% retracement of the swing (swing_low → swing_high)
            swing_high = max(float(highs[bo_idx]), last_sh)
            swing_low  = last_sl
            swing_range = swing_high - swing_low
            if swing_range <= 0:
                continue

            zone_high = swing_high - swing_range * SMC5_OTE_LOW   # 67% retrace
            zone_low  = swing_high - swing_range * SMC5_OTE_HIGH  # 73% retrace

            # Phase 2: cidx touches zone
            if lows[cidx] <= zone_high and highs[cidx] >= zone_low:
                sl = swing_low * (1 - SMC5_RISK_BUFFER)
                sl = max(sl, ep * (1 - MAX_SL_PCT))
                if sl >= ep:
                    continue
                risk = ep - sl
                tp = ep + risk * SMC5_RISK_REWARD
                return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': tp,
                        'reason': 'Tab4_OTE_Long',
                        'exit_swing_low': swing_low, 'exit_swing_high': swing_high}

    # --- SHORT ---
    # Phase 2: cidx must be bearish. EMA is required at the break candle only.
    if closes[cidx] < opens[cidx] and pivot_highs and pivot_lows:
        for k in range(1, SMC5_SIGNAL_EXPIRY + 1):
            bo_idx = cidx - k
            if bo_idx < 0:
                break

            valid_phs = [(i, p) for i, p in pivot_highs if i + SMC5_PIVOT_RIGHT <= bo_idx]
            valid_pls = [(i, p) for i, p in pivot_lows  if i + SMC5_PIVOT_RIGHT <= bo_idx]
            if not valid_phs or not valid_pls:
                continue

            _, last_sh = valid_phs[-1]
            _, last_sl = valid_pls[-1]

            if closes[bo_idx] >= last_sl * (1 - SMC5_BREAKOUT_MARGIN):
                continue
            if closes[bo_idx] >= ema[bo_idx]:
                continue

            # OTE zone: 67–73% retracement of the swing (swing_high → swing_low)
            swing_low  = min(float(lows[bo_idx]), last_sl)
            swing_high = last_sh
            swing_range = swing_high - swing_low
            if swing_range <= 0:
                continue

            zone_low  = swing_low + swing_range * SMC5_OTE_LOW   # 67% retrace
            zone_high = swing_low + swing_range * SMC5_OTE_HIGH  # 73% retrace

            # Phase 2: cidx touches zone
            if lows[cidx] <= zone_high and highs[cidx] >= zone_low:
                sl = swing_high * (1 + SMC5_RISK_BUFFER)
                sl = min(sl, ep * (1 + MAX_SL_PCT))
                if sl <= ep:
                    continue
                risk = sl - ep
                tp = ep - risk * SMC5_RISK_REWARD
                return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp,
                        'reason': 'Tab4_OTE_Short',
                        'exit_swing_low': swing_low, 'exit_swing_high': swing_high}

    return None


def _calc_rsi(closes, length):
    """RSI using Wilder's smoothing (EWM alpha=1/length)."""
    delta = np.diff(closes.astype(float), prepend=closes[0])
    gains  = np.where(delta > 0,  delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gains).ewm(alpha=1 / length, adjust=False).mean().values
    avg_loss = pd.Series(losses).ewm(alpha=1 / length, adjust=False).mean().values
    with np.errstate(divide='ignore', invalid='ignore'):
        rs = np.where(avg_loss == 0, np.inf, avg_gain / avg_loss)
    return np.where(np.isinf(rs), 100.0, 100.0 - 100.0 / (1.0 + rs))


def evaluate_tab5_rsi_divergence_1h(df):
    """Tab 5: RSI Divergence.

    Note: Function name is legacy; production scheduler runs this on 4H candles.

    RSI(2) with pivot left=3, right=3. Ties are allowed, matching backtests.
    Bullish divergence  – lower pivot-low price + higher pivot-low RSI → LONG
    Bearish divergence  – higher pivot-high price + lower pivot-high RSI → SHORT

    Pivot confirmation: 3 candles after the pivot candle must have closed.
    Signal fires when the confirmation bar is the latest closed candle (cidx).
    Entry at open of the next candle (opens[-1]).
    SL = entry ± 1.7×ATR(14), TP = entry ∓ 6.12×ATR(14).
    """
    min_bars = TAB5_PIVOT_LEFT + TAB5_PIVOT_RIGHT + TAB5_RSI_LEN + 20
    if len(df) < min_bars:
        return None

    highs  = df['high'].values.astype(float)
    lows   = df['low'].values.astype(float)
    closes = df['close'].values.astype(float)
    opens  = df['open'].values.astype(float)

    rsi = _calc_rsi(closes, TAB5_RSI_LEN)
    atr = _calc_atr(df, TAB5_ATR_LEN).values

    pivot_highs, pivot_lows = _find_pivots(highs, lows, TAB5_PIVOT_LEFT, TAB5_PIVOT_RIGHT)

    cidx = len(df) - 2      # latest closed candle index
    ep   = float(opens[-1]) # entry price = open of next bar

    # Only pivot indices whose right-side confirmation bar == cidx (no lookahead)
    max_pivot_idx = cidx - TAB5_PIVOT_RIGHT

    # --- BULLISH DIVERGENCE → LONG ---
    valid_pls = [(i, p) for i, p in pivot_lows if i <= max_pivot_idx]
    if len(valid_pls) >= 2:
        (i2, p2) = valid_pls[-1]   # most recent confirmed pivot low
        (i1, p1) = valid_pls[-2]   # previous confirmed pivot low
        # Signal fires exactly when the right-side confirmation bar closes at cidx
        if i2 + TAB5_PIVOT_RIGHT == cidx:
            rsi2, rsi1 = rsi[i2], rsi[i1]
            if p2 < p1 and rsi2 > rsi1:               # lower price, higher RSI
                atr_val = float(atr[cidx])
                if np.isfinite(atr_val) and atr_val > 0:
                    sl = ep - TAB5_SL_ATR_MULT * atr_val
                    sl = max(sl, ep * (1 - MAX_SL_PCT))
                    tp = ep + (ep - sl) * (TAB5_TP_ATR_MULT / TAB5_SL_ATR_MULT)
                    if sl < ep:
                        return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': tp,
                                'reason': 'Tab5_RSIDivLong'}

    # --- BEARISH DIVERGENCE → SHORT ---
    valid_phs = [(i, p) for i, p in pivot_highs if i <= max_pivot_idx]
    if len(valid_phs) >= 2:
        (i2, p2) = valid_phs[-1]
        (i1, p1) = valid_phs[-2]
        if i2 + TAB5_PIVOT_RIGHT == cidx:
            rsi2, rsi1 = rsi[i2], rsi[i1]
            if p2 > p1 and rsi2 < rsi1:               # higher price, lower RSI
                atr_val = float(atr[cidx])
                if np.isfinite(atr_val) and atr_val > 0:
                    sl = ep + TAB5_SL_ATR_MULT * atr_val
                    sl = min(sl, ep * (1 + MAX_SL_PCT))
                    tp = ep - (sl - ep) * (TAB5_TP_ATR_MULT / TAB5_SL_ATR_MULT)
                    if sl > ep:
                        return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp,
                                'reason': 'Tab5_RSIDivShort'}

    return None


def _tab6_indicators(df):
    """Shared indicator calculation for Tab6. Returns (bb_upper, bb_lower, kc_upper, kc_lower, atr14, squeeze)."""
    closes  = df['close'].values.astype(float)
    s_close = pd.Series(closes)

    # Bollinger Bands: SMA(30) ± stdev(30) × 1.3
    basis    = s_close.rolling(TAB6_BB_LEN).mean().values
    std30    = s_close.rolling(TAB6_BB_LEN).std(ddof=0).values
    bb_upper = basis + TAB6_BB_STD * std30
    bb_lower = basis - TAB6_BB_STD * std30

    # Keltner Channel: EMA(20) ± SMA(TR, 20) × 1.6
    kc_mid   = s_close.ewm(span=TAB6_KC_LEN, adjust=False).mean().values
    kc_range = _calc_atr(df, TAB6_KC_LEN).values   # SMA(TR, 20)
    kc_upper = kc_mid + TAB6_KC_MULT * kc_range
    kc_lower = kc_mid - TAB6_KC_MULT * kc_range

    # ATR14 = SMA(TR, 14) — for SL sizing
    atr14   = _calc_atr(df, TAB6_ATR_LEN).values

    # Squeeze: BB fully inside KC
    squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)

    return bb_upper, bb_lower, kc_upper, kc_lower, atr14, squeeze


def evaluate_tab6_squeeze_1h(df):
    """BB/KC Squeeze Breakout.
    Note: Function name is legacy; production scheduler runs this on 4H candles.
    BB(30, 1.3) inside KC(EMA20 ± SMA_TR20×1.6) → squeeze.
    Signal: squeeze[i-1]=True AND close[i] breaks KC (transition only).
    Entry at open[i+1] ± 0.02%. SL = min(2.0×ATR14, 7.5%), TP = SL×1.5.
    """
    if len(df) < TAB6_BB_LEN + 5:
        return None

    _, _, kc_upper, kc_lower, atr14, squeeze = _tab6_indicators(df)
    closes = df['close'].values.astype(float)

    cidx = len(df) - 2      # latest confirmed closed bar
    if cidx < 2:
        return None

    long_cond_curr  = bool(squeeze[cidx - 1]) and closes[cidx] > kc_upper[cidx]
    long_cond_prev  = bool(squeeze[cidx - 2]) and closes[cidx - 1] > kc_upper[cidx - 1]
    short_cond_curr = bool(squeeze[cidx - 1]) and closes[cidx] < kc_lower[cidx]
    short_cond_prev = bool(squeeze[cidx - 2]) and closes[cidx - 1] < kc_lower[cidx - 1]

    long_signal  = long_cond_curr  and not long_cond_prev
    short_signal = short_cond_curr and not short_cond_prev

    if not long_signal and not short_signal:
        return None

    atr_val = float(atr14[cidx])
    ep_raw  = float(df['open'].iloc[-1])

    if long_signal:
        entry      = ep_raw * 1.0002
        final_risk = min(atr_val * TAB6_SL_ATR_MULT, entry * TAB6_SL_CAP_PCT)
        sl = entry - final_risk
        tp = entry + final_risk * TAB6_RR
        if sl >= entry:
            return None
        return {'side': 'Long',  'ep': entry, 'sl': sl, 'tp': tp, 'reason': 'Tab6_Squeeze'}
    else:
        entry      = ep_raw * 0.9998
        final_risk = min(atr_val * TAB6_SL_ATR_MULT, entry * TAB6_SL_CAP_PCT)
        sl = entry + final_risk
        tp = entry - final_risk * TAB6_RR
        if sl <= entry:
            return None
        return {'side': 'Short', 'ep': entry, 'sl': sl, 'tp': tp, 'reason': 'Tab6_Squeeze'}


def _calc_tab6_signals(df):
    """Returns (long_signal, short_signal) for current cidx. Used by invalidation check."""
    if len(df) < TAB6_BB_LEN + 5:
        return False, False
    _, _, kc_upper, kc_lower, _, squeeze = _tab6_indicators(df)
    closes = df['close'].values.astype(float)
    cidx = len(df) - 2
    if cidx < 2:
        return False, False
    long_cond_curr  = bool(squeeze[cidx - 1]) and closes[cidx] > kc_upper[cidx]
    long_cond_prev  = bool(squeeze[cidx - 2]) and closes[cidx - 1] > kc_upper[cidx - 1]
    short_cond_curr = bool(squeeze[cidx - 1]) and closes[cidx] < kc_lower[cidx]
    short_cond_prev = bool(squeeze[cidx - 2]) and closes[cidx - 1] < kc_lower[cidx - 1]
    return (long_cond_curr and not long_cond_prev), (short_cond_curr and not short_cond_prev)


def check_invalidations(df, pos, tab_name):
    """Returns True if the position should be invalidated based on its Tab rules."""
    row = df.iloc[-2]  # latest closed candle

    if tab_name == "Tab1":
        df['EMA_FAST'] = df['close'].ewm(span=TAB1_EMA_FAST, adjust=False).mean()
        ema_fast = float(df['EMA_FAST'].iloc[-2])
        ema_break_buffer = 0.002
        if pos['side'] == 'Long'  and row['close'] < ema_fast * (1 - ema_break_buffer): return True
        if pos['side'] == 'Short' and row['close'] > ema_fast * (1 + ema_break_buffer): return True

    elif tab_name == "Tab3":
        # CHoCH uses the swing level captured at entry, matching the backtest.
        close_val = float(df['close'].iloc[-2])
        if pos['side'] == 'Long' and pos.get('exit_swing_low') is not None:
            if close_val < float(pos['exit_swing_low']): return True
        if pos['side'] == 'Short' and pos.get('exit_swing_high') is not None:
            if close_val > float(pos['exit_swing_high']): return True

    elif tab_name == "Tab4":
        # CHoCH uses the swing level captured at entry, matching the backtest.
        close_val = float(df['close'].iloc[-2])
        if pos['side'] == 'Long' and pos.get('exit_swing_low') is not None:
            if close_val < float(pos['exit_swing_low']) * (1 - SMC5_BREAKOUT_MARGIN): return True
        if pos['side'] == 'Short' and pos.get('exit_swing_high') is not None:
            if close_val > float(pos['exit_swing_high']) * (1 + SMC5_BREAKOUT_MARGIN): return True

    elif tab_name == "Tab6":
        # Opposite squeeze signal → exit at next open
        long_sig, short_sig = _calc_tab6_signals(df)
        if pos['side'] == 'Long'  and short_sig: return True
        if pos['side'] == 'Short' and long_sig:  return True

    elif tab_name == "Tab7":
        # Opposite CCI 125 crossover → exit at next open
        long_sig, short_sig = _calc_tab7_signals(df)
        if pos['side'] == 'Long'  and short_sig: return True
        if pos['side'] == 'Short' and long_sig:  return True

    elif tab_name == "Tab5":
        # Max hold: TAB5_MAX_HOLD_BARS bars x 1H = 48 hours.
        # Use UTC-aware datetimes to avoid timezone-offset errors on non-UTC servers
        entry_time_raw = datetime.fromisoformat(pos['entry_time'])
        if entry_time_raw.tzinfo is None:
            entry_time_raw = entry_time_raw.replace(tzinfo=timezone.utc)
        elapsed_hours = (datetime.now(timezone.utc) - entry_time_raw).total_seconds() / 3600
        if elapsed_hours >= TAB5_MAX_HOLD_BARS:
            return True

        # Opposite RSI divergence → exit
        highs  = df['high'].values.astype(float)
        lows   = df['low'].values.astype(float)
        closes = df['close'].values.astype(float)
        rsi_vals = _calc_rsi(closes, TAB5_RSI_LEN)
        pivot_highs_t5, pivot_lows_t5 = _find_pivots(highs, lows, TAB5_PIVOT_LEFT, TAB5_PIVOT_RIGHT)
        cidx = len(df) - 2
        max_pivot_idx = cidx - TAB5_PIVOT_RIGHT

        if pos['side'] == 'Long':
            valid_phs = [(i, p) for i, p in pivot_highs_t5 if i <= max_pivot_idx]
            if len(valid_phs) >= 2:
                (i2, p2), (i1, p1) = valid_phs[-1], valid_phs[-2]
                if i2 + TAB5_PIVOT_RIGHT == cidx and p2 > p1 and rsi_vals[i2] < rsi_vals[i1]:
                    return True
        elif pos['side'] == 'Short':
            valid_pls = [(i, p) for i, p in pivot_lows_t5 if i <= max_pivot_idx]
            if len(valid_pls) >= 2:
                (i2, p2), (i1, p1) = valid_pls[-1], valid_pls[-2]
                if i2 + TAB5_PIVOT_RIGHT == cidx and p2 < p1 and rsi_vals[i2] > rsi_vals[i1]:
                    return True

    return False


def _calc_cci(df, length):
    """CCI = (TP - SMA(TP, n)) / (0.015 * MeanDeviation(TP, n))."""
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    sma_tp = tp.rolling(length).mean()
    mean_dev = tp.rolling(length).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    with np.errstate(divide='ignore', invalid='ignore'):
        cci = (tp - sma_tp) / (0.015 * mean_dev)
    return cci


def _calc_tab7_signals(df):
    """Return (long_signal, short_signal) on the latest closed candle (iloc[-2])."""
    if len(df) < TAB7_CCI_LEN + 5:
        return False, False
    cci = _calc_cci(df, TAB7_CCI_LEN).values
    prev_cci = cci[-3]
    curr_cci = cci[-2]
    if not (np.isfinite(prev_cci) and np.isfinite(curr_cci)):
        return False, False
    thr = TAB7_CCI_THRESHOLD
    long_sig  = (prev_cci <=  thr) and (curr_cci >  thr)
    short_sig = (prev_cci >= -thr) and (curr_cci < -thr)
    return bool(long_sig), bool(short_sig)


def evaluate_tab7_cci_1h(df):
    """Tab 7: CCI(30) ±125 Crossover.

    Note: Function name is legacy; production scheduler runs this on 4H candles.

    Long  : prev CCI <= +125 AND curr CCI >  +125 on latest closed bar.
    Short : prev CCI >= -125 AND curr CCI <  -125 on latest closed bar.
    Entry = open of next bar (opens[-1]).
    Risk  = min(ATR(14) * 2.1, entry * 0.05);  SL = entry ∓ risk; TP = entry ± risk * 1.75.
    Invalidation: opposite signal on a new closed bar → exit at next open.
    """
    min_bars = TAB7_CCI_LEN + TAB7_ATR_LEN + 5
    if len(df) < min_bars:
        return None

    long_sig, short_sig = _calc_tab7_signals(df)
    if not long_sig and not short_sig:
        return None

    atr = _calc_atr(df, TAB7_ATR_LEN).values
    atr_val = float(atr[-2])
    if not np.isfinite(atr_val) or atr_val <= 0:
        return None

    ep = float(df['open'].iloc[-1])
    raw_risk   = atr_val * TAB7_SL_ATR_MULT
    cap_risk   = ep * TAB7_RISK_CAP_PCT
    final_risk = min(raw_risk, cap_risk)
    if final_risk <= 0:
        return None

    if long_sig:
        sl = ep - final_risk
        tp = ep + final_risk * TAB7_RR
        if sl <= 0 or sl >= ep:
            return None
        return {'side': 'Long',  'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab7_CCI125'}
    else:
        sl = ep + final_risk
        tp = ep - final_risk * TAB7_RR
        if tp <= 0 or sl <= ep:
            return None
        return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab7_CCI125'}


# ----- TAB 8: THREE SOLDIERS / THREE CROWS 4H COMBO STABLE OPT -----
def _calc_adx(df, length):
    """Wilder's ADX(length). Returns pd.Series aligned with df index."""
    high  = df['high']
    low   = df['low']
    close = df['close']
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm  = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move.clip(lower=0)
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move.clip(lower=0)

    # Wilder's smoothing = RMA (alpha = 1/length)
    alpha = 1.0 / length
    atr      = tr.ewm(alpha=alpha, adjust=False, min_periods=length).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False, min_periods=length).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False, min_periods=length).mean() / atr
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=alpha, adjust=False, min_periods=length).mean()


def evaluate_tab8_three_soldiers_crows(df):
    """Tab 8: Three Soldiers / Three Crows 1H Combo Stable Opt.

    Long (Three White Soldiers):
      - 3 bullish candles i-2, i-1, i each with body_ratio >= 0.4 and close making HH
      - close[i] > EMA200[i]
      - ADX14[i] >= 20, vol_ratio >= 1.0, ema_dist_pct >= 0
      - close[i] > max(high[i-2], high[i-1]) * 1.001 (breakout confirm)
    Short: mirror — Three Black Crows, EMA below, breakout down by 0.999.

    Entry = open of next bar.
    Risk  = min(ATR14 * 2.5, entry * 0.07);  SL = entry ∓ risk;  TP = entry ± risk * 1.75.
    """
    min_bars = max(TAB8_EMA_LEN, TAB8_ADX_LEN, TAB8_VOLUME_SMA_LEN, TAB8_ATR_LEN) + 10
    if len(df) < min_bars:
        return None

    df = df.copy()
    df['_ema']       = df['close'].ewm(span=TAB8_EMA_LEN, adjust=False).mean()
    df['_adx']       = _calc_adx(df, TAB8_ADX_LEN)
    df['_vol_sma']   = df['volume'].rolling(TAB8_VOLUME_SMA_LEN).mean()
    df['_atr']       = _calc_atr(df, TAB8_ATR_LEN)

    # Signal candle is the latest CLOSED bar → iloc[-2]. Entry at open of iloc[-1].
    if len(df) < 4:
        return None
    c_i   = df.iloc[-2]   # signal candle (i)
    c_i1  = df.iloc[-3]   # i-1
    c_i2  = df.iloc[-4]   # i-2
    ep    = float(df['open'].iloc[-1])

    ema_val = float(c_i['_ema'])
    adx_val = float(c_i['_adx'])
    atr_val = float(c_i['_atr'])
    vol_sma = float(c_i['_vol_sma'])
    if not all(np.isfinite(v) and v > 0 for v in (ema_val, atr_val, vol_sma)):
        return None
    if not np.isfinite(adx_val):
        return None

    # Per-candle body ratio helper
    def body_ratio(candle):
        rng = float(candle['high']) - float(candle['low'])
        if rng <= 0:
            return 0.0
        return abs(float(candle['close']) - float(candle['open'])) / rng

    # Quality filters (shared)
    vol_ratio = float(c_i['volume']) / vol_sma if vol_sma > 0 else 0.0
    ema_dist_pct = abs(float(c_i['close']) - ema_val) / ema_val * 100 if ema_val > 0 else 0.0
    quality_ok = (
        adx_val >= TAB8_ADX_MIN
        and vol_ratio >= TAB8_VOLUME_RATIO_MIN
        and ema_dist_pct >= TAB8_EMA_DIST_MIN_PCT
    )
    if not quality_ok:
        return None

    # Body ratio filter
    if body_ratio(c_i2) < TAB8_BODY_MIN_RATIO: return None
    if body_ratio(c_i1) < TAB8_BODY_MIN_RATIO: return None
    if body_ratio(c_i)  < TAB8_BODY_MIN_RATIO: return None

    # Three White Soldiers
    bull_all = (
        c_i2['close'] > c_i2['open']
        and c_i1['close'] > c_i1['open']
        and c_i['close']  > c_i['open']
    )
    close_rising = c_i2['close'] < c_i1['close'] < c_i['close']
    trend_up = float(c_i['close']) > ema_val
    breakout_up = float(c_i['close']) > max(float(c_i2['high']), float(c_i1['high'])) * (1.0 + TAB8_BREAKOUT_BUFFER)

    # Three Black Crows
    bear_all = (
        c_i2['close'] < c_i2['open']
        and c_i1['close'] < c_i1['open']
        and c_i['close']  < c_i['open']
    )
    close_falling = c_i2['close'] > c_i1['close'] > c_i['close']
    trend_down = float(c_i['close']) < ema_val
    breakout_dn = float(c_i['close']) < min(float(c_i2['low']), float(c_i1['low'])) * (1.0 - TAB8_BREAKOUT_BUFFER)

    long_sig  = bull_all and close_rising and trend_up and breakout_up
    short_sig = bear_all and close_falling and trend_down and breakout_dn
    if not long_sig and not short_sig:
        return None

    # Risk model
    atr_risk = atr_val * TAB8_SL_ATR_MULT
    cap_risk = ep * TAB8_RISK_CAP_PCT
    risk_abs = min(atr_risk, cap_risk)
    if risk_abs <= 0:
        return None

    if long_sig:
        sl = ep - risk_abs
        # Global MAX_SL_PCT defensive cap
        sl = max(sl, ep * (1 - MAX_SL_PCT))
        tp = ep + (ep - sl) * TAB8_RR
        if sl <= 0 or sl >= ep:
            return None
        return {'side': 'Long',  'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab8_3Soldiers'}
    else:
        sl = ep + risk_abs
        sl = min(sl, ep * (1 + MAX_SL_PCT))
        tp = ep - (sl - ep) * TAB8_RR
        if tp <= 0 or sl <= ep:
            return None
        return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab8_3Crows'}


def evaluate_tab9_impulse_move_continuation(df):
    """Tab 9: PA Impulse Move Continuation 1H Best.

    Signal candle = latest closed bar (i = iloc[-2]); entry = open of next bar.
    Long: 3-bar net move from close[i-3] to close[i] >= 1.8*ATR14,
    closes rising for the last 3 closed bars, and close[i] above EMA200.
    Short: mirror condition below EMA200.
    Risk = min(ATR14 * 1.75, entry * 7%); TP = risk * 1.25.
    """
    min_bars = max(TAB9_EMA_LEN, TAB9_ATR_LEN) + 10
    if len(df) < min_bars:
        return None

    df = df.copy()
    df['_ema200'] = df['close'].ewm(span=TAB9_EMA_LEN, adjust=False).mean()
    df['_atr'] = _calc_atr(df, TAB9_ATR_LEN)

    cidx = len(df) - 2
    if cidx < 3:
        return None

    c0 = float(df['close'].iloc[cidx - 3])
    c1 = float(df['close'].iloc[cidx - 2])
    c2 = float(df['close'].iloc[cidx - 1])
    c3 = float(df['close'].iloc[cidx])
    ema200 = float(df['_ema200'].iloc[cidx])
    atr14 = float(df['_atr'].iloc[cidx])
    ep = float(df['open'].iloc[-1])

    if not all(np.isfinite(v) and v > 0 for v in (c0, c1, c2, c3, ema200, atr14, ep)):
        return None

    net_move = c3 - c0
    long_sig = (
        net_move >= atr14 * TAB9_NET_ATR_MULT
        and c1 < c2 < c3
        and c3 > ema200
    )
    short_sig = (
        net_move <= -atr14 * TAB9_NET_ATR_MULT
        and c1 > c2 > c3
        and c3 < ema200
    )
    if not long_sig and not short_sig:
        return None

    risk_abs = min(atr14 * TAB9_SL_ATR_MULT, ep * TAB9_RISK_CAP_PCT)
    if risk_abs <= 0:
        return None

    if long_sig:
        sl = ep - risk_abs
        sl = max(sl, ep * (1 - MAX_SL_PCT))
        tp = ep + (ep - sl) * TAB9_RR
        if sl <= 0 or sl >= ep:
            return None
        return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab9_ImpulseCont'}

    sl = ep + risk_abs
    sl = min(sl, ep * (1 + MAX_SL_PCT))
    tp = ep - (sl - ep) * TAB9_RR
    if tp <= 0 or sl <= ep:
        return None
    return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab9_ImpulseCont'}


def evaluate_tab10_vol_range_expansion_spike(df):
    """Tab 10: Volume Range Expansion Spike 1H.

    Signal candle = latest closed bar; entry = open of next bar.
    Long: bullish candle above EMA150, range >= 0.9*ATR14, volume spike
    >= 1.4*SMA20(volume), and body ratio >= 0.5.
    Short: bearish mirror below EMA150. Exit is SL/TP only.
    """
    min_bars = max(TAB10_EMA_LEN, TAB10_ATR_LEN, TAB10_VOLUME_SMA_LEN) + 5
    if len(df) < min_bars:
        return None

    df = df.copy()
    df['_ema150'] = df['close'].ewm(span=TAB10_EMA_LEN, adjust=False).mean()
    df['_atr'] = _calc_atr(df, TAB10_ATR_LEN)
    df['_vol_sma'] = df['volume'].rolling(TAB10_VOLUME_SMA_LEN).mean()

    row = df.iloc[-2]
    ep = float(df['open'].iloc[-1])

    o = float(row['open'])
    h = float(row['high'])
    l = float(row['low'])
    c = float(row['close'])
    v = float(row['volume'])
    ema150 = float(row['_ema150'])
    atr14 = float(row['_atr'])
    vol_sma = float(row['_vol_sma'])

    if not all(np.isfinite(x) and x > 0 for x in (o, h, l, c, v, ema150, atr14, vol_sma, ep)):
        return None

    candle_range = h - l
    if candle_range <= 0:
        return None

    body_ratio = abs(c - o) / max(candle_range, 1e-12)
    expansion_ok = candle_range >= atr14 * TAB10_RANGE_ATR_MULT
    volume_ok = v >= vol_sma * TAB10_VOLUME_MULT
    body_ok = body_ratio >= TAB10_BODY_MIN_RATIO
    if not (expansion_ok and volume_ok and body_ok):
        return None

    long_sig = c > o and c > ema150
    short_sig = c < o and c < ema150
    if not long_sig and not short_sig:
        return None

    risk_abs = min(atr14 * TAB10_SL_ATR_MULT, ep * TAB10_RISK_CAP_PCT)
    if risk_abs <= 0:
        return None

    if long_sig:
        sl = ep - risk_abs
        sl = max(sl, ep * (1 - MAX_SL_PCT))
        tp = ep + (ep - sl) * TAB10_RR
        if sl <= 0 or sl >= ep:
            return None
        return {'side': 'Long', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab10_VolRangeSpike'}

    sl = ep + risk_abs
    sl = min(sl, ep * (1 + MAX_SL_PCT))
    tp = ep - (sl - ep) * TAB10_RR
    if tp <= 0 or sl <= ep:
        return None
    return {'side': 'Short', 'ep': ep, 'sl': sl, 'tp': tp, 'reason': 'Tab10_VolRangeSpike'}


# Backward-compatible aliases kept while legacy names still exist.
evaluate_tab7_cci_4h = evaluate_tab7_cci_1h   # old 4H name kept for rollback compatibility
evaluate_tab1_ema4h = evaluate_tab1_ema_pullback_1h
evaluate_tab2_ema_cross = evaluate_tab2_ema_1h
evaluate_tab3_smc260 = evaluate_tab3_smc_ob_1h
evaluate_tab4_ote = evaluate_tab4_ote_1h
evaluate_tab5_rsi_divergence = evaluate_tab5_rsi_divergence_1h
evaluate_tab6_squeeze = evaluate_tab6_squeeze_1h
