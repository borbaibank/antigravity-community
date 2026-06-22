"""Log capture, terminal styling, print hook, and rotating file log."""

from __future__ import annotations

import asyncio
import builtins
import logging
import os
import re
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

import binance_live
from config import (
    BINANCE_CLOSE_HISTORY_DAYS,
    BINANCE_CLOSE_HISTORY_ENABLED,
    ENTRY_4192_PRICE_POLL_SEC,
    ENTRY_4192_RETRY_DELAY_SEC,
    ENTRY_LOCAL_SL_GRACE_SEC,
    ENTRY_PRICE_POLL_SEC,
    ENTRY_PRICE_WAIT_MAX_SEC,
    ENTRY_MIN_PRICE_IMPROVE_PCT,
    ENTRY_TRIGGER_PRICE,
    ENTRY_STAGGER_SEC,
    ENTRY_WAIT_FOR_BETTER_PRICE,
    DASHBOARD_PORT,
    LIVE_MODE,
    ORDER_ENV,
    PRICE_FEED_ENV,
    STARTUP_ENABLED_TABS,
    TELEGRAM_ENABLED,
)

_log_buffer: deque = deque(maxlen=300)

_ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
}
_NOISY_HTTP_PATHS = ("/api/logs", "/static/", "/favicon.ico")
_TAG_COLORS = {
    "WARN": "yellow",
    "ERROR": "red",
    "CRITICAL": "red",
    "Scheduler": "magenta",
    "Live Sync": "blue",
    "Live": "yellow",
    "Filter": "gray",
    "Preflight": "gray",
    "Paper": "gray",
    "Side Filter": "gray",
    "Entry Gate": "gray",
    "Entry Retry": "cyan",
    "Entry Wait": "cyan",
    "Entry Guard": "yellow",
    "SyncIssue": "yellow",
    "Circuit Breaker": "red",
    "Telegram": "gray",
    "Startup": "cyan",
    "Migration": "cyan",
    "Price Monitor": "gray",
    "Price Poll": "gray",
    "API Ban": "yellow",
    "Purge": "yellow",
    "PnL Reconcile": "dim",
    "Income Sync": "cyan",
}

_send_telegram_hook: Callable[..., Awaitable[Any]] | None = None
_record_error_event_hook: Callable[..., Awaitable[Any]] | None = None


def register_log_hooks(
    send_telegram: Callable[..., Awaitable[Any]] | None = None,
    record_error_event: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    global _send_telegram_hook, _record_error_event_hook
    if send_telegram is not None:
        _send_telegram_hook = send_telegram
    if record_error_event is not None:
        _record_error_event_hook = record_error_event


def _enable_windows_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0007)
    except Exception:
        pass


