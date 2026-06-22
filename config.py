# Central configuration — all magic numbers live here
import os
from dotenv import load_dotenv

load_dotenv()  # reads .env file if present

# ── Live Trading ──────────────────────────────────────────────────────────────
LIVE_MODE          = os.getenv("LIVE_MODE", "false").lower() == "true"
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BINANCE_API_ACCOUNT_TYPE = os.getenv("BINANCE_API_ACCOUNT_TYPE", "futures_regular").strip().lower()
BINANCE_FUTURES_API_KEY = os.getenv("BINANCE_FUTURES_API_KEY", BINANCE_API_KEY)
BINANCE_FUTURES_API_SECRET = os.getenv("BINANCE_FUTURES_API_SECRET", BINANCE_API_SECRET)
BINANCE_LEADER_API_KEY = os.getenv("BINANCE_LEADER_API_KEY", "")
BINANCE_LEADER_API_SECRET = os.getenv("BINANCE_LEADER_API_SECRET", "")
_legacy_testnet    = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
ORDER_ENV          = os.getenv("ORDER_ENV", "mainnet").strip().lower()
PRICE_FEED_ENV     = os.getenv("PRICE_FEED_ENV", "mainnet").strip().lower()
if BINANCE_API_ACCOUNT_TYPE not in {"futures_regular", "leader_trade"}:
    BINANCE_API_ACCOUNT_TYPE = "futures_regular"
if BINANCE_API_ACCOUNT_TYPE == "leader_trade":
    BINANCE_API_KEY = BINANCE_LEADER_API_KEY
    BINANCE_API_SECRET = BINANCE_LEADER_API_SECRET
else:
    BINANCE_API_KEY = BINANCE_FUTURES_API_KEY
    BINANCE_API_SECRET = BINANCE_FUTURES_API_SECRET
if ORDER_ENV not in {"testnet", "mainnet"}:
    ORDER_ENV = "testnet"
if PRICE_FEED_ENV not in {"testnet", "mainnet"}:
    PRICE_FEED_ENV = "mainnet"
BINANCE_TESTNET    = ORDER_ENV == "testnet"
PRICE_FEED_TESTNET = PRICE_FEED_ENV == "testnet"
# SL/TP placement mode (dashboard can override via state["sltp_mode"]):
#   local            — bot-managed SL+TP (no exchange algo orders)
#   binance          — STOP+TP algo on exchange (cap ~200 mainnet)
#   hybrid           — SL on exchange, TP bot-managed
#   binance_fallback — exchange SL+TP when slots available, else local both
# Legacy LOCAL_SLTP=true maps to local when SLTP_MODE is unset.
LOCAL_SLTP         = os.getenv("LOCAL_SLTP", "false").lower() == "true"
_SLTP_MODE_RAW     = os.getenv("SLTP_MODE", "").strip().lower()
SLTP_MODES         = frozenset({"local", "binance", "hybrid", "binance_fallback"})
if _SLTP_MODE_RAW in SLTP_MODES:
    SLTP_MODE = _SLTP_MODE_RAW
elif LOCAL_SLTP:
    SLTP_MODE = "local"
else:
    SLTP_MODE = "binance_fallback"
PRICE_FEED_BASE_URL = (
    "https://testnet.binancefuture.com" if PRICE_FEED_TESTNET
    else "https://fapi.binance.com"
)
# Binance split WS bases (2025+): !miniTicker@arr (last; !ticker@arr deprecated) + !markPrice@arr@1s (mark).
# Mark is used for unrealized PnL; miniTicker updates latest_prices (last trade).
PRICE_FEED_WS_URL = (
    "wss://stream.binancefuture.com/market/stream?streams=!miniTicker@arr/!markPrice@arr@1s"
    if PRICE_FEED_TESTNET
    else "wss://fstream.binance.com/market/stream?streams=!miniTicker@arr/!markPrice@arr@1s"
)
LEVERAGE                   = int(os.getenv("LEVERAGE", "5"))
DASHBOARD_PASSCODE         = os.getenv("DASHBOARD_PASSCODE", "")
DASHBOARD_AUTH_ENABLED     = os.getenv("DASHBOARD_AUTH_ENABLED", "false").lower() == "true"
DASHBOARD_ALLOWED_ORIGINS  = [
    o.strip() for o in os.getenv("DASHBOARD_ALLOWED_ORIGINS", "").split(",") if o.strip()
] or ["*"]  # fallback: allow all origins when env var is not set (set explicitly in production!)
# Default 8765 — Chrome/Edge block port 6000 (X11 / ERR_UNSAFE_PORT).
DASHBOARD_PORT             = int(os.getenv("DASHBOARD_PORT", "8765"))
# LIVE All-tab equity curve: plot account margin balance from this USD baseline (0 = strategy PnL curve).
EQUITY_CURVE_MARGIN_BASELINE = float(os.getenv("EQUITY_CURVE_MARGIN_BASELINE", "7000") or 0)
CIRCUIT_BREAKER_DAILY_LOSS = float(os.getenv("CIRCUIT_BREAKER_DAILY_LOSS", "500"))
TELEGRAM_ENABLED           = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID           = os.getenv("TELEGRAM_CHAT_ID", "")
# Telegram policy: only TP/SL exit fills and errors (no entry, sync recovery, or info alerts).
# ─────────────────────────────────────────────────────────────────────────────

