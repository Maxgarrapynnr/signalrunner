"""
signalrunner/announcement_monitor.py

Monitors the AMMC (Autorité Marocaine du Marché des Capitaux) official
publication feed for new company filings — earnings, dividends, capital
operations, board decisions — and fires a Telegram alert within minutes
of publication.

Why AMMC and not bourse.ma:
  AMMC is the official regulator. Every filing must pass through them.
  Their actualités page (ammc.ma/fr/actualites) is updated in near
  real-time and lists company name + filing type in a parseable format.

Usage: called by the SignalRunner scheduler as a django-q task.
Run manually: python manage.py shell
  >>> from signalrunner.announcement_monitor import check_announcements
  >>> check_announcements()
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import requests
from django.conf import settings
from django.utils import timezone as dj_tz
from django_q.tasks import async_task

from signalrunner.models import (
    Delivery, DeliveryKind, DeliveryStatus,
    Evaluation, EvaluationStatus, Signal, SignalDirection,
    Strategy, TriggerType,
)

# ── Configuration ─────────────────────────────────────────────────────────────

AMMC_URL = "https://www.ammc.ma/fr/actualites"
REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Keywords that indicate a high-value filing — these trigger an alert.
HIGH_VALUE_KEYWORDS = [
    "dividende", "dividend",
    "résultats", "résultat annuel", "résultat semestriel",
    "bénéfice", "benefice",
    "augmentation de capital",
    "rachat d'actions", "rachat d actions",
    "offre publique",
    "fusion", "acquisition",
    "assemblée générale", "ago", "age",
    "mise à jour annuelle",
    "émission", "obligation",
    "suspension", "radiation",
    "note d'information", "prospectus",
]

# Company name → BVC ticker mapping (for recognising mentions in AMMC titles).
COMPANY_TICKERS = {
    "itissalat al-maghrib": "IAM", "maroc telecom": "IAM",
    "attijariwafa": "ATW",
    "banque centrale populaire": "BCP", "bcp": "BCP",
    "bank of africa": "BOA",
    "cih": "CIH", "crédit immobilier": "CIH",
    "label vie": "LBV",
    "managem": "MNG",
    "cosumar": "CSR",
    "holcim": "LHM", "lafargeholcim": "LHM",
    "hps": "HPS", "hightech payment": "HPS",
    "afriquia gaz": "GAZ",
    "ocp": "OCP",
    "bmci": "BCI",
    "delta holding": "DHO",
    "addoha": "ADH",
    "alliances": "ALM",
    "lydec": "LYD",
    "microdata": "MIC",
    "lesieur": "LES",
    "cmgp": "CMG",
    "cfg bank": "CFG",
    "akdital": "AKT",
}


# ── Main entry point ───────────────────────────────────────────────────────────

def check_announcements(strategy_name: str = "BVC Announcement Monitor") -> None:
    """
    Fetch AMMC latest news, detect new high-value filings,
    and fire a Signal + Telegram delivery for each one.
    Called by the django-q scheduler.
    """
    try:
        strategy = Strategy.objects.filter(
            name=strategy_name, enabled=True
        ).first()
        if strategy is None:
            print(f"[monitor] Strategy '{strategy_name}' not found or disabled")
            return

        ev = Evaluation.objects.create(
            strategy=strategy,
            trigger=TriggerType.SCHEDULED,
            status=EvaluationStatus.RUNNING,
            log=[],
        )

        announcements = _fetch_announcements()
        if not announcements:
            _finish(ev, EvaluationStatus.FAILED, "[ERROR] could not fetch AMMC page")
            return

        _log(ev, f"[INFO] fetched {len(announcements)} announcements from AMMC")

        seen_hashes = _load_seen(strategy)
        new_found = []

        for ann in announcements:
            h = _hash(ann)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            new_found.append(ann)

        _log(ev, f"[INFO] {len(new_found)} new announcement(s)")

        fired = []
        for ann in new_found:
            if not _is_high_value(ann):
                continue
            ticker = _extract_ticker(ann, strategy.tickers)
            sig = Signal.objects.create(
                evaluation=ev,
                strategy=strategy,
                ticker=ticker or "BVC",
                direction=SignalDirection.BUY,  # informational — not a buy/sell
                reason={
                    "type": "announcement",
                    "title": ann["title"],
                    "date": ann["date"],
                    "url": ann.get("url", ""),
                    "matched_keywords": _matched_keywords(ann),
                },
                price=None,
            )
            fired.append(sig)
            _log(ev, f"[OK] new filing: {ann['title'][:80]}")
            _send_announcement_alert(sig, ann)

        _save_seen(strategy, seen_hashes)
        ev.fired = bool(fired)
        _finish(ev, EvaluationStatus.SUCCESS,
                f"[OK] {len(new_found)} new, {len(fired)} high-value alerts sent")

    except Exception as exc:
        print(f"[monitor] ERROR: {exc}")
        raise


# ── AMMC scraper ──────────────────────────────────────────────────────────────

def _fetch_announcements() -> list[dict]:
    """Fetch and parse the AMMC actualités page."""
    try:
        resp = requests.get(
            AMMC_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[monitor] AMMC fetch failed: {exc}")
        return []

    return _parse_ammc_html(resp.text)


def _parse_ammc_html(html: str) -> list[dict]:
    """
    Parse AMMC news items from the HTML. AMMC uses a Drupal-generated
    list of articles. Each item has a date (DD/MM/YYYY) and a title/link.
    Falls back to a simple regex approach if the structure changes.
    """
    import re
    announcements = []

    # Pattern 1: the structured news list on actualités page
    # Each block looks like:
    #   <span class="...date...">05/06/2026</span>
    #   <a href="/fr/actualites/...">Title here</a>
    date_title_pattern = re.compile(
        r'(\d{2}/\d{2}/\d{4})\s*(?:</[^>]+>)?\s*(?:<[^>]+>)*\s*'
        r'<a[^>]+href="(/fr/actualites/[^"]+)"[^>]*>([^<]+)</a>',
        re.DOTALL,
    )
    for m in date_title_pattern.finditer(html):
        date_str, path, title = m.group(1), m.group(2), m.group(3).strip()
        announcements.append({
            "date": date_str,
            "title": title,
            "url": f"https://www.ammc.ma{path}",
        })

    # Pattern 2: fallback — find any heading/link with a date nearby
    if not announcements:
        link_pattern = re.compile(
            r'href="(/fr/actualites/[^"]+)"[^>]*>([^<]{10,200})</a>'
        )
        date_pattern = re.compile(r'(\d{2}/\d{2}/\d{4})')
        dates = date_pattern.findall(html)
        links = link_pattern.findall(html)
        for i, (path, title) in enumerate(links):
            announcements.append({
                "date": dates[i] if i < len(dates) else "",
                "title": title.strip(),
                "url": f"https://www.ammc.ma{path}",
            })

    # Deduplicate by URL
    seen_urls: set[str] = set()
    unique = []
    for a in announcements:
        if a["url"] not in seen_urls:
            seen_urls.add(a["url"])
            unique.append(a)
    return unique


# ── Filtering ─────────────────────────────────────────────────────────────────

def _is_high_value(ann: dict) -> bool:
    """True if the announcement title contains a high-value keyword."""
    title_lower = ann["title"].lower()
    return any(kw in title_lower for kw in HIGH_VALUE_KEYWORDS)


def _matched_keywords(ann: dict) -> list[str]:
    title_lower = ann["title"].lower()
    return [kw for kw in HIGH_VALUE_KEYWORDS if kw in title_lower]


def _extract_ticker(ann: dict, watched_tickers: list) -> str | None:
    """
    Try to identify which BVC ticker this announcement relates to
    by matching company names in the title.
    """
    title_lower = ann["title"].lower()
    # First check watched tickers' company names
    for company, ticker in COMPANY_TICKERS.items():
        if company in title_lower:
            if not watched_tickers or ticker in watched_tickers:
                return ticker
    return None


# ── Persistent seen-hash store (uses the Strategy's config JSON) ──────────────

def _load_seen(strategy: Strategy) -> set[str]:
    """Load the set of already-seen announcement hashes from strategy config."""
    return set(strategy.config.get("_seen_hashes", []))


def _save_seen(strategy: Strategy, hashes: set[str]) -> None:
    """Persist seen hashes back into strategy config (keep last 500)."""
    config = dict(strategy.config)
    config["_seen_hashes"] = list(hashes)[-500:]
    strategy.config = config
    strategy.save(update_fields=["config"])


def _hash(ann: dict) -> str:
    return hashlib.md5(ann["url"].encode()).hexdigest()


# ── Telegram delivery ─────────────────────────────────────────────────────────

def _send_announcement_alert(signal: Signal, ann: dict) -> None:
    """Format and send a Telegram message for a new BVC announcement."""
    from signalrunner.models import Secret

    try:
        token = Secret.objects.get(name="TELEGRAM_BOT_TOKEN").value
        chat_id = Secret.objects.get(name="TELEGRAM_CHAT_ID").value
    except Exception:
        print("[monitor] Telegram secrets not configured")
        return

    ticker = signal.ticker
    title = ann["title"]
    date = ann["date"]
    url = ann.get("url", "")
    keywords = signal.reason.get("matched_keywords", [])

    emoji = "📢"
    if any(k in keywords for k in ["dividende", "dividend"]):
        emoji = "💰"
    elif any(k in keywords for k in ["résultats", "bénéfice"]):
        emoji = "📊"
    elif any(k in keywords for k in ["augmentation de capital"]):
        emoji = "🏦"
    elif any(k in keywords for k in ["offre publique", "fusion", "acquisition"]):
        emoji = "🤝"

    text = (
        f"{emoji} *BVC FILING*\n"
        f"{'📌 ' + ticker if ticker != 'BVC' else '🏛 Marché'}\n\n"
        f"{title}\n\n"
        f"📅 {date}\n"
        f"🔗 [Voir sur AMMC]({url})\n\n"
        f"_SignalRunner · not financial advice_"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                  "disable_web_page_preview": False},
            timeout=20,
        )
        if resp.status_code == 200:
            print(f"[monitor] Telegram sent: {title[:50]}")
        else:
            print(f"[monitor] Telegram error {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        print(f"[monitor] Telegram exception: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log(ev: Evaluation, line: str) -> None:
    ev.log = (ev.log or []) + [line]
    ev.save(update_fields=["log"])
    print(line)


def _finish(ev: Evaluation, status: str, msg: str) -> None:
    ev.status = status
    ev.finished_at = dj_tz.now()
    _log(ev, msg)
    ev.save(update_fields=["status", "finished_at"])
