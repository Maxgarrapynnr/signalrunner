"""
signalrunner/delivery.py

Sends a fired Signal to Telegram. Enqueued by the worker, one task per Delivery.
A separate, independently-retryable concern from evaluation — a failed delivery
never affects the evaluation's status.

Telegram setup (stored as Secrets, never in code):
  TELEGRAM_BOT_TOKEN  — from @BotFather
  TELEGRAM_CHAT_ID    — the owner's chat/channel id
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests
from django_q.tasks import async_task

from signalrunner.models import Delivery, Secret, DeliveryStatus

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30
TELEGRAM_TIMEOUT = 20


class DeliveryError(Exception):
    """A delivery attempt failed; triggers retry/backoff."""


def send_delivery(delivery_id: str) -> None:
    """Attempt one Telegram send; re-enqueue with backoff on transient failure."""
    delivery = Delivery.objects.select_related("signal").get(id=delivery_id)
    if delivery.status == DeliveryStatus.SENT:
        return

    delivery.attempts += 1
    delivery.save(update_fields=["attempts"])

    try:
        _send_telegram(delivery)
    except DeliveryError as exc:
        _handle_failure(delivery, str(exc))
        return
    except Exception as exc:
        _handle_failure(delivery, f"{type(exc).__name__}: {exc}")
        return

    delivery.status = DeliveryStatus.SENT
    delivery.sent_at = datetime.now(timezone.utc)
    delivery.last_error = ""
    delivery.save(update_fields=["status", "sent_at", "last_error"])


def _send_telegram(delivery: Delivery) -> None:
    token = _secret("TELEGRAM_BOT_TOKEN")
    chat_id = (delivery.target or {}).get("chat_id") or _secret("TELEGRAM_CHAT_ID")

    sig = delivery.signal
    arrow = "🟢 BUY" if sig.direction == "buy" else "🔴 SELL"
    price = f" @ {sig.price}" if sig.price is not None else ""
    reason = ", ".join(f"{k}={v}" for k, v in (sig.reason or {}).items())
    text = (
        f"{arrow}  *{sig.ticker}*{price}\n"
        f"{reason}\n"
        f"_SignalRunner · not financial advice_"
    )

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=TELEGRAM_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise DeliveryError(f"Telegram request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise DeliveryError(f"Telegram HTTP {resp.status_code}: {resp.text[:200]}")


def _handle_failure(delivery: Delivery, error: str) -> None:
    delivery.last_error = error[:2000]
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = DeliveryStatus.FAILED
        delivery.save(update_fields=["status", "last_error"])
        print(f"[ERROR] delivery {delivery.id} gave up after {delivery.attempts}: {error}")
        return
    delivery.status = DeliveryStatus.PENDING
    delivery.save(update_fields=["status", "last_error"])
    delay = BACKOFF_BASE_SECONDS * (2 ** (delivery.attempts - 1))
    print(f"[WARN] delivery {delivery.id} attempt {delivery.attempts} failed; "
          f"retry in {delay}s: {error}")
    async_task("signalrunner.delivery.send_delivery", str(delivery.id),
               q_options={"delay": delay})


def retry_delivery(delivery_id: str) -> None:
    """Manual retry hook for the UI."""
    delivery = Delivery.objects.get(id=delivery_id)
    if delivery.status == DeliveryStatus.SENT:
        return
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.last_error = ""
    delivery.save(update_fields=["status", "attempts", "last_error"])
    async_task("signalrunner.delivery.send_delivery", str(delivery.id))


def _secret(name: str) -> str:
    try:
        return Secret.objects.get(name=name).value
    except Secret.DoesNotExist:
        raise DeliveryError(f"secret '{name}' not found") from None