def _use_terminal_color() -> bool:
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _strip_ansi(text: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", text)


def _dt_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo).astimezone(timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


_enable_windows_ansi()


def _c(text: str, color: str) -> str:
    if not _use_terminal_color():
        return text
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


def _utc_log_stamp(value: str | None = None) -> str:
    """Human-readable UTC timestamp for terminal logs."""
    if value:
        ms = _dt_to_ms(value) if isinstance(value, str) else None
        if ms:
            dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _log_price(sym: str, px: float) -> str:
    try:
        return f"{binance_live.round_price(sym, float(px)):g}"
    except Exception:
        return f"{float(px):.6g}"


def _sl_tp_pct_from_entry(side: str, entry: float, sl: float, tp: float) -> tuple[str, str]:
    e = float(entry or 0)
    if e <= 0:
        return ("—", "—")
    sl_p, tp_p = float(sl), float(tp)
    if str(side).lower().startswith("l"):
        sl_pct = (sl_p - e) / e * 100.0
        tp_pct = (tp_p - e) / e * 100.0
    else:
        sl_pct = (e - sl_p) / e * 100.0
        tp_pct = (e - tp_p) / e * 100.0
    return (f"{sl_pct:+.2f}%", f"{tp_pct:+.2f}%")


def _exit_move_pct_from_entry(side: str, entry: float, exit_px: float) -> tuple[str, str]:
    """Signed price move entry→exit (%); color green for profit, red for loss."""
    e = float(entry or 0)
    if e <= 0:
        return ("—", "dim")
    x = float(exit_px)
    if str(side).lower().startswith("l"):
        pct = (x - e) / e * 100.0
    else:
        pct = (e - x) / e * 100.0
    label = "กำไร" if pct >= 0 else "ขาดทุน"
    color = "green" if pct >= 0 else "red"
    return (f"{label} {pct:+.2f}%", color)


def _signed_price_delta(sym: str, delta: float) -> str:
    if delta >= 0:
        return f"+{_log_price(sym, delta)}"
    return f"-{_log_price(sym, -delta)}"


def _exit_target_slip_from_fill(
    *,
    reason: str,
    side: str,
    entry_price: float,
    exit_price: float,
    placed_sl: float | None,
    placed_tp: float | None,
    sym: str,
) -> dict | None:
    """Placed SL/TP (from fill) vs actual exit — for terminal/Telegram."""
    r = str(reason or "").upper()
    if r == "SL":
        label, target, target_color = "SL", placed_sl, "red"
    elif r == "TP" or r.startswith("TP"):
        label, target, target_color = "TP", placed_tp, "green"
    else:
        return None
    if target is None:
        return None
    target_f = float(target)
    entry_f = float(entry_price or 0)
    exit_f = float(exit_price or 0)
    if target_f <= 0 or entry_f <= 0:
        return None
    is_long = str(side).lower().startswith("l")
    slip_abs = exit_f - target_f if is_long else target_f - exit_f
    slip_pct = slip_abs / entry_f * 100.0
    sl_pct_s, tp_pct_s = _sl_tp_pct_from_entry(
        side,
        entry_f,
        float(placed_sl) if placed_sl is not None else target_f,
        float(placed_tp) if placed_tp is not None else target_f,
    )
    target_pct = tp_pct_s if label == "TP" else sl_pct_s
    favorable = slip_abs >= 0
    return {
        "label": label,
        "target_color": target_color,
        "target_px": _log_price(sym, target_f),
        "target_pct": target_pct,
        "slip_pct_str": f"{slip_pct:+.3f}%",
        "slip_delta_str": _signed_price_delta(sym, slip_abs),
        "slip_color": "green" if favorable else "red",
    }


def _log_print_block(*lines: str) -> None:
    for line in lines:
        print(line)


def _log_entry_open(
    *,
    mode: str,
    tab: str,
    sym: str,
    side: str,
    pos_side: str,
    entry_time_iso: str,
    fill_price: float,
    qty: float,
    placed_sl: float,
    placed_tp: float,
    signal_ep: float | None = None,
    signal_sl: float | None = None,
    signal_tp: float | None = None,
    protection: str = "exchange",
    leverage: int | None = None,
    notional_usd: float | None = None,
    fill_source: str | None = None,
) -> None:
    """Structured multi-line entry log (terminal + file via print hook)."""
    ts = _utc_log_stamp(entry_time_iso)
    sl_pct, tp_pct = _sl_tp_pct_from_entry(side, fill_price, placed_sl, placed_tp)
    filled_px = _log_price(sym, fill_price)
    sl_px = _log_price(sym, placed_sl)
    tp_px = _log_price(sym, placed_tp)
    sep = _c("-" * 56, "dim")
    head = _c(f"[ENTRY | {mode}]", "cyan")
    side_badge = _c(side.upper(), "green" if side == "Long" else "red")
    prot = protection.replace("_", " ")
    lines = [
        sep,
        f"{head}  {_c(tab, 'bold')}  {sym}  {side_badge}  ({pos_side})  |  {prot}",
        f"  {_c('Time', 'dim'):<6} {ts}",
    ]
    if signal_ep is not None and signal_sl is not None and signal_tp is not None:
        sig_ep = _log_price(sym, signal_ep)
        sig_sl = _log_price(sym, signal_sl)
        sig_tp = _log_price(sym, signal_tp)
        lines.append(
            f"  {_c('Signal', 'dim'):<6} ep {sig_ep}  sl {sig_sl}  tp {sig_tp}"
        )
    filled_tail = (
        f"  {_c('Filled', 'bold'):<6} {filled_px}  x  {qty:g}"
        + (f"  (~${notional_usd:.2f})" if notional_usd else "")
        + (f"  lev {leverage}x" if leverage else "")
    )
    if fill_source:
        filled_tail += f"  [{fill_source}]"
    if signal_ep is not None and float(signal_ep) > 0 and float(fill_price) > 0:
        drift = (float(fill_price) - float(signal_ep)) / float(signal_ep) * 100.0
        if abs(drift) >= 0.005:
            drift_color = "yellow" if abs(drift) < 1.0 else "red"
            filled_tail += f"  vs ep {_c(f'{drift:+.2f}%', drift_color)}"
    lines.append(filled_tail)
    lines.extend([
        f"  {_c('SL', 'red'):<6} {sl_px}  ({_c(sl_pct, 'red')} from filled)",
        f"  {_c('TP', 'green'):<6} {tp_px}  ({_c(tp_pct, 'green')} from filled)",
    ])
    lines.append(sep)
    _log_print_block(*lines)


def _log_exit_close(
    *,
    pos_key: str,
    sym: str,
    tab: str,
    side: str,
    reason: str,
    entry_time_iso: str | None,
    exit_time_iso: str | None,
    entry_price: float,
    exit_price: float,
    net_pnl: float,
    fee_usd: float = 0.0,
    placed_sl: float | None = None,
    placed_tp: float | None = None,
) -> None:
    ts = _utc_log_stamp(exit_time_iso)
    ent_ts = _utc_log_stamp(entry_time_iso) if entry_time_iso else "—"
    pnl_color = "green" if net_pnl >= 0 else "red"
    move_pct, move_color = _exit_move_pct_from_entry(side, entry_price, exit_price)
    sep = _c("-" * 56, "dim")
    head = _c("[EXIT]", "green" if net_pnl >= 0 else "red")
    lines = [
        sep,
        f"{head}  {sym}  {_c(tab, 'bold')}  {side}  |  {reason}",
        f"  {_c('Opened', 'dim'):<6} {ent_ts}",
        f"  {_c('Closed', 'dim'):<6} {ts}",
        f"  {_c('Prices', 'dim'):<6} entry {_log_price(sym, entry_price)} -> exit {_log_price(sym, exit_price)}"
        f"  ({_c(move_pct, move_color)})",
    ]
    slip_info = _exit_target_slip_from_fill(
        reason=reason,
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        placed_sl=placed_sl,
        placed_tp=placed_tp,
        sym=sym,
    )
    if slip_info:
        tc = slip_info["target_color"]
        lines.append(
            f"  {_c('Target', 'dim'):<6} "
            f"{_c(slip_info['label'], tc)} {slip_info['target_px']}  "
            f"({_c(slip_info['target_pct'], tc)} from fill)  |  "
            f"slip {_c(slip_info['slip_pct_str'], slip_info['slip_color'])}  "
            f"({slip_info['slip_delta_str']})"
        )
    lines.append(
        f"  {_c('PnL', 'dim'):<6} {_c(f'${net_pnl:+.2f}', pnl_color)}"
        + f"  fee ${_c(f'{fee_usd:.3f}', 'dim')}"
    )
    lines.append(sep)
    _log_print_block(*lines)


def _is_noisy_http_line(line: str) -> bool:
    lower = line.lower()
    if '"get ' not in lower and '"post ' not in lower:
        return False
    return any(p in lower for p in _NOISY_HTTP_PATHS)


def _is_harmless_dashboard_ws_log(record: logging.LogRecord) -> bool:
    """Suppress uvicorn/websockets noise when a dashboard tab closes mid-ping."""
    msg = record.getMessage().lower()
    if "keepalive ping failed" not in msg and "data transfer failed" not in msg:
        return False
    exc = record.exc_info[1] if record.exc_info else None
    if isinstance(exc, AssertionError):
        return True
    if exc is not None:
        name = type(exc).__name__
        if "ConnectionClosed" in name:
            return True
    return "assert waiter is none" in msg


def _format_http_access_line(line: str) -> str:
    m = re.search(
        r'"(GET|POST|PUT|DELETE|PATCH|OPTIONS|HEAD)\s+([^\s]+)\s+HTTP/[^"]*"\s+(\d{3})',
        line,
    )
    if not m:
        return _c(line, "dim")
    method, path, status = m.group(1), m.group(2), m.group(3)
    st_color = "green" if status.startswith("2") else "yellow" if status.startswith("3") else "red"
    prefix = line.split('"', 1)[0].strip()
    return (
        f"{_c(prefix, 'dim')} "
        f"{_c(method, 'bold')} {_c(path, 'cyan')} "
        f"{_c(status, st_color)}"
    )


def _highlight_exit_move_pct(line: str) -> str:
    """Color กำไร/ขาดทุn % on terminal when source line has no ANSI yet."""
    plain = _strip_ansi(line)
    m = re.search(r"(กำไร|ขาดทุน)\s*([+-][\d.]+%)", plain)
    if not m:
        return line
    token = m.group(0)
    if plain != line:
        return line
    color = "green" if m.group(1) == "กำไร" else "red"
    return plain.replace(token, _c(token, color), 1)


def _format_bot_line(line: str) -> str:
    plain = _strip_ansi(line)
    if plain != line:
        return line

    if plain.startswith("Connected to Binance"):
        return _c(plain, "green")
    if plain.startswith("WS stale:") or plain.startswith("WS Error:"):
        return _c(plain, "yellow")
    if plain.startswith("Top ") and "loaded" in plain:
        return _c(plain, "cyan")

    m = re.match(r"^(\[[^\]]+\])(.*)$", plain)
    if not m:
        return _highlight_exit_move_pct(plain)

    tag, rest = m.group(1), m.group(2)
    inner = tag[1:-1]
    color = "dim"
    for key, c in _TAG_COLORS.items():
        if inner.startswith(key) or key in inner:
            color = c
            break
    if "ENTRY" in inner.upper() or inner.endswith("ENTRY") or "ENTRY |" in tag:
        color = "cyan"
    if inner == "EXIT" or inner.startswith("EXIT"):
        plain_rest = _strip_ansi(rest)
        if "ขาดทุน" in plain_rest or "Net PnL: $-" in plain_rest or "Net PnL: -" in plain_rest:
            color = "red"
        elif "กำไร" in plain_rest or "Net PnL:" in plain_rest:
            color = "green"

    styled_rest = rest
    if "Net PnL:" in rest:
        pnl_m = re.search(r"Net PnL:\s*(\$?-?[\d.]+)", rest)
        if pnl_m:
            val = pnl_m.group(1)
            pnl_color = "red" if val.startswith("-") or val.startswith("$-") else "green"
            styled_rest = rest.replace(val, _c(val, pnl_color), 1)
    elif inner == "Income Sync":
        net_m = re.search(r"net=\$(-?[\d.]+)", styled_rest)
        if net_m:
            net_val = net_m.group(1)
            net_color = "red" if net_val.startswith("-") else "green"
            styled_rest = styled_rest.replace(
                net_m.group(0), _c(net_m.group(0), net_color), 1,
            )

    styled = _highlight_exit_move_pct(f"{_c(tag, color)}{styled_rest}")
    if styled != f"{_c(tag, color)}{styled_rest}":
        return styled

    return f"{_c(tag, color)}{styled_rest}"


def _format_terminal_line(line: str) -> str:
    if _is_noisy_http_line(line):
        return ""
    if " 127.0.0.1:" in line and '"GET ' in line:
        return _format_http_access_line(line)
    if line.startswith("INFO:") and "uvicorn" in line.lower():
        return _c(line, "dim")
    return _format_bot_line(line)


def _buf_core(line: str) -> None:
    if _is_noisy_http_line(line):
        return
    _log_buffer.append({"t": datetime.now().strftime("%H:%M:%S"), "msg": line})


_orig_print = builtins.print


def _captured_print(*args, **kwargs):
    line = " ".join(str(a) for a in args)
    _buf(line)
    display = _format_terminal_line(line)
    if not display:
        return
    out_kw = {k: v for k, v in kwargs.items() if k != "file"}
    out_kw.setdefault("flush", True)
    try:
        _orig_print(display, **out_kw)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = display.encode(enc, "replace").decode(enc)
        _orig_print(safe, **out_kw)


def install_print_hook() -> None:
    builtins.print = _captured_print
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass


class _LogBufHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            if _is_noisy_http_line(msg):
                return
            _buf(msg)
        except Exception:
            pass


class _HarmlessWsDisconnectFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_harmless_dashboard_ws_log(record)


class _PrettyConsoleHandler(logging.Handler):
    """Colorized stderr for uvicorn/websockets without polluting the dashboard buffer."""

    def emit(self, record):
        try:
            if _is_harmless_dashboard_ws_log(record):
                return
            msg = self.format(record)
            if _is_noisy_http_line(msg):
                return
            display = _format_terminal_line(msg)
            if display:
                _orig_print(display, flush=True)
        except Exception:
            pass


def _configure_library_loggers() -> None:
    plain = logging.Formatter("%(message)s")
    err_fmt = logging.Formatter("%(levelname)s %(name)s: %(message)s")

    buf_h = _LogBufHandler()
    buf_h.setFormatter(plain)

    access_h = _PrettyConsoleHandler()
    access_h.setFormatter(plain)
    access_log = logging.getLogger("uvicorn.access")
    access_log.handlers.clear()
    access_log.addHandler(access_h)
    access_log.addHandler(buf_h)
    access_log.setLevel(logging.INFO)
    access_log.propagate = False

    err_h = _PrettyConsoleHandler()
    err_h.setFormatter(err_fmt)
    err_log = logging.getLogger("uvicorn.error")
    err_log.handlers.clear()
    err_log.addFilter(_HarmlessWsDisconnectFilter())
    err_log.addHandler(err_h)
    err_log.addHandler(buf_h)
    err_log.propagate = False

    ws_log = logging.getLogger("websockets")
    ws_log.handlers.clear()
    ws_h = _PrettyConsoleHandler()
    ws_h.setFormatter(err_fmt)
    ws_log.addHandler(ws_h)
    ws_log.addHandler(buf_h)
    ws_log.setLevel(logging.WARNING)
    ws_log.propagate = False


class _SafeRotatingFileHandler(RotatingFileHandler):
    def doRollover(self):
        try:
            super().doRollover()
        except (OSError, PermissionError):
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                with open(self.baseFilename, "w", encoding="utf-8"):
                    pass
                self.stream = self._open()
            except Exception:
                pass


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_ARCHIVE_DIR = os.path.join(_PROJECT_ROOT, "archive", "server_logs")
os.makedirs(LOG_ARCHIVE_DIR, exist_ok=True)
_file_log = _SafeRotatingFileHandler(
    os.path.join(LOG_ARCHIVE_DIR, "server_log.txt"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_log.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.raiseExceptions = False
_file_log.setLevel(logging.INFO)
_file_logger = logging.getLogger("server.file")
_file_logger.setLevel(logging.INFO)
_file_logger.propagate = False
_file_logger.addHandler(_file_log)

_ERROR_MARKERS = ("[ERROR]", "ERROR ", "Traceback", "Exception", "Failed", "CRITICAL", "[WARN]", "⚠️")
_ERROR_IGNORE = ("[Telegram] Send error",)
_tg_err_recent: dict[str, float] = {}
_TG_ERR_DEDUP_SEC = 120.0


def _forward_error_to_telegram(line: str) -> None:
    if not any(m in line for m in _ERROR_MARKERS):
        return
    if any(m in line for m in _ERROR_IGNORE):
        return
    import time as _t
    now = _t.time()
    for k in [k for k, ts in _tg_err_recent.items() if now - ts > _TG_ERR_DEDUP_SEC]:
        _tg_err_recent.pop(k, None)
    key = line[:180]
    if key in _tg_err_recent:
        return
    _tg_err_recent[key] = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    snippet = line if len(line) <= 1500 else line[:1500] + "…"
    if _send_telegram_hook is not None:
        loop.create_task(_send_telegram_hook(f"🚨 <b>ERROR</b>\n<pre>{snippet}</pre>", is_error=True))
    if _record_error_event_hook is not None:
        loop.create_task(_record_error_event_hook(line, severity="error", source="log", notify=False))


def _buf(line: str):
    plain = _strip_ansi(line)
    _buf_core(plain)
    try:
        _file_logger.info(plain)
    except Exception:
        pass
    try:
        _forward_error_to_telegram(plain)
    except Exception:
        pass


def print_startup_banner(
    *,
    effective_sltp_mode: Callable[[], str],
    max_open_algo_orders: Callable[[], int],
    effective_kline_fetch_delay_sec: Callable[[], float],
    tg_tp_close: str,
    tg_profit_close: str,
    tg_loss_close: str,
) -> None:
    mode = "LIVE" if LIVE_MODE else "PAPER"
    env = f"order={ORDER_ENV} feed={PRICE_FEED_ENV}"
    tabs = ", ".join(sorted(STARTUP_ENABLED_TABS))
    sep = _c("─" * 56, "dim")
    print(sep)
    print(f"  {_c('Antigravity', 'bold')} {_c(mode, 'green' if LIVE_MODE else 'cyan')}  {_c(env, 'dim')}")
    print(f"  {_c('Dashboard', 'dim')}: http://localhost:{DASHBOARD_PORT}")
    if DASHBOARD_PORT == 6000:
        print(
            f"  {_c('Warning', 'yellow')}: Chrome/Edge block port 6000 (ERR_UNSAFE_PORT). "
            f"Set DASHBOARD_PORT=8765 in .env and restart."
        )
    print(f"  {_c('Default tabs', 'dim')}: {tabs}")
    if LIVE_MODE:
        print(f"  {_c('SL/TP mode', 'yellow')}: {effective_sltp_mode()} (algo cap {max_open_algo_orders()})")
    if TELEGRAM_ENABLED:
        print(
            f"  {_c('Telegram', 'dim')}: errors only"
        )
    if LIVE_MODE:
        if BINANCE_CLOSE_HISTORY_ENABLED:
            print(
                f"  {_c('Close history', 'dim')}: Binance userTrades REST "
                f"(~{BINANCE_CLOSE_HISTORY_DAYS}d, round-robin)"
            )
        else:
            print(
                f"  {_c('Close history', 'dim')}: bot state (no bulk userTrades REST; "
                f"account PnL via income sync)"
            )
    print(
        f"  {_c('Candle schedule', 'dim')}: scan +{effective_kline_fetch_delay_sec()}s UTC | "
        f"entries staggered {ENTRY_STAGGER_SEC:g}s apart | "
        f"-4192 retry: +{ENTRY_4192_RETRY_DELAY_SEC}s then {ENTRY_TRIGGER_PRICE} at-or-better ep (poll {ENTRY_4192_PRICE_POLL_SEC:g}s)"
    )
    if ENTRY_WAIT_FOR_BETTER_PRICE:
        improve_note = (
            f" (min {ENTRY_MIN_PRICE_IMPROVE_PCT * 100:.2g}% better than ep)"
            if ENTRY_MIN_PRICE_IMPROVE_PCT > 0
            else ""
        )
        print(
            f"  {_c('Entry wait', 'dim')}: {ENTRY_TRIGGER_PRICE} price better than ep before fill{improve_note} "
            f"(poll {ENTRY_PRICE_POLL_SEC:g}s, skip after {ENTRY_PRICE_WAIT_MAX_SEC}s)"
        )
    if ENTRY_LOCAL_SL_GRACE_SEC > 0:
        print(
            f"  {_c('Local SL grace', 'dim')}: no bot-managed SL/TP for "
            f"{ENTRY_LOCAL_SL_GRACE_SEC:g}s after live entry"
        )
    print(sep)


def init_logging() -> None:
    install_print_hook()
    _configure_library_loggers()


init_logging()
