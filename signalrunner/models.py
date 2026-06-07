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
# Backtest  (NEW — replay a strategy over history)
# ──────────────────────────────────────────────
class BacktestStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"


class Backtest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    strategy = models.ForeignKey(
        Strategy, related_name="backtests", null=True, on_delete=models.SET_NULL
    )
    # Snapshot of the strategy config at backtest time (survives strategy edits).
    strategy_name = models.CharField(max_length=200, blank=True)
    config_snapshot = models.JSONField(default=dict, blank=True)

    start_date = models.DateField()
    end_date = models.DateField()
    horizon_days = models.PositiveIntegerField(default=5)  # forward-return window
    take_profit_pct = models.FloatField(null=True, blank=True)  # e.g. 8.0
    stop_loss_pct = models.FloatField(null=True, blank=True)    # e.g. 4.0

    status = models.CharField(
        max_length=12, choices=BacktestStatus.choices, default=BacktestStatus.QUEUED
    )
    # Computed summary statistics (shape documented in backtest.py).
    stats = models.JSONField(default=dict, blank=True)
    # Cumulative equity curve points for charting: [{"date","equity"}...]
    equity_curve = models.JSONField(default=list, blank=True)
    log = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Backtest {self.strategy_name} [{self.status}]"


class BacktestSignal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    backtest = models.ForeignKey(
        Backtest, related_name="signals", on_delete=models.CASCADE
    )
    ticker = models.CharField(max_length=20)
    direction = models.CharField(max_length=4, choices=SignalDirection.choices)
    session_date = models.DateField()           # the day the signal fired
    entry_price = models.FloatField()
    # Forward-return measurement
    exit_price = models.FloatField(null=True, blank=True)
    exit_date = models.DateField(null=True, blank=True)
    return_pct = models.FloatField(null=True, blank=True)
    won = models.BooleanField(null=True)        # direction correct?
    exit_kind = models.CharField(max_length=12, blank=True)  # horizon|take_profit|stop_loss
    reason = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["session_date"]
        indexes = [models.Index(fields=["backtest", "session_date"])]

    def __str__(self):
        return f"{self.direction.upper()} {self.ticker} @ {self.session_date}"


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



# ──────────────────────────────────────────────
# Fundamental data
# ──────────────────────────────────────────────
class StockFundamentals(models.Model):
    """Daily fundamental snapshot per ticker. Populated by the fundamentals
    scraper from BVC market data (market cap, price) and AMMC PDF extracts
    (EPS, dividend, revenue, net income). Refreshed once per trading day."""
    ticker = models.CharField(max_length=20, db_index=True)
    name = models.CharField(max_length=200, blank=True)
    date = models.DateField(db_index=True)

    # From BVC live market API (casabourse)
    price = models.FloatField(null=True, blank=True)
    market_cap = models.FloatField(null=True, blank=True)     # MAD
    shares_outstanding = models.FloatField(null=True, blank=True)
    week_52_high = models.FloatField(null=True, blank=True)
    week_52_low = models.FloatField(null=True, blank=True)

    # From AMMC PDF extracts (latest annual report)
    revenue = models.FloatField(null=True, blank=True)         # MAD
    net_income = models.FloatField(null=True, blank=True)      # MAD
    eps = models.FloatField(null=True, blank=True)             # BNA — bénéfice net par action
    dividend_per_share = models.FloatField(null=True, blank=True)  # MAD
    fiscal_year = models.IntegerField(null=True, blank=True)

    # Computed ratios (derived from above)
    pe_ratio = models.FloatField(null=True, blank=True)        # price / eps
    dividend_yield = models.FloatField(null=True, blank=True)  # dividend / price * 100
    payout_ratio = models.FloatField(null=True, blank=True)    # dividend / eps * 100
    price_to_book = models.FloatField(null=True, blank=True)

    # Metadata
    pdf_source_url = models.URLField(blank=True)
    data_quality = models.CharField(max_length=20, default="partial",
        choices=[("full","Full"),("partial","Partial"),("price_only","Price only")])
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-date"]
        unique_together = [["ticker", "date"]]
        indexes = [models.Index(fields=["ticker", "-date"])]

    def __str__(self):
        return f"{self.ticker} fundamentals {self.date}"

    def compute_ratios(self):
        """Recompute derived ratios from raw fields."""
        if self.price and self.eps and self.eps != 0:
            self.pe_ratio = round(self.price / self.eps, 2)
        if self.price and self.dividend_per_share and self.price != 0:
            self.dividend_yield = round(self.dividend_per_share / self.price * 100, 2)
        if self.eps and self.dividend_per_share and self.eps != 0:
            self.payout_ratio = round(self.dividend_per_share / self.eps * 100, 1)


class FundamentalScore(models.Model):
    """Composite fundamental score per ticker, recomputed daily.
    Not a buy signal — a ranking to help identify stocks worth researching."""
    ticker = models.CharField(max_length=20, unique=True)
    date = models.DateField()

    # Sub-scores (0–10 each, higher = better)
    yield_score = models.FloatField(default=0)      # dividend yield vs market avg
    value_score = models.FloatField(default=0)      # P/E vs market avg (lower = better)
    consistency_score = models.FloatField(default=0) # dividend payment consistency
    momentum_score = models.FloatField(default=0)   # 3-month price momentum

    # Composite (0–10)
    total_score = models.FloatField(default=0)
    rank = models.PositiveSmallIntegerField(null=True, blank=True)  # 1 = best

    # Readable breakdown
    summary = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-total_score"]

    def __str__(self):
        return f"{self.ticker} score={self.total_score:.1f} rank={self.rank}"


class EarningsExtract(models.Model):
    """Key numbers extracted from an AMMC earnings PDF.
    One row per annual/semi-annual report per company."""
    ticker = models.CharField(max_length=20, db_index=True)
    report_type = models.CharField(max_length=20,
        choices=[("annual","Annual"),("semi_annual","Semi-annual"),("other","Other")],
        default="annual")
    fiscal_year = models.IntegerField()
    period_end = models.DateField(null=True, blank=True)

    # Extracted financials (MAD thousands)
    revenue = models.FloatField(null=True, blank=True)
    net_income = models.FloatField(null=True, blank=True)
    eps = models.FloatField(null=True, blank=True)
    dividend_per_share = models.FloatField(null=True, blank=True)
    total_assets = models.FloatField(null=True, blank=True)
    equity = models.FloatField(null=True, blank=True)

    # YoY growth (computed vs prior year)
    revenue_growth_pct = models.FloatField(null=True, blank=True)
    net_income_growth_pct = models.FloatField(null=True, blank=True)
    eps_growth_pct = models.FloatField(null=True, blank=True)

    # Source
    ammc_url = models.URLField(blank=True)
    pdf_url = models.URLField(blank=True)
    extracted_at = models.DateTimeField(auto_now_add=True)
    extraction_confidence = models.CharField(max_length=10, default="medium",
        choices=[("high","High"),("medium","Medium"),("low","Low")])
    raw_text_snippet = models.TextField(blank=True)  # for audit

    class Meta:
        ordering = ["-fiscal_year", "-period_end"]
        unique_together = [["ticker", "report_type", "fiscal_year"]]

    def __str__(self):
        return f"{self.ticker} {self.report_type} {self.fiscal_year}"