# ── Pionex (dashboard balance card; read-only) ──────────────────────────────
PIONEX_API_KEY    = os.getenv("PIONEX_API_KEY", "")
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET", "")
PIONEX_BALANCE_POLL_SEC = int(os.getenv("PIONEX_BALANCE_POLL_SEC", "60"))
PIONEX_USDT_THB_RATE = float(os.getenv("PIONEX_USDT_THB_RATE", "0") or 0)
PIONEX_CONFIGURED = bool(PIONEX_API_KEY and PIONEX_API_SECRET)

INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "7000"))
# Position sizing — .env only (dashboard cannot change; restart required)
NOTIONAL_SIZE = float(os.getenv("NOTIONAL_SIZE", "100"))   # USD notional per trade
_margin_env = os.getenv("MARGIN_SIZE", "").strip()
MARGIN_SIZE = (
    float(_margin_env)
    if _margin_env
    else (NOTIONAL_SIZE / LEVERAGE if LEVERAGE > 0 else NOTIONAL_SIZE)
)

# Paper trading cost model. Live trading uses exchange fills/commission history.
ENTRY_FEE_PCT = float(os.getenv("PAPER_ENTRY_FEE_PCT", "0.0005"))       # 0.05% taker
EXIT_FEE_MAKER_PCT = float(os.getenv("PAPER_EXIT_MAKER_FEE_PCT", "0.0002"))  # 0.02% maker
EXIT_FEE_TAKER_PCT = float(os.getenv("PAPER_EXIT_TAKER_FEE_PCT", "0.0005"))  # 0.05% taker
SLIPPAGE_PCT = float(os.getenv("PAPER_SLIPPAGE_PCT", "0.0003"))        # 0.03% adverse

# Risk
MAX_SL_PCT = 0.085              # hard cap on stop-loss distance
# Block new entries when effective notional exceeds this USD cap (0 = disabled).
MAX_NOTIONAL_SIZE = float(os.getenv("MAX_NOTIONAL_SIZE", "0") or 0)
MAX_POSITIONS_PER_TAB = 20      # max concurrent positions per tab
SYMBOL_SCAN_LIMIT = 500         # scan top N USDT Futures symbols by quote volume
# Per-tab symbol filter (dashboard): off | auto_winners | allowlist
SYMBOL_FILTER_MODES = frozenset({"off", "auto_winners", "allowlist"})
SYMBOL_FILTER_DEFAULT_MIN_TRADES = 5
SYMBOL_FILTER_DEFAULT_MIN_WIN_RATE = 0.60   # 60%
SYMBOL_FILTER_DEFAULT_MIN_NET_PNL = 0.50    # USD
# Auto-winners + leaderboard use last N closed trades per tab+symbol (not all-time).
SYMBOL_FILTER_ROLLING_WINDOW = int(os.getenv("SYMBOL_FILTER_ROLLING_WINDOW", "30"))
# Max concurrent Binance kline REST requests during candle scans (avoids 429 bursts).
KLINE_FETCH_CONCURRENCY = int(os.getenv("KLINE_FETCH_CONCURRENCY", "25"))

# Tabs enabled on fresh reset / new paper_state (dashboard can override)
STARTUP_ENABLED_TABS = frozenset({
    "Tab3",
})
LOW_MARGIN_THRESHOLD = 50.0    # USDT — alert when available margin drops below this
MIN_ENTRY_AVAILABLE_MARGIN = float(os.getenv("MIN_ENTRY_AVAILABLE_MARGIN", "100"))  # USDT — block new live entries below this

