"""
signalrunner/backtest.py

Replays a strategy over historical BVC data and measures how its signals would
have performed. A backtest is a loop over historical sessions: on each day, look
only at data available up to that day (no look-ahead), evaluate the strategy's
indicator conditions, and when a signal fires, measure the forward return.

Two exit modes are measured for every signal:
  - horizon:  return after a fixed N trading days
  - tp/sl:    return if a take-profit or stop-loss would have triggered first

IMPORTANT CAVEATS (surfaced in the UI):
  - Uses closing prices only. No slippage, spreads, taxes, or commissions.
  - Real-world returns will be LOWER, sometimes much lower on illiquid names.
  - Past performance does not predict future results.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from django.utils import timezone as djtz
from django_q.tasks import async_task

from signalrunner import datasource
from signalrunner.models import (
    Backtest, BacktestSignal, BacktestStatus, SignalDirection,
)

MIN_HISTORY_BARS = 200  # need enough history for SMA200 etc.


def run_backtest(backtest_id: str) -> None:
    """Execute one backtest end to end. Enqueued on the django-q2 worker."""
    bt = Backtest.objects.select_related("strategy").get(id=backtest_id)
    if bt.status != BacktestStatus.QUEUED:
        return

    started = time.monotonic()
    bt.status = BacktestStatus.RUNNING
    bt.log = []
    bt.save(update_fields=["status", "log"])

    try:
        _execute(bt)
    except Exception as exc:
        bt.status = BacktestStatus.FAILED
        bt.error = repr(exc)
        bt.finished_at = djtz.now()
        bt.duration_ms = int((time.monotonic() - started) * 1000)
        _log(bt, f"[ERROR] {type(exc).__name__}: {exc}")
        bt.save(update_fields=["status", "error", "finished_at", "duration_ms", "log"])
        raise

    bt.status = BacktestStatus.SUCCESS
    bt.finished_at = djtz.now()
    bt.duration_ms = int((time.monotonic() - started) * 1000)
    _log(bt, f"[OK] backtest complete in {bt.duration_ms} ms")
    bt.save(update_fields=["status", "finished_at", "duration_ms", "log",
                           "stats", "equity_curve"])


def _execute(bt: Backtest) -> None:
    cfg = bt.config_snapshot or {}
    tickers = [t.upper() for t in cfg.get("tickers", [])]
    if not tickers:
        raise ValueError("strategy has no tickers")

    _log(bt, f"[INFO] backtesting {bt.strategy_name} on {tickers} "
             f"from {bt.start_date} to {bt.end_date}")

    all_signals: list[BacktestSignal] = []
    # Buffer past the end date so horizon exits can be measured.
    fetch_start = bt.start_date.isoformat()
    fetch_end = bt.end_date.isoformat()

    for ticker in tickers:
        try:
            bars = _load_bars(ticker, fetch_start, fetch_end)
        except datasource.DataSourceError as exc:
            _log(bt, f"[WARN] {ticker}: no history ({exc})")
            continue
        if len(bars) < MIN_HISTORY_BARS:
            _log(bt, f"[WARN] {ticker}: only {len(bars)} bars, need {MIN_HISTORY_BARS}; skipping")
            continue

        sigs = _backtest_ticker(bt, ticker, bars, cfg)
        all_signals.extend(sigs)
        _log(bt, f"[INFO] {ticker}: {len(sigs)} signal(s)")

    BacktestSignal.objects.bulk_create(all_signals)
    bt.stats = _compute_stats(all_signals, bt)
    bt.equity_curve = _build_equity_curve(all_signals)
    _log(bt, f"[OK] {len(all_signals)} total signals; "
             f"win rate {bt.stats.get('win_rate', 0):.1f}%")


# ──────────────────────────────────────────────
# Per-ticker replay
# ──────────────────────────────────────────────
def _backtest_ticker(bt, ticker, bars, cfg) -> list[BacktestSignal]:
    """Walk forward through bars; evaluate the strategy at each session using
    only data up to that bar; measure forward return for each fired signal."""
    indicator = (cfg.get("indicator") or "rsi").lower()
    horizon = bt.horizon_days
    signals: list[BacktestSignal] = []

    closes = [b["close"] for b in bars]
    dates = [b["date"] for b in bars]

    # Start once we have enough history; stop leaving room for the horizon.
    for i in range(MIN_HISTORY_BARS, len(bars) - horizon):
        window = closes[: i + 1]   # data available "as of" session i (no look-ahead)
        decision = _evaluate_indicator(indicator, window, cfg)
        if not decision:
            continue
        direction, reason = decision
        entry = closes[i]
        if not entry:
            continue

        # Exit measurement: horizon and (optionally) tp/sl
        exit_price, exit_date, exit_kind = _measure_exit(
            closes, dates, i, direction, horizon,
            bt.take_profit_pct, bt.stop_loss_pct
        )
        ret = (exit_price - entry) / entry * 100
        if direction == SignalDirection.SELL:
            ret = -ret  # a short: profit when price falls
        won = ret > 0

        signals.append(BacktestSignal(
            backtest=bt, ticker=ticker, direction=direction,
            session_date=dates[i], entry_price=entry,
            exit_price=exit_price, exit_date=exit_date,
            return_pct=round(ret, 3), won=won, exit_kind=exit_kind, reason=reason,
        ))
    return signals


def _measure_exit(closes, dates, i, direction, horizon, tp, sl):
    """Return (exit_price, exit_date, exit_kind). If tp/sl set, exit at whichever
    triggers first within the horizon; otherwise exit at the horizon close."""
    entry = closes[i]
    if tp or sl:
        for j in range(i + 1, min(i + 1 + horizon, len(closes))):
            move = (closes[j] - entry) / entry * 100
            if direction == SignalDirection.SELL:
                move = -move
            if tp and move >= tp:
                return closes[j], dates[j], "take_profit"
            if sl and move <= -sl:
                return closes[j], dates[j], "stop_loss"
    # Default: horizon exit
    k = min(i + horizon, len(closes) - 1)
    return closes[k], dates[k], "horizon"


# ──────────────────────────────────────────────
# Indicator evaluation (backtest variants — work on a closes window)
# ──────────────────────────────────────────────
def _evaluate_indicator(indicator, closes, cfg):
    """Return (direction, reason) if the condition fires on the last bar, else None."""
    if indicator == "rsi":
        period = int(cfg.get("period", 14))
        if len(closes) < period + 1:
            return None
        rsi = _rsi(closes, period)
        op = cfg.get("op", "<"); thr = cfg.get("value", 30)
        direction = cfg.get("direction", "buy")
        if _cmp(rsi, op, thr):
            return direction, {"rsi": round(rsi, 2), "op": op, "threshold": thr}
        return None

    if indicator in ("ma_cross", "ma", "sma_cross"):
        fast = int(cfg.get("fast", 20)); slow = int(cfg.get("slow", 50))
        if len(closes) < slow + 1:
            return None
        f, s = _sma(closes, fast), _sma(closes, slow)
        fp, sp = _sma(closes[:-1], fast), _sma(closes[:-1], slow)
        if None in (f, s, fp, sp):
            return None
        if fp <= sp and f > s:
            return "buy", {"cross": "golden", "fast": round(f, 2), "slow": round(s, 2)}
        if fp >= sp and f < s:
            return "sell", {"cross": "death", "fast": round(f, 2), "slow": round(s, 2)}
        return None

    if indicator == "macd":
        fast = int(cfg.get("fast", 12)); slow = int(cfg.get("slow", 26))
        if len(closes) < slow + 2:
            return None
        macd = _ema(closes, fast) - _ema(closes, slow)
        macd_p = _ema(closes[:-1], fast) - _ema(closes[:-1], slow)
        if macd_p <= 0 and macd > 0:
            return "buy", {"macd": round(macd, 4)}
        if macd_p >= 0 and macd < 0:
            return "sell", {"macd": round(macd, 4)}
        return None

    if indicator == "rsi_above_ma":
        # Compound: RSI < threshold AND price > SMA(ma_period).
        # Buys dips in uptrends only — avoids catching falling knives.
        rsi_period = int(cfg.get("period", 14))
        ma_period = int(cfg.get("ma_period", 50))
        rsi_thr = cfg.get("value", 30)
        if len(closes) < max(rsi_period + 1, ma_period):
            return None
        rsi = _rsi(closes, rsi_period)
        sma = _sma(closes, ma_period)
        price = closes[-1]
        if rsi < rsi_thr and price > sma:
            return "buy", {
                "rsi": round(rsi, 2), "rsi_threshold": rsi_thr,
                "price": round(price, 2), "sma": round(sma, 2),
                "filter": f"price > SMA{ma_period}",
            }
        return None

    return None


# ──────────────────────────────────────────────
# Statistics (thorough)
# ──────────────────────────────────────────────
def _compute_stats(signals, bt) -> dict:
    if not signals:
        return {"signal_count": 0, "win_rate": 0, "avg_return": 0,
                "total_return": 0, "profit_factor": 0, "max_drawdown": 0,
                "sharpe": 0, "buy_count": 0, "sell_count": 0,
                "note": "No signals fired in this period."}

    returns = [s.return_pct for s in signals if s.return_pct is not None]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    n = len(returns)

    win_rate = len(wins) / n * 100 if n else 0
    avg_return = sum(returns) / n if n else 0
    total_return = sum(returns)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss else (999.0 if gross_win else 0)

    # Equity-based max drawdown (cumulative sum of returns)
    cum = 0.0; peak = 0.0; max_dd = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)

    # Sharpe-like: mean / std of per-signal returns (not annualized — descriptive)
    if n > 1:
        mean = avg_return
        var = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = var ** 0.5
        sharpe = (mean / std) if std else 0
    else:
        sharpe = 0

    return {
        "signal_count": n,
        "buy_count": sum(1 for s in signals if s.direction == "buy"),
        "sell_count": sum(1 for s in signals if s.direction == "sell"),
        "win_rate": round(win_rate, 1),
        "avg_return": round(avg_return, 3),
        "total_return": round(total_return, 2),
        "best": round(max(returns), 2),
        "worst": round(min(returns), 2),
        "profit_factor": round(profit_factor, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 3),
        "horizon_days": bt.horizon_days,
        "exit_breakdown": _exit_breakdown(signals),
        "caveat": ("Closing prices only — no slippage, spreads, or fees. "
                   "Real returns will be lower. Not financial advice."),
    }


def _exit_breakdown(signals):
    out = {}
    for s in signals:
        out[s.exit_kind] = out.get(s.exit_kind, 0) + 1
    return out


def _build_equity_curve(signals):
    """Cumulative return over time, ordered by signal date."""
    ordered = sorted([s for s in signals if s.return_pct is not None],
                     key=lambda s: s.session_date)
    curve = []
    cum = 0.0
    for s in ordered:
        cum += s.return_pct
        curve.append({"date": s.session_date.isoformat(), "equity": round(cum, 2)})
    return curve


# ──────────────────────────────────────────────
# Data loading
# ──────────────────────────────────────────────
def _load_bars(ticker, start, end) -> list[dict]:
    """Load historical bars as a list of {date, close} dicts, oldest first."""
    df = datasource.get_history(ticker, start, end)
    if df is None:
        raise datasource.DataSourceError(f"no data for {ticker}")
    return _df_to_bars(df)


def _df_to_bars(df) -> list[dict]:
    """Normalize a provider DataFrame to [{date, close}], tolerant of column names."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
    except AttributeError:
        return []
    close_col = next((cols[c] for c in ("close", "cloture", "clôture", "last",
                                         "dernier cours", "price") if c in cols), None)
    if close_col is None:
        import pandas as pd
        num = df.select_dtypes("number")
        if num.shape[1] == 0:
            return []
        close_col = num.columns[-1]

    # Date: prefer the index if it's datetime, else a date column
    bars = []
    date_col = next((cols[c] for c in ("date", "created", "séance", "seance") if c in cols), None)
    for idx, row in df.iterrows():
        try:
            close = float(row[close_col])
        except (TypeError, ValueError):
            continue
        if date_col is not None:
            d = row[date_col]
        else:
            d = idx
        d = _to_date(d)
        if d is None:
            continue
        bars.append({"date": d, "close": close})
    bars.sort(key=lambda b: b["date"])
    return bars


