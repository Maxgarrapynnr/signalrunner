"""
signalrunner/datasource.py

The market-data layer — the genuinely new concept versus PyRunner. Strategies
don't fetch their own data; this module pulls BVC quotes from a provider,
caches them as MarketDataSnapshot rows, and serves them to every evaluation.

Design goals:
- One provider pull serves all strategies evaluated in the same window
  (rate-limit protection + consistency).
- The provider is swappable: casabourse is v1; Medias24 or a paid feed can
  drop in behind the same `MarketDataProvider` interface.
- Network/import failures degrade gracefully — an evaluation fails cleanly
  rather than crashing the worker.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from signalrunner.models import MarketDataSnapshot

# How long a cached snapshot is considered fresh. Many strategies firing in the
# same scheduler tick reuse one pull instead of hammering the provider.
CACHE_TTL_SECONDS = getattr(settings, "MARKETDATA_CACHE_TTL_SECONDS", 300)


class DataSourceError(Exception):
    """Market data could not be fetched."""


# ──────────────────────────────────────────────
# Provider interface (swappable)
# ──────────────────────────────────────────────
class MarketDataProvider:
    """Interface every provider implements. A provider returns a dict keyed by
    ticker: {ticker: {"price": float, "pct_change": float, "volume": float,
    "raw": {...}}}."""

    name = "base"

    def fetch(self, tickers: list[str]) -> dict[str, dict]:
        raise NotImplementedError

    def history(self, ticker: str, start: str, end: str):
        """Return a price history (pandas DataFrame) for indicator strategies."""
        raise NotImplementedError


class CasabourseProvider(MarketDataProvider):
    """v1 provider backed by the `casabourse` PyPI library (BVC)."""

    name = "casabourse"

    def fetch(self, tickers: list[str]) -> dict[str, dict]:
        cb = _import_casabourse()
        try:
            df = cb.get_live_market_data()  # pandas DataFrame of the whole market
        except Exception as exc:
            raise DataSourceError(f"casabourse live fetch failed: {exc}") from exc

        out: dict[str, dict] = {}
        wanted = {t.upper() for t in tickers} if tickers else None
        for row in _iter_rows(df):
            tk = _row_ticker(row)
            if not tk:
                continue
            if wanted is not None and tk.upper() not in wanted:
                continue
            out[tk] = {
                "price": _num(row, ("price", "cours", "last", "dernier")),
                "pct_change": _num(row, ("pct_change", "variation", "var", "change")),
                "volume": _num(row, ("volume", "vol")),
                "raw": _row_to_dict(row),
            }
        return out

    def history(self, ticker: str, start: str, end: str):
        cb = _import_casabourse()
        try:
            return cb.get_historical_data_auto(ticker, start, end)
        except Exception as exc:
            raise DataSourceError(
                f"casabourse history fetch failed for {ticker}: {exc}"
            ) from exc


def get_provider() -> MarketDataProvider:
    """Return the configured provider. Swap here when adding new sources."""
    name = getattr(settings, "MARKETDATA_PROVIDER", "casabourse")
    if name == "casabourse":
        return CasabourseProvider()
    raise DataSourceError(f"unknown market-data provider: {name}")


# ──────────────────────────────────────────────
# Cached access — what the worker calls
# ──────────────────────────────────────────────
def get_quotes(tickers: list[str], *, force_refresh: bool = False) -> dict[str, dict]:
    """Return {ticker: quote-dict} for the requested tickers, using cached
    snapshots when fresh and pulling from the provider only for stale/missing
    ones. Persists fresh pulls as MarketDataSnapshot rows."""
    tickers = [t.upper() for t in tickers]
    result: dict[str, dict] = {}
    stale: list[str] = []

    cutoff = timezone.now() - timedelta(seconds=CACHE_TTL_SECONDS)
    if not force_refresh:
        for tk in tickers:
            snap = (
                MarketDataSnapshot.objects.filter(ticker=tk, fetched_at__gte=cutoff)
                .first()
            )
            if snap:
                result[tk] = _snap_to_quote(snap)
            else:
                stale.append(tk)
    else:
        stale = list(tickers)

    if stale:
        fetched = get_provider().fetch(stale)
        for tk in stale:
            q = fetched.get(tk)
            if q is None:
                continue  # provider had no data for this ticker; skip
            MarketDataSnapshot.objects.create(
                ticker=tk, price=q.get("price"), pct_change=q.get("pct_change"),
                volume=q.get("volume"), raw=q.get("raw", {}),
                source=get_provider().name,
            )
            result[tk] = q

    return result


def get_history(ticker: str, start: str, end: str):
    """Pass-through to the provider's history (used by indicator strategies)."""
    return get_provider().history(ticker, start, end)


def cleanup_snapshots() -> int:
    """Delete snapshots older than the retention window. Scheduled task.
    Retention is a tunable setting (SNAPSHOT_RETENTION_DAYS, default 30)."""
    days = getattr(settings, "SNAPSHOT_RETENTION_DAYS", 30)
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = MarketDataSnapshot.objects.filter(fetched_at__lt=cutoff).delete()
    return deleted


# ──────────────────────────────────────────────
# Helpers (tolerant of casabourse's column naming)
# ──────────────────────────────────────────────
def _import_casabourse():
    try:
        import casabourse as cb
        return cb
    except ImportError as exc:
        raise DataSourceError(
            "casabourse is not installed; `pip install casabourse`"
        ) from exc


def _snap_to_quote(snap: MarketDataSnapshot) -> dict:
    return {"price": snap.price, "pct_change": snap.pct_change,
            "volume": snap.volume, "raw": snap.raw}


def _iter_rows(df):
    """Yield rows from a pandas DataFrame as dicts, without hard-importing pandas."""
    if df is None:
        return
    # DataFrame duck-typing: to_dict('records') gives a list of row dicts.
    try:
        for rec in df.to_dict("records"):
            yield rec
    except AttributeError:
        # Already a list of dicts, or similar.
        for rec in df:
            yield rec


def _row_to_dict(row) -> dict:
    return {str(k): _jsonable(v) for k, v in row.items()}


def _row_ticker(row) -> str | None:
    for key in ("ticker", "Ticker", "symbol", "Symbol", "code", "Code", "valeur",
                "Valeur", "instrument", "Instrument"):
        if key in row and row[key] not in (None, ""):
            return str(row[key]).strip()
    return None


def _num(row, keys) -> float | None:
    """Find the first present numeric-ish column among `keys` (case-insensitive)."""
    lower = {str(k).lower(): v for k, v in row.items()}
    for k in keys:
        if k.lower() in lower:
            return _to_float(lower[k.lower()])
    return None


def _to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)