# Entry quality filter — skip entries when market conditions are hostile.
MAX_FUNDING_RATE_ABS       = float(os.getenv("MAX_FUNDING_RATE_ABS", "0.01"))    # 1.00% per 8h
MAX_SPREAD_PCT             = float(os.getenv("MAX_SPREAD_PCT", "0.001"))         # 0.10% bid/ask spread
MAX_ENTRY_SIGNAL_DRIFT_PCT = float(os.getenv("MAX_ENTRY_SIGNAL_DRIFT_PCT", "0.003"))  # 0.30% from signal entry

# Exchange SL/TP placement vs trigger price (live algo orders only; LOCAL_SLTP skips nudge)
# SLTP_TRIGGER_PRICE: "last" = contract/last price (Binance CONTRACT_PRICE); "mark" = index mark (MARK_PRICE)
_SLTP_TRIGGER_RAW = os.getenv("SLTP_TRIGGER_PRICE", "last").strip().lower()
SLTP_TRIGGER_PRICE = _SLTP_TRIGGER_RAW if _SLTP_TRIGGER_RAW in ("last", "mark") else "last"
ALGO_WORKING_TYPE = "CONTRACT_PRICE" if SLTP_TRIGGER_PRICE == "last" else "MARK_PRICE"
MARK_FILL_SANITY_PCT = float(os.getenv("MARK_FILL_SANITY_PCT", "0.03"))  # ignore trigger ref for nudge when |ref-fill|/fill exceeds this
EXCHANGE_MARK_NUDGE_PCT = float(os.getenv("EXCHANGE_MARK_NUDGE_PCT", "0.005"))  # min distance from trigger ref (Binance algo safety)
MAX_EXCHANGE_PROTECTION_NUDGE_PCT = float(os.getenv("MAX_EXCHANGE_PROTECTION_NUDGE_PCT", "0.02"))  # cap SL/TP drift from strategy due to nudge

# State file caps
HISTORY_CAP = int(os.getenv("HISTORY_CAP", "50000"))  # keep last N closed trades in memory
USED_SETUPS_CAP = 1000          # keep last N used setup keys

