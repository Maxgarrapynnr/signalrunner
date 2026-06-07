"""
signalrunner/telegram_bot.py

Inbound Telegram command handler. Polls the Telegram Bot API for messages
and responds to commands from the owner. Run as a django-q scheduled task.

Commands:
  /status          — show enabled strategies + last evaluation status
  /signals         — last 5 signals fired
  /signals today   — signals fired today
  /evaluate IAM    — trigger an on-demand evaluation of strategies watching IAM
  /evaluate all    — trigger all enabled strategies now
  /help            — list commands

Security: only responds to messages from TELEGRAM_CHAT_ID (the owner).
All others are silently ignored.

Setup: add to django-q schedule in settings or via the admin.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import requests
from django.conf import settings

from signalrunner.models import (
    Evaluation, EvaluationStatus, Signal, Strategy, TriggerType,
)

TELEGRAM_TIMEOUT = 15
_LAST_UPDATE_ID_KEY = "_tg_last_update_id"


def poll_telegram_commands() -> None:
    """
    Long-poll Telegram for new messages. Called every minute by django-q.
    Processes any pending commands from the owner.
    """
    try:
        token, chat_id = _get_secrets()
    except Exception as exc:
        print(f"[tgbot] secrets not configured: {exc}")
        return

    last_id = _load_last_update_id()
    updates = _get_updates(token, offset=last_id + 1 if last_id else None)

    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id > (last_id or 0):
            last_id = update_id

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue

        # Security: only respond to the configured owner chat
        sender_id = str(msg.get("chat", {}).get("id", ""))
        if sender_id != str(chat_id):
            print(f"[tgbot] ignored message from unknown chat {sender_id}")
            continue

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        response = _handle_command(text)
        if response:
            _send(token, chat_id, response)

    if last_id:
        _save_last_update_id(last_id)


# ── Command handlers ───────────────────────────────────────────────────────────

def _handle_command(text: str) -> str | None:
    parts = text.lower().split()
    cmd = parts[0]

    if cmd in ("/help", "/start"):
        return (
            "🤖 *SignalRunner Bot*\n\n"
            "Commands:\n"
            "`/status` — enabled strategies + last eval\n"
            "`/signals` — last 5 signals\n"
            "`/signals today` — signals fired today\n"
            "`/evaluate all` — run all strategies now\n"
            "`/evaluate IAM` — run strategies watching a ticker\n"
            "`/help` — this message\n\n"
            "_Not financial advice._"
        )

    if cmd == "/status":
        return _cmd_status()

    if cmd == "/signals":
        filter_today = len(parts) > 1 and parts[1] == "today"
        return _cmd_signals(today=filter_today)

    if cmd == "/evaluate":
        arg = parts[1] if len(parts) > 1 else "all"
        return _cmd_evaluate(arg.upper())

    return f"Unknown command: `{cmd}`\nType `/help` for available commands."


def _cmd_status() -> str:
    strategies = Strategy.objects.filter(enabled=True).order_by("name")
    if not strategies:
        return "No enabled strategies."

    lines = ["📊 *Active strategies:*\n"]
    for st in strategies:
        last_ev = st.evaluations.first()
        if last_ev:
            status_emoji = {"success": "✅", "failed": "❌",
                            "running": "🔄", "queued": "⏳"}.get(last_ev.status, "❓")
            fired = " · 🔔 fired" if last_ev.fired else ""
            when = last_ev.queued_at.strftime("%m-%d %H:%M") if last_ev.queued_at else "?"
            last_info = f"{status_emoji} {when}{fired}"
        else:
            last_info = "never run"

        schedule = {
            "manual": "manual",
            "interval": f"every {st.interval_minutes}m",
            "daily": f"daily {st.daily_at}",
        }.get(st.schedule_kind, st.schedule_kind)

        lines.append(f"• *{st.name}* ({schedule})\n  {last_info}")

    return "\n".join(lines)


def _cmd_signals(today: bool = False) -> str:
    qs = Signal.objects.select_related("strategy").order_by("-created_at")
    if today:
        from django.utils import timezone as dj_tz
        qs = qs.filter(created_at__date=dj_tz.now().date())
        qs = qs[:20]
        header = "📈 *Signals today:*\n"
    else:
        qs = qs[:5]
        header = "📈 *Last 5 signals:*\n"

    signals = list(qs)
    if not signals:
        return "No signals" + (" today" if today else "") + "."

    lines = [header]
    for s in signals:
        arrow = "🟢 BUY" if s.direction == "buy" else "🔴 SELL"
        price = f" @ {s.price:.2f}" if s.price else ""
        strategy_name = s.strategy.name if s.strategy else "—"
        when = s.created_at.strftime("%m-%d %H:%M") if s.created_at else "?"
        lines.append(f"{arrow} *{s.ticker}*{price}\n  {strategy_name} · {when}")

    return "\n".join(lines)


def _cmd_evaluate(arg: str) -> str:
    from django_q.tasks import async_task

    if arg == "ALL":
        strategies = list(Strategy.objects.filter(enabled=True))
    else:
        # Find strategies that watch this ticker
        strategies = [
            st for st in Strategy.objects.filter(enabled=True)
            if arg in (t.upper() for t in (st.tickers or []))
        ]

    if not strategies:
        return f"No enabled strategies found" + (f" watching {arg}" if arg != "ALL" else "") + "."

    evals_started = []
    for st in strategies:
        ev = Evaluation.objects.create(
            strategy=st,
            trigger=TriggerType.ON_DEMAND,
            status=EvaluationStatus.QUEUED,
        )
        async_task("signalrunner.tasks.run_evaluation", str(ev.id))
        evals_started.append(st.name)

    names = "\n".join(f"• {n}" for n in evals_started)
    return (
        f"⚡ *Evaluation started for {len(evals_started)} strategy(-ies):*\n"
        f"{names}\n\n"
        f"_You'll receive a signal alert if any conditions fire._"
    )


# ── Telegram API helpers ───────────────────────────────────────────────────────

def _get_updates(token: str, offset: int | None = None) -> list[dict]:
    params: dict = {"timeout": 10}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params=params,
            timeout=TELEGRAM_TIMEOUT,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except Exception as exc:
        print(f"[tgbot] getUpdates failed: {exc}")
    return []


def _send(token: str, chat_id: str, text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=TELEGRAM_TIMEOUT,
        )
    except Exception as exc:
        print(f"[tgbot] send failed: {exc}")


def _get_secrets() -> tuple[str, str]:
    from signalrunner.models import Secret
    token = Secret.objects.get(name="TELEGRAM_BOT_TOKEN").value
    chat_id = Secret.objects.get(name="TELEGRAM_CHAT_ID").value
    return token, chat_id


def _load_last_update_id() -> int | None:
    try:
        from signalrunner.models import Secret
        s = Secret.objects.filter(name=_LAST_UPDATE_ID_KEY).first()
        return int(s.value) if s else None
    except Exception:
        return None


def _save_last_update_id(update_id: int) -> None:
    try:
        from signalrunner.models import Secret
        s, _ = Secret.objects.get_or_create(name=_LAST_UPDATE_ID_KEY,
                                             defaults={"description": "internal"})
        s.set_value(str(update_id))
        s.save()
    except Exception as exc:
        print(f"[tgbot] failed to save update_id: {exc}")
