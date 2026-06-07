"""
signalrunner/opportunity.py

Composite Opportunity Score — merges the three layers that genuinely have
edge on BVC into a single daily ranked list:

  Layer 1  Fundamentals   (FundamentalScore: yield + value + consistency)
  Layer 2  Events         (recent AMMC filing → fresh catalyst)
  Layer 3  Technical pos.  (RSI + distance from 52-week low = entry timing)

This is a RESEARCH PRIORITISATION tool, not an autonomous trading signal.
It tells you where to point attention. The buy decision stays with you,
because on a thin market like BVC human judgment beats mechanical signals
(proven by backtesting — see journal).

The composite is pushed to Telegram daily as a ranked watchlist, and when a
top-ranked stock also hits a technical entry zone, a high-conviction alert
fires. Nothing auto-executes.
"""
from __future__ import annotations

from datetime import date, timedelta

from signalrunner import datasource
from signalrunner.models import (
    EarningsExtract, FundamentalScore, Signal, StockFundamentals, Strategy,
)

# Weights for the composite (must sum to 1.0)
W_FUNDAMENTAL = 0.50   # the layer with most documented edge on frontier markets
W_EVENT = 0.20         # fresh catalyst — information speed is BVC's only real alpha
W_TECHNICAL = 0.30     # entry timing — not prediction, just "is it oversold/cheap now"

EVENT_LOOKBACK_DAYS = 14   # an AMMC filing within this window counts as "fresh"


def compute_opportunity_scores(tickers: list[str]) -> list[dict]:
    """
    Build the composite ranked list. Returns a list of dicts (highest first):
      {ticker, composite, fundamental, event, technical, breakdown}
    """
    results = []
    for ticker in tickers:
        ticker = ticker.upper()
        fund = _fundamental_component(ticker)
        event = _event_component(ticker)
        tech = _technical_component(ticker)

        composite = (
            fund["score"] * W_FUNDAMENTAL +
            event["score"] * W_EVENT +
            tech["score"] * W_TECHNICAL
        )
        results.append({
            "ticker": ticker,
            "composite": round(composite, 2),
            "fundamental": fund["score"],
            "event": event["score"],
            "technical": tech["score"],
            "breakdown": {
                "fundamental": fund["detail"],
                "event": event["detail"],
                "technical": tech["detail"],
            },
        })

    results.sort(key=lambda r: r["composite"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def _fundamental_component(ticker: str) -> dict:
    """Layer 1 — pull the existing FundamentalScore (0–10)."""
    fs = FundamentalScore.objects.filter(ticker=ticker).first()
    if not fs:
        return {"score": 0.0, "detail": "no fundamental data"}
    return {"score": fs.total_score,
            "detail": f"fund score {fs.total_score}/10 · {fs.summary.get('dividend_yield','?')} yield · PE {fs.summary.get('pe_ratio','?')}"}


def _event_component(ticker: str) -> dict:
    """Layer 2 — recent AMMC filing = fresh catalyst worth attention (0–10)."""
    cutoff = date.today() - timedelta(days=EVENT_LOOKBACK_DAYS)
    # Signals of type 'announcement' fired by the AMMC monitor
    recent = Signal.objects.filter(
        ticker=ticker,
        created_at__date__gte=cutoff,
        reason__type="announcement",
    ).order_by("-created_at")
    count = recent.count()
    if count == 0:
        return {"score": 0.0, "detail": "no recent filing"}
    latest = recent.first()
    title = (latest.reason or {}).get("title", "")[:60]
    # 1 filing = 6, 2+ = 10 (more activity = more to react to)
    score = min(10.0, 4 + count * 3)
    return {"score": float(score), "detail": f"{count} filing(s) in {EVENT_LOOKBACK_DAYS}d · {title}"}


def _technical_component(ticker: str) -> dict:
    """
    Layer 3 — entry timing (0–10). Higher = better ENTRY zone (cheap/oversold).
    Combines: RSI (oversold = higher score) + position vs 52-week range.
    This is timing, NOT prediction.
    """
    try:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=200)).isoformat()
        df = datasource.get_history(ticker, start, end)
        closes = _closes(df)
    except Exception:
        closes = None

    if not closes or len(closes) < 30:
        return {"score": 5.0, "detail": "insufficient price history (neutral)"}

    rsi = _rsi(closes, 14)
    lo, hi = min(closes), max(closes)
    price = closes[-1]
    rng_pos = (price - lo) / (hi - lo) * 100 if hi > lo else 50  # 0 = at low, 100 = at high

    # RSI sub-score: oversold (low RSI) = better entry → higher score
    rsi_sub = max(0.0, min(10.0, (50 - rsi) / 5 + 5)) if rsi is not None else 5.0
    # Range sub-score: nearer 52w low = better entry → higher score
    range_sub = max(0.0, min(10.0, (100 - rng_pos) / 10))

    score = rsi_sub * 0.5 + range_sub * 0.5
    detail = f"RSI {rsi:.0f} · {rng_pos:.0f}% of 52w range"
    return {"score": round(score, 2), "detail": detail}