# LIVE dashboard: optional bulk Binance userTrades REST cache (off by default — use bot history).
BINANCE_CLOSE_HISTORY_ENABLED = os.getenv("BINANCE_CLOSE_HISTORY_ENABLED", "0").strip().lower() in (
    "1", "true", "yes", "on",
)
# LIVE dashboard: Binance userTrades close cache (Recent Trades + optional stats; only when ENABLED)
BINANCE_CLOSE_HISTORY_DAYS = int(os.getenv("BINANCE_CLOSE_HISTORY_DAYS", "90"))
# Max closed trades per WebSocket tick (full history stays in state; prevents WS >1MB frame errors).
DASHBOARD_WS_HISTORY_LIMIT = int(os.getenv("DASHBOARD_WS_HISTORY_LIMIT", "800"))
# Max rows per GET /api/history page (dashboard Recent Trades pagination).
DASHBOARD_HISTORY_PAGE_MAX = int(os.getenv("DASHBOARD_HISTORY_PAGE_MAX", "200"))
# Max points returned by GET /api/equity-curve (downsampled when history is larger).
DASHBOARD_EQUITY_CURVE_MAX_POINTS = int(os.getenv("DASHBOARD_EQUITY_CURVE_MAX_POINTS", "2000"))
BINANCE_CLOSE_HISTORY_SYMBOL_CAP = int(os.getenv("BINANCE_CLOSE_HISTORY_SYMBOL_CAP", "40"))
# Background refresh: fewer symbols + longer TTL to avoid REST/IP bans (force/startup uses full cap).
BINANCE_CLOSE_HISTORY_TTL_SEC = int(os.getenv("BINANCE_CLOSE_HISTORY_TTL_SEC", "300"))
BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP = int(
    os.getenv("BINANCE_CLOSE_HISTORY_REFRESH_SYMBOL_CAP", "15")
)
# Round-robin userTrades: N symbols per refresh cycle (full list capped by SYMBOL_CAP).
BINANCE_CLOSE_HISTORY_BATCH_SIZE = int(os.getenv("BINANCE_CLOSE_HISTORY_BATCH_SIZE", "5"))
BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC = float(
    os.getenv("BINANCE_CLOSE_HISTORY_SYMBOL_DELAY_SEC", "0.25")
)
# Live account snapshot: REST poll slows when UDS ACCOUNT_UPDATE is fresh.
EXCHANGE_ACCOUNT_POLL_SEC_UDS = int(os.getenv("EXCHANGE_ACCOUNT_POLL_SEC_UDS", "90"))
EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS = int(os.getenv("EXCHANGE_ACCOUNT_POLL_SEC_NO_UDS", "15"))
EXCHANGE_ACCOUNT_SYNC_SEC_UDS = int(os.getenv("EXCHANGE_ACCOUNT_SYNC_SEC_UDS", "120"))
EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS = int(os.getenv("EXCHANGE_ACCOUNT_SYNC_SEC_NO_UDS", "60"))
# When UDS is connected + fresh, position sync can be much less frequent.
EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED = int(
    os.getenv("EXCHANGE_ACCOUNT_SYNC_SEC_UDS_CONNECTED", "300")
)
UDS_ACCOUNT_FRESH_SEC = int(os.getenv("UDS_ACCOUNT_FRESH_SEC", "600"))
# Startup PnL repair: defer + small batches to avoid userTrades burst.
PNL_REPAIR_STARTUP_DELAY_SEC = int(os.getenv("PNL_REPAIR_STARTUP_DELAY_SEC", "120"))
PNL_REPAIR_BATCH_SIZE = int(os.getenv("PNL_REPAIR_BATCH_SIZE", "3"))
PNL_REPAIR_BATCH_PAUSE_SEC = float(os.getenv("PNL_REPAIR_BATCH_PAUSE_SEC", "5.0"))
PNL_REPAIR_ENTRY_DELAY_SEC = float(os.getenv("PNL_REPAIR_ENTRY_DELAY_SEC", "1.0"))
PNL_REPAIR_DEFER_POLL_SEC = float(os.getenv("PNL_REPAIR_DEFER_POLL_SEC", "10.0"))
# Defer userTrades (PnL repair/reconcile) while candle eval / live entries run.
ENTRY_EVAL_BUDGET_SEC = float(os.getenv("ENTRY_EVAL_BUDGET_SEC", "180"))
ENTRY_BUSY_BUFFER_SEC = float(os.getenv("ENTRY_BUSY_BUFFER_SEC", "90"))
# Ticker prescreen (minute UTC): build watchlist before candle close; kline scan uses list at hour open.
KLINE_PRESCREEN_ENABLED = os.getenv("KLINE_PRESCREEN_ENABLED", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
KLINE_PRESCREEN_MINUTE = int(os.getenv("KLINE_PRESCREEN_MINUTE", "58"))
KLINE_PRESCREEN_TOP_N = int(os.getenv("KLINE_PRESCREEN_TOP_N", "500"))
KLINE_PRESCREEN_MIN_CHG_PCT = float(os.getenv("KLINE_PRESCREEN_MIN_CHG_PCT", "1.5"))
# Short-only: keep symbols in lower portion of 24h range; long-only uses upper portion.
KLINE_PRESCREEN_RANGE_EDGE = float(os.getenv("KLINE_PRESCREEN_RANGE_EDGE", "0.40"))

# Candle schedule: fetch klines at close+KLINE_FETCH_DELAY_SEC (min 10s so new-candle open settles).
KLINE_FETCH_DELAY_SEC = int(os.getenv("KLINE_FETCH_DELAY_SEC", "10"))
KLINE_FETCH_MIN_DELAY_SEC = int(os.getenv("KLINE_FETCH_MIN_DELAY_SEC", "10"))
# Live local SL/TP: skip bot-managed exit checks briefly after entry (fill verify + mark sync).
ENTRY_LOCAL_SL_GRACE_SEC = float(os.getenv("ENTRY_LOCAL_SL_GRACE_SEC", "30"))
ENTRY_STAGGER_SEC = float(os.getenv("ENTRY_STAGGER_SEC", "2"))
# Dashboard Close All: one market order per hedge leg, staggered to avoid REST bursts.
CLOSE_ALL_STAGGER_SEC = float(os.getenv("CLOSE_ALL_STAGGER_SEC", "2"))
CLOSE_ALL_PREFLIGHT = os.getenv("CLOSE_ALL_PREFLIGHT", "1").strip().lower() in ("1", "true", "yes")
# Dashboard Close All / Close Strategy: auto-retry remaining positions after rate limit (429).
CLOSE_ALL_RETRY_SEC = int(os.getenv("CLOSE_ALL_RETRY_SEC", "60"))
# Live entry: on Binance -4192, halt batch; after cooling delay, poll until mark is at-or-better than signal ep.
ENTRY_4192_RETRY_DELAY_SEC = int(os.getenv("ENTRY_4192_RETRY_DELAY_SEC", "10"))
ENTRY_4192_PRICE_POLL_SEC = float(os.getenv("ENTRY_4192_PRICE_POLL_SEC", "2"))
ENTRY_4192_MAX_RETRIES = int(os.getenv("ENTRY_4192_MAX_RETRIES", "5"))
ENTRY_4192_RETRY_MAX_AGE_SEC = int(os.getenv("ENTRY_4192_RETRY_MAX_AGE_SEC", "600"))  # 10 min from candle close
# Entry: wait for mark at-or-better than signal ep before market fill (live + paper).
ENTRY_WAIT_FOR_BETTER_PRICE = os.getenv("ENTRY_WAIT_FOR_BETTER_PRICE", "1").strip().lower() in (
    "1", "true", "yes", "on",
)
ENTRY_PRICE_WAIT_MAX_SEC = int(os.getenv("ENTRY_PRICE_WAIT_MAX_SEC", "600"))  # skip if not ready by then
ENTRY_PRICE_POLL_SEC = float(os.getenv("ENTRY_PRICE_POLL_SEC", "2"))
# Min mark improvement vs signal ep before entry (0 = at-or-better; 0.001 = 0.1% better).
ENTRY_MIN_PRICE_IMPROVE_PCT = float(os.getenv("ENTRY_MIN_PRICE_IMPROVE_PCT", "0") or 0)
# Entry price wait / -4192 retry: "last" = contract last trade; "mark" = index mark.
_ENTRY_TRIGGER_RAW = os.getenv("ENTRY_TRIGGER_PRICE", "last").strip().lower()
ENTRY_TRIGGER_PRICE = _ENTRY_TRIGGER_RAW if _ENTRY_TRIGGER_RAW in ("last", "mark") else "last"
# Entry order style: market (default) or limit GTC at signal ep.
_ENTRY_ORDER_STYLE_RAW = os.getenv("ENTRY_ORDER_STYLE", "limit").strip().lower()
ENTRY_ORDER_STYLE = _ENTRY_ORDER_STYLE_RAW if _ENTRY_ORDER_STYLE_RAW in ("market", "limit") else "market"
ENTRY_LIMIT_TIF = os.getenv("ENTRY_LIMIT_TIF", "GTC").strip().upper()
ENTRY_LIMIT_MAX_AGE_SEC = int(os.getenv("ENTRY_LIMIT_MAX_AGE_SEC", str(ENTRY_PRICE_WAIT_MAX_SEC)))
# Exchange TP style when SLTP mode places orders on Binance (SL stays STOP_MARKET).
_SLTP_TP_STYLE_RAW = os.getenv("SLTP_TP_STYLE", "limit").strip().lower()
SLTP_TP_STYLE = _SLTP_TP_STYLE_RAW if _SLTP_TP_STYLE_RAW in ("market", "limit") else "market"
ENTRY_LIMIT_TP_PRICE_MODE = os.getenv("ENTRY_LIMIT_TP_PRICE_MODE", "same_as_trigger").strip().lower()
# Default REST backoff when Binance returns 418/-1003 without "banned until <ms>" in the body.
BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC = int(os.getenv("BINANCE_IP_BAN_DEFAULT_BACKOFF_SEC", "120"))

# Timeframe each Tab runs on
TAB_TIMEFRAMES = {
    "Tab1": "4h",
    "Tab2": "4h",
    "Tab3": "4h",
    "Tab4": "4h",
    "Tab5": "1h",
    "Tab6": "4h",
    "Tab7": "4h",
    "Tab8": "1h",
    "Tab9": "1h",
    "Tab10": "1h",
}

# Tab2: EMA5/EMA21 Crossover + ATR SL/TP (4H)
TAB2_EMA_FAST         = 6
TAB2_EMA_SLOW         = 35
TAB2_ATR_LEN          = 14
TAB2_SL_ATR_MULT      = 2.1
TAB2_RR               = 1.8
TAB2_TP_ATR_MULT      = TAB2_SL_ATR_MULT * TAB2_RR   # 3.78

TABS = list(TAB_TIMEFRAMES.keys())

# --- Tab 1: EMA Pullback (native 4H 110/190) ---
TAB1_EMA_FAST        = 110
TAB1_EMA_SLOW        = 190

# --- Tab 4: Premium/Discount OTE EMA240 (native 4H) ---
SMC5_PIVOT_LEFT        = 3
SMC5_PIVOT_RIGHT       = 3
SMC5_TREND_EMA         = 240
SMC5_SIGNAL_EXPIRY     = 5
SMC5_BREAKOUT_MARGIN   = 0.00125
SMC5_RISK_BUFFER       = 0.0003
SMC5_RISK_REWARD       = 3.75
SMC5_OTE_LOW           = 0.66     # 66% fib retracement
SMC5_OTE_HIGH          = 0.72     # 72% fib retracement

# --- Tab 3: SMC Order Block EMA260 (native 4H) ---
SMC4_PIVOT_LEFT                    = 3
SMC4_PIVOT_RIGHT                   = 3
SMC4_TREND_EMA                     = 260
SMC4_OB_LOOKBACK                   = 7
SMC4_SIGNAL_EXPIRY                 = 4
SMC4_BREAKOUT_MARGIN               = 0.00135
SMC4_RISK_BUFFER                   = 0.0003
SMC4_RISK_REWARD                   = 3.75
SMC4_BREAKOUT_DISPLACEMENT_ATR_MULT = 0.45

# --- Tab 5: RSI Divergence 1H Balance ---
TAB5_RSI_LEN       = 2
TAB5_PIVOT_LEFT    = 3
TAB5_PIVOT_RIGHT   = 3
TAB5_ATR_LEN       = 14
TAB5_SL_ATR_MULT   = 1.7
TAB5_RR            = 3.6
TAB5_TP_ATR_MULT   = TAB5_SL_ATR_MULT * TAB5_RR   # 6.12
TAB5_MAX_HOLD_BARS = 48                            # 48 bars × 1H = 48 hours max hold (2 days)

# --- Tab 6: BB/KC Squeeze Breakout 4H Opt ---
TAB6_BB_LEN       = 30
TAB6_BB_STD       = 1.3
TAB6_KC_LEN       = 20
TAB6_KC_MULT      = 1.6      # KC uses SMA(TR, kc_len) × mult
TAB6_ATR_LEN      = 14       # SMA(TR, 14) for SL sizing
TAB6_SL_ATR_MULT  = 2.0
TAB6_SL_CAP_PCT   = 0.075
TAB6_RR           = 1.5

# --- Tab 7: CCI 125 OPT 4H ---
TAB7_CCI_LEN       = 30
TAB7_CCI_THRESHOLD = 125
TAB7_ATR_LEN       = 14
TAB7_SL_ATR_MULT   = 2.1
TAB7_RISK_CAP_PCT  = 0.05
TAB7_RR            = 1.75

# --- Tab 8: Three Soldiers / Three Crows 1H Combo Stable Opt ---
TAB8_EMA_LEN            = 200
TAB8_ADX_LEN            = 14
TAB8_ADX_MIN            = 20
TAB8_VOLUME_SMA_LEN     = 20
TAB8_VOLUME_RATIO_MIN   = 1.0
TAB8_EMA_DIST_MIN_PCT   = 0.0
TAB8_BODY_MIN_RATIO     = 0.4
TAB8_BREAKOUT_BUFFER    = 0.001   # 0.1% breakout buffer (×1.001 long / ×0.999 short)
TAB8_ATR_LEN            = 14
TAB8_SL_ATR_MULT        = 2.5
TAB8_RISK_CAP_PCT       = 0.07   # per-strategy SL cap (tighter than global 8.5%)
TAB8_RR                 = 1.75

# --- Tab 9: PA Impulse Move Continuation 1H Best ---
TAB9_EMA_LEN            = 200
TAB9_ATR_LEN            = 14
TAB9_NET_ATR_MULT       = 1.8
TAB9_SL_ATR_MULT        = 1.75
TAB9_RISK_CAP_PCT       = 0.07   # per-strategy SL cap (tighter than global 8.5%)
TAB9_RR                 = 1.25

# --- Tab 10: Volume Range Expansion Spike 1H ---
TAB10_EMA_LEN           = 150
TAB10_ATR_LEN           = 14
TAB10_VOLUME_SMA_LEN    = 20
TAB10_VOLUME_MULT       = 1.4
TAB10_RANGE_ATR_MULT    = 0.9
TAB10_BODY_MIN_RATIO    = 0.5
TAB10_SL_ATR_MULT       = 1.5
TAB10_RISK_CAP_PCT      = 0.07   # per-strategy SL cap (tighter than global 8.5%)
TAB10_RR                = 1.5




