"""
signalrunner/models.py

Single source of truth, imported by every other module. Single-owner: no
User/owner FKs anywhere; the login is a gate, not a tenancy boundary.

PyRunner mapping: Script→Strategy, Run→Evaluation, Notification→Delivery,
Secret→Secret. New objects: Signal (the fired event) and MarketDataSnapshot
(shared cache so many strategies share one provider pull).
"""
import uuid

from django.conf import settings
from django.db import models
from cryptography.fernet import Fernet


# ──────────────────────────────────────────────
# Enumerations
# ──────────────────────────────────────────────
class StrategyKind(models.TextChoices):
    RULE = "rule", "Simple rule (threshold / % move)"
    INDICATOR = "indicator", "Technical indicator (RSI / MACD / MA)"
    CUSTOM = "custom_python", "Custom Python strategy"


class ScheduleKind(models.TextChoices):
    MANUAL = "manual", "Manual only"
    INTERVAL = "interval", "Every N minutes (market hours)"
    DAILY = "daily", "Daily at a set time"


class TriggerType(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ON_DEMAND = "on_demand", "On demand"
    WEBHOOK = "webhook", "Webhook"


class EvaluationStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"   # ran cleanly (may or may not have fired)
    FAILED = "failed", "Failed"      # data fetch or strategy error


class SignalDirection(models.TextChoices):
    BUY = "buy", "Buy"
    SELL = "sell", "Sell"


class DeliveryKind(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    # EMAIL / WEBHOOK / DISCORD deferred to later


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


# ──────────────────────────────────────────────
# Strategy  (PyRunner's Script)
# ──────────────────────────────────────────────
class Strategy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=StrategyKind.choices)

    # Watched BVC tickers, e.g. ["IAM", "ATW", "BCP"].
    tickers = models.JSONField(default=list)

    # Kind-specific config. Examples:
    #   rule:      {"field": "price", "op": ">", "value": 120}
    #              {"field": "pct_change", "op": ">=", "value": 3.0}
    #   indicator: {"indicator": "rsi", "period": 14, "op": "<", "value": 30,
    #               "direction": "buy"}
    #              {"indicator": "ma_cross", "fast": 20, "slow": 50}
    #   custom:    {}  (logic lives in `code`)
    config = models.JSONField(default=dict, blank=True)

    # Only for kind=custom_python: the uploaded strategy source.
    code = models.TextField(blank=True)

    # Scheduling (per-strategy; cadence is a config choice, not a global lock).
    schedule_kind = models.CharField(
        max_length=12, choices=ScheduleKind.choices, default=ScheduleKind.MANUAL
    )
    interval_minutes = models.PositiveIntegerField(null=True, blank=True)
    daily_at = models.CharField(max_length=5, blank=True)  # "HH:MM" local market time

    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "strategies"

    def __str__(self):
        return f"{self.name} [{self.kind}]"


# ──────────────────────────────────────────────
# Evaluation  (PyRunner's Run)
# ──────────────────────────────────────────────
class Evaluation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # SET_NULL (not CASCADE): evaluations + their signals are historical records
    # that should outlive the strategy that produced them.
    strategy = models.ForeignKey(
        Strategy, related_name="evaluations", null=True, on_delete=models.SET_NULL
    )

    trigger = models.CharField(max_length=12, choices=TriggerType.choices)
    status = models.CharField(
        max_length=12, choices=EvaluationStatus.choices, default=EvaluationStatus.QUEUED
    )

    # What the strategy computed: the data it saw + indicator values, for audit.
    # e.g. {"IAM": {"price": 118.4, "rsi": 28.1}, ...}
    computed = models.JSONField(default=dict, blank=True)
    fired = models.BooleanField(default=False)  # did it emit any signal?

    # Prefixed log lines ([INFO]/[OK]/[WARN]/[ERROR]), PyRunner-style.
    log = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-queued_at"]
        indexes = [models.Index(fields=["status", "-queued_at"])]

    def __str__(self):
        return f"Eval {self.id} [{self.status}] {self.strategy.name}"


# ──────────────────────────────────────────────
# Signal  (NEW — the fired buy/sell event)
# ──────────────────────────────────────────────
class Signal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evaluation = models.ForeignKey(
        Evaluation, related_name="signals", on_delete=models.CASCADE
    )
    # Denormalized strategy ref so the signals feed survives strategy deletion.
    strategy = models.ForeignKey(
        Strategy, related_name="signals", null=True, on_delete=models.SET_NULL
    )

    ticker = models.CharField(max_length=20)
    direction = models.CharField(max_length=4, choices=SignalDirection.choices)
    # Why it fired: the values that satisfied the condition.
    reason = models.JSONField(default=dict, blank=True)  # {"rsi": 28.1, "threshold": 30}
    price = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["ticker", "-created_at"])]

    def __str__(self):
        return f"{self.direction.upper()} {self.ticker}"


# ──────────────────────────────────────────────
# MarketDataSnapshot  (NEW — shared provider cache)
# ──────────────────────────────────────────────
class MarketDataSnapshot(models.Model):
    """One cached quote for a ticker at a point in time. Many strategies
    evaluated in the same window read the same snapshot instead of each
    hitting the provider — rate-limit protection + consistency."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticker = models.CharField(max_length=20, db_index=True)

    price = models.FloatField(null=True, blank=True)
    pct_change = models.FloatField(null=True, blank=True)
    volume = models.FloatField(null=True, blank=True)
    # Full provider payload for indicators that need OHLC/history.
    raw = models.JSONField(default=dict, blank=True)

    source = models.CharField(max_length=40, default="casabourse")
    fetched_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-fetched_at"]
        indexes = [models.Index(fields=["ticker", "-fetched_at"])]

    def __str__(self):
        return f"{self.ticker} @ {self.fetched_at:%Y-%m-%d %H:%M}"


# ──────────────────────────────────────────────
# Delivery  (PyRunner's Notification)
# ──────────────────────────────────────────────
class Delivery(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    signal = models.ForeignKey(
        Signal, related_name="deliveries", on_delete=models.CASCADE
    )
    kind = models.CharField(
        max_length=12, choices=DeliveryKind.choices, default=DeliveryKind.TELEGRAM
    )
    # Resolved target, e.g. {"chat_id": "123456"}.
    target = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=12, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} -> {self.status}"


# ──────────────────────────────────────────────
# Secret  (unchanged from PyRunner/DocRunner)
# ──────────────────────────────────────────────
class Secret(models.Model):
    name = models.CharField(max_length=100, unique=True)  # e.g. TELEGRAM_BOT_TOKEN
    description = models.CharField(max_length=255, blank=True)
    _ciphertext = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def set_value(self, plaintext: str) -> None:
        f = Fernet(settings.ENCRYPTION_KEY)
        self._ciphertext = f.encrypt(plaintext.encode())

    @property
    def value(self) -> str:
        f = Fernet(settings.ENCRYPTION_KEY)
        return f.decrypt(bytes(self._ciphertext)).decode()

    def __str__(self):
        return self.name