def send_daily_opportunities(strategy_name: str = "Composite Opportunity Monitor") -> None:
    """
    Compute the composite ranking for all tracked tickers and push the top
    list to Telegram. High-conviction alert when a top-3 fundamental stock
    also sits in a strong technical entry zone. Scheduled daily.
    """
    strategy = Strategy.objects.filter(name=strategy_name, enabled=True).first()
    tickers = strategy.tickers if strategy else _all_tracked_tickers()
    if not tickers:
        print("[opportunity] no tickers")
        return

    ranked = compute_opportunity_scores(tickers)
    _send_telegram_ranking(ranked)

    # High-conviction: strong fundamentals AND strong entry timing
    for r in ranked:
        if r["fundamental"] >= 6 and r["technical"] >= 7:
            _send_high_conviction(r)


def _all_tracked_tickers() -> list[str]:
    s: set[str] = set()
    for st in Strategy.objects.filter(enabled=True):
        s.update(t.upper() for t in (st.tickers or []))
    return list(s)


def _send_telegram_ranking(ranked: list[dict]) -> None:
    from signalrunner.models import Secret
    try:
        token = Secret.objects.get(name="TELEGRAM_BOT_TOKEN").value
        chat_id = Secret.objects.get(name="TELEGRAM_CHAT_ID").value
    except Exception:
        print("[opportunity] telegram secrets missing")
        return

    lines = ["📋 *BVC Daily Opportunity Ranking*", "_Research priorities — not buy signals_\n"]
    for r in ranked[:8]:
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(r["rank"], f"{r['rank']}.")
        lines.append(
            f"{medal} *{r['ticker']}* — {r['composite']}/10\n"
            f"   F:{r['fundamental']:.0f} E:{r['event']:.0f} T:{r['technical']:.0f} · "
            f"{r['breakdown']['technical']}"
        )
    lines.append("\n_F=fundamentals E=events T=timing. You decide. Not financial advice._")
    text = "\n".join(lines)

    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        print("[opportunity] daily ranking sent")
    except Exception as exc:
        print(f"[opportunity] telegram failed: {exc}")


def _send_high_conviction(r: dict) -> None:
    from signalrunner.models import Secret
    try:
        token = Secret.objects.get(name="TELEGRAM_BOT_TOKEN").value
        chat_id = Secret.objects.get(name="TELEGRAM_CHAT_ID").value
    except Exception:
        return
    text = (
        f"⭐ *HIGH-CONVICTION SETUP* — {r['ticker']}\n\n"
        f"Strong fundamentals AND favourable entry timing both align:\n\n"
        f"📊 Fundamentals: {r['fundamental']:.0f}/10\n"
        f"   {r['breakdown']['fundamental']}\n"
        f"⏱ Entry timing: {r['technical']:.0f}/10\n"
        f"   {r['breakdown']['technical']}\n"
        f"📢 Events: {r['breakdown']['event']}\n\n"
        f"This means: a stock with good fundamentals is currently in a cheap/oversold "
        f"zone. *Research it and decide.* It is not an instruction to buy.\n\n"
        f"_Not financial advice._"
    )
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        print(f"[opportunity] high-conviction alert sent: {r['ticker']}")
    except Exception as exc:
        print(f"[opportunity] hc alert failed: {exc}")


# ── small numeric helpers (self-contained) ──────────────────────────────────────

def _closes(df):
    if df is None:
        return None
    try:
        cols = {str(c).lower(): c for c in df.columns}
    except AttributeError:
        return None
    col = next((cols[c] for c in ("close", "cloture", "clôture", "last", "price")
                if c in cols), None)
    if col is None:
        return None
    return [float(x) for x in df[col].tolist() if x is not None]


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        (gains if diff >= 0 else losses).append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