def _to_date(v):
    from datetime import date, datetime as dt
    if isinstance(v, date) and not isinstance(v, dt):
        return v
    if isinstance(v, dt):
        return v.date()
    try:
        return dt.fromisoformat(str(v)[:10]).date()
    except ValueError:
        try:
            import pandas as pd
            return pd.to_datetime(v).date()
        except Exception:
            return None


# ──────────────────────────────────────────────
# Indicator math (shared with tasks.py logic)
# ──────────────────────────────────────────────
def _cmp(a, op, b):
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "==": a == b}.get(op, False)


def _rsi(closes, period):
    gains = losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0); losses += max(-diff, 0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def _sma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ema(closes, n):
    if len(closes) < n:
        return closes[-1] if closes else 0
    k = 2 / (n + 1)
    ema = closes[-n]
    for price in closes[-n + 1:]:
        ema = price * k + ema * (1 - k)
    return ema


def _log(bt, line):
    bt.log = (bt.log or []) + [line]
    print(line)


# ──────────────────────────────────────────────
# Entry helper (called by the view)
# ──────────────────────────────────────────────
def start_backtest(strategy, start_date, end_date, *, horizon_days=5,
                   take_profit_pct=None, stop_loss_pct=None) -> Backtest:
    """Create a Backtest row snapshotting the strategy, and enqueue it."""
    bt = Backtest.objects.create(
        strategy=strategy,
        strategy_name=strategy.name,
        config_snapshot={**strategy.config, "tickers": strategy.tickers,
                         "kind": strategy.kind},
        start_date=start_date, end_date=end_date,
        horizon_days=horizon_days,
        take_profit_pct=take_profit_pct, stop_loss_pct=stop_loss_pct,
        status=BacktestStatus.QUEUED,
    )
    async_task("signalrunner.backtest.run_backtest", str(bt.id))
    return bt
