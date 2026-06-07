"""
signalrunner/tasks.py

The evaluation worker — SignalRunner's equivalent of PyRunner's run executor.
Every trigger (scheduled / on-demand / webhook) enqueues `run_evaluation(eval_id)`;
nothing evaluates inline in a request.

Flow: QUEUED -> RUNNING -> fetch data -> evaluate by kind -> emit Signals ->
SUCCESS|FAILED -> enqueue Telegram deliveries for any fired signals.

This module owns ALL Evaluation status transitions.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from django_q.tasks import async_task

from signalrunner import datasource
from signalrunner.models import (
    Evaluation, Signal, Strategy,
    EvaluationStatus, StrategyKind, SignalDirection,
)

CUSTOM_TIMEOUT_SECONDS = 30


# ──────────────────────────────────────────────
# Entry point (enqueued with the evaluation's UUID)
# ──────────────────────────────────────────────
def run_evaluation(eval_id: str) -> None:
    """Execute one evaluation end to end. Idempotent on retry."""
    ev = Evaluation.objects.select_related("strategy").get(id=eval_id)
    if ev.status != EvaluationStatus.QUEUED:
        _log(ev, f"[WARN] run_evaluation on status={ev.status}; skipping")
        return
    if ev.strategy is None:
        _finalize(ev, EvaluationStatus.FAILED, time.monotonic())
        _log(ev, "[ERROR] strategy was deleted before evaluation ran")
        ev.save(update_fields=["log"])
        return

    started = time.monotonic()
    ev.status = EvaluationStatus.RUNNING
    ev.started_at = _now()
    ev.log = []
    ev.save(update_fields=["status", "started_at", "log"])
    strategy = ev.strategy
    _log(ev, f"[INFO] Evaluating '{strategy.name}' [{strategy.kind}] on {strategy.tickers}")

    try:
        quotes = datasource.get_quotes(strategy.tickers)
        ev.computed = {tk: {k: v for k, v in q.items() if k != "raw"}
                       for tk, q in quotes.items()}
        _log(ev, f"[INFO] fetched {len(quotes)} quote(s)")

        signals = _evaluate(strategy, quotes, ev)
    except datasource.DataSourceError as exc:
        _finalize(ev, EvaluationStatus.FAILED, started)
        ev.error = str(exc)
        _log(ev, f"[ERROR] data: {exc}")
        ev.save(update_fields=["error", "log", "computed"])
        return
    except Exception as exc:
        _finalize(ev, EvaluationStatus.FAILED, started)
        ev.error = repr(exc)
        _log(ev, f"[ERROR] {type(exc).__name__}: {exc}")
        ev.save(update_fields=["error", "log", "computed"])
        raise

    ev.fired = bool(signals)
    _finalize(ev, EvaluationStatus.SUCCESS, started)
    _log(ev, f"[OK] done: {len(signals)} signal(s) in {ev.duration_ms} ms")
    ev.save(update_fields=["fired", "computed", "log"])

    for sig in signals:
        _enqueue_delivery(sig, ev)


# ──────────────────────────────────────────────
# Evaluation dispatch
# ──────────────────────────────────────────────
def _evaluate(strategy: Strategy, quotes: dict, ev: Evaluation) -> list[Signal]:
    if strategy.kind == StrategyKind.RULE:
        return _eval_rule(strategy, quotes, ev)
    if strategy.kind == StrategyKind.INDICATOR:
        return _eval_indicator(strategy, quotes, ev)
    if strategy.kind == StrategyKind.CUSTOM:
        return _eval_custom(strategy, quotes, ev)
    raise ValueError(f"unknown strategy kind: {strategy.kind}")


# ── Rule: threshold / % move ──
_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}


def _eval_rule(strategy, quotes, ev) -> list[Signal]:
    cfg = strategy.config or {}
    field = cfg.get("field", "price")        # price | pct_change | volume
    op = cfg.get("op", ">")
    value = cfg.get("value")
    direction = cfg.get("direction", "buy")
    if value is None or op not in _OPS:
        _log(ev, f"[WARN] rule misconfigured: op={op} value={value}")
        return []

    fired = []
    for tk, q in quotes.items():
        actual = q.get(field)
        if actual is None:
            continue
        if _OPS[op](actual, value):
            fired.append(_make_signal(ev, strategy, tk, direction,
                         {"field": field, "actual": actual, "op": op, "value": value},
                         q.get("price")))
            _log(ev, f"[OK] {tk}: {field}={actual} {op} {value} -> {direction}")
    return fired


# ── Indicator: RSI / MACD / MA crossover ──
def _eval_indicator(strategy, quotes, ev) -> list[Signal]:
    cfg = strategy.config or {}
    indicator = (cfg.get("indicator") or "rsi").lower()
    fired = []
    for tk in strategy.tickers:
        tk = tk.upper()
        try:
            values = _compute_indicator(tk, indicator, cfg)
        except datasource.DataSourceError as exc:
            _log(ev, f"[WARN] {tk}: history unavailable ({exc})")
            continue
        if values is None:
            continue
        decision = _indicator_decision(indicator, values, cfg)
        # record what we computed for this ticker
        ev.computed.setdefault(tk, {}).update(values)
        if decision:
            direction, reason = decision
            price = (quotes.get(tk) or {}).get("price")
            fired.append(_make_signal(ev, strategy, tk, direction, reason, price))
            _log(ev, f"[OK] {tk}: {indicator} -> {direction} ({reason})")
    return fired


def _compute_indicator(ticker, indicator, cfg) -> dict | None:
    """Compute indicator values from price history. Uses pandas + a TA helper."""
    import pandas as pd
    # Pull ~6 months of history; enough for the default periods.
    end = datetime.now(timezone.utc).date().isoformat()
    start = (datetime.now(timezone.utc).date().replace(
        year=datetime.now(timezone.utc).year)).isoformat()
    df = datasource.get_history(ticker, _six_months_ago(), end)
    closes = _close_series(df)
    if closes is None or len(closes) < 30:
        return None

    if indicator == "rsi":
        period = int(cfg.get("period", 14))
        return {"rsi": _rsi(closes, period)}
    if indicator in ("ma_cross", "ma", "sma_cross"):
        fast = int(cfg.get("fast", 20)); slow = int(cfg.get("slow", 50))
        return {"ma_fast": _sma(closes, fast), "ma_slow": _sma(closes, slow),
                "ma_fast_prev": _sma(closes[:-1], fast),
                "ma_slow_prev": _sma(closes[:-1], slow)}
    if indicator == "macd":
        return _macd(closes, int(cfg.get("fast", 12)), int(cfg.get("slow", 26)),
                     int(cfg.get("signal", 9)))

    if indicator == "volume_spike":
        # Volume anomaly: fires when today's volume > multiplier × N-day average.
        # Unusual volume on BVC often precedes major news or a move worth watching.
        period = int(cfg.get("period", 20))
        df = datasource.get_history(ticker, _six_months_ago(), end)
        vols = _volume_series(df)
        if vols is None or len(vols) < period + 1:
            return None
        avg_vol = sum(vols[-(period + 1):-1]) / period
        today_vol = vols[-1]
        if avg_vol <= 0:
            return None
        ratio = today_vol / avg_vol
        return {"volume_today": today_vol, "volume_avg": round(avg_vol, 0),
                "volume_ratio": round(ratio, 2)}

    if indicator == "momentum":
        # N-day price momentum for live monitoring.
        lookback = int(cfg.get("lookback", 63))
        if len(closes) < lookback + 1:
            return None
        past = closes[-(lookback + 1)]
        now = closes[-1]
        mom = (now - past) / past * 100 if past else 0
        return {"momentum_pct": round(mom, 2), "lookback_days": lookback}

    if indicator == "price_target":
        # Watchlist alert: fires when price crosses a specific level you set.
        # Perfect for "alert me when ATW drops below 480 so I can buy."
        target = cfg.get("target") or cfg.get("value")
        if target is None:
            return None
        return {"price": round(closes[-1], 2), "target": float(target)}

    return None


def _indicator_decision(indicator, v, cfg):
    """Return (direction, reason) if the indicator condition fires, else None."""
    if indicator == "rsi":
        rsi = v.get("rsi")
        if rsi is None:
            return None
        op = cfg.get("op", "<"); thr = cfg.get("value", 30)
        direction = cfg.get("direction", "buy")
        if op in _OPS and _OPS[op](rsi, thr):
            return direction, {"rsi": round(rsi, 2), "op": op, "threshold": thr}
        return None
    if indicator in ("ma_cross", "ma", "sma_cross"):
        f, s = v.get("ma_fast"), v.get("ma_slow")
        fp, sp = v.get("ma_fast_prev"), v.get("ma_slow_prev")
        if None in (f, s, fp, sp):
            return None
        if fp <= sp and f > s:   # golden cross
            return "buy", {"cross": "golden", "fast": round(f, 2), "slow": round(s, 2)}
        if fp >= sp and f < s:   # death cross
            return "sell", {"cross": "death", "fast": round(f, 2), "slow": round(s, 2)}
        return None
    if indicator == "macd":
        macd, sigln = v.get("macd"), v.get("signal")
        mp, sp = v.get("macd_prev"), v.get("signal_prev")
        if None in (macd, sigln, mp, sp):
            return None
        if mp <= sp and macd > sigln:
            return "buy", {"macd": round(macd, 4), "signal": round(sigln, 4)}
        if mp >= sp and macd < sigln:
            return "sell", {"macd": round(macd, 4), "signal": round(sigln, 4)}
        return None

    if indicator == "volume_spike":
        ratio = v.get("volume_ratio", 0)
        multiplier = float((cfg.get("multiplier") or cfg.get("value")) or 3.0)
        direction = cfg.get("direction", "buy")
        if ratio >= multiplier:
            return direction, {
                "volume_ratio": ratio,
                "multiplier": multiplier,
                "volume_today": v.get("volume_today"),
                "volume_avg_20d": v.get("volume_avg"),
                "note": f"Volume is {ratio}× the 20-day average — unusual activity",
            }
        return None

    if indicator == "momentum":
        mom = v.get("momentum_pct", 0)
        threshold = float(cfg.get("value", 10.0))
        direction = cfg.get("direction", "buy")
        if direction == "buy" and mom >= threshold:
            return "buy", {"momentum_pct": mom, "lookback_days": v.get("lookback_days"),
                           "threshold": threshold}
        if direction == "sell" and mom <= -threshold:
            return "sell", {"momentum_pct": mom, "lookback_days": v.get("lookback_days"),
                            "threshold": threshold}
        return None

    if indicator == "price_target":
        price = v.get("price", 0)
        target = v.get("target", 0)
        op = cfg.get("op", "<")
        direction = cfg.get("direction", "buy")
        label = cfg.get("label", "")
        if op in _OPS and _OPS[op](price, target):
            return direction, {
                "price": price, "target": target, "op": op,
                "label": label or f"Price {op} {target}",
            }
        return None

    return None


# ── Custom Python: sandboxed subprocess ──
def _eval_custom(strategy, quotes, ev) -> list[Signal]:
    """Run user code in a subprocess with a timeout. Single-owner trust model
    (like PyRunner); the subprocess + timeout is defense-in-depth. The user
    code receives `quotes` as JSON on stdin and prints a JSON list of signals:
      [{"ticker": "IAM", "direction": "buy", "reason": {...}}]
    """
    harness = (
        "import json,sys\n"
        "quotes=json.load(sys.stdin)\n"
        "ns={'quotes':quotes,'signals':[]}\n"
        "exec(USER_CODE, ns)\n"
        "print(json.dumps(ns.get('signals', [])))\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("USER_CODE = " + repr(strategy.code) + "\n" + harness)
        path = f.name

    try:
        proc = subprocess.run(
            [sys.executable, path], input=json.dumps(quotes),
            capture_output=True, text=True, timeout=CUSTOM_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        _log(ev, f"[ERROR] custom strategy timed out after {CUSTOM_TIMEOUT_SECONDS}s")
        return []
    if proc.returncode != 0:
        _log(ev, f"[ERROR] custom strategy error: {proc.stderr[:300]}")
        return []

    try:
        raw_signals = json.loads(proc.stdout.strip() or "[]")
    except json.JSONDecodeError:
        _log(ev, "[ERROR] custom strategy did not emit valid JSON")
        return []

    fired = []
    for s in raw_signals:
        tk = str(s.get("ticker", "")).upper()
        direction = s.get("direction", "buy")
        if not tk or direction not in (SignalDirection.BUY, SignalDirection.SELL):
            continue
        price = (quotes.get(tk) or {}).get("price")
        fired.append(_make_signal(ev, strategy, tk, direction, s.get("reason", {}), price))
        _log(ev, f"[OK] {tk}: custom -> {direction}")
    return fired


# ──────────────────────────────────────────────
# Signal creation + delivery hand-off
# ──────────────────────────────────────────────
def _make_signal(ev, strategy, ticker, direction, reason, price) -> Signal:
    return Signal.objects.create(
        evaluation=ev, strategy=strategy, ticker=ticker,
        direction=direction, reason=reason, price=price,
    )


def _enqueue_delivery(signal: Signal, ev: Evaluation) -> None:
    from signalrunner.models import Delivery, DeliveryKind, DeliveryStatus
    delivery = Delivery.objects.create(
        signal=signal, kind=DeliveryKind.TELEGRAM, status=DeliveryStatus.PENDING,
    )
    async_task("signalrunner.delivery.send_delivery", str(delivery.id))
    _log(ev, f"[INFO] queued Telegram delivery for {signal.direction} {signal.ticker}")


# ──────────────────────────────────────────────
# Indicator math (no hard TA dependency for the simple ones)
# ──────────────────────────────────────────────
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


def _macd(closes, fast, slow, signal):
    ef, es = _ema(closes, fast), _ema(closes, slow)
    if ef is None or es is None:
        return None
    macd_line = ef - es
    # crude signal/prev using one-step-back series
    ef_p, es_p = _ema(closes[:-1], fast), _ema(closes[:-1], slow)
    macd_prev = (ef_p - es_p) if (ef_p is not None and es_p is not None) else None
    return {"macd": macd_line, "signal": macd_line, "macd_prev": macd_prev,
            "signal_prev": macd_prev}


def _ema(closes, n):
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    ema = closes[-n]
    for price in closes[-n + 1:]:
        ema = price * k + ema * (1 - k)
    return ema


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _close_series(df):
    """Extract a list of closing prices from a casabourse history DataFrame."""
    if df is None:
        return None
    try:
        cols = {str(c).lower(): c for c in df.columns}
    except AttributeError:
        return None
    col = next((cols[c] for c in ("close", "cloture", "clôture", "last", "price")
                if c in cols), None)
    if col is None:
        # fall back to first numeric column
        import pandas as pd
        num = df.select_dtypes("number")
        if num.shape[1] == 0:
            return None
        col = num.columns[0]
    return [float(x) for x in df[col].tolist() if x is not None]


def _volume_series(df):
    """Extract a list of volume values from a casabourse history DataFrame."""
    if df is None:
        return None
    try:
        cols = {str(c).lower(): c for c in df.columns}
    except AttributeError:
        return None
    col = next((cols[c] for c in ("volume", "vol", "quantité échangée",
                                   "cumulvolumeechange", "volume échangé")
                if c in cols), None)
    if col is None:
        return None
    return [float(x) for x in df[col].tolist() if x is not None]


def _six_months_ago():
    from datetime import date, timedelta
    return (date.today() - timedelta(days=183)).isoformat()


def _now():
    return datetime.now(timezone.utc)


def _finalize(ev, status, started):
    ev.status = status
    ev.finished_at = _now()
    ev.duration_ms = int((time.monotonic() - started) * 1000)
    ev.save(update_fields=["status", "finished_at", "duration_ms"])


def _log(ev, line):
    ev.log = (ev.log or []) + [line]
    print(line)
