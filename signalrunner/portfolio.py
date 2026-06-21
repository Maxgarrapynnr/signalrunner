"""
signalrunner/portfolio.py

Portfolio backtester for a diversified buy-and-hold BVC strategy.

Philosophy (learned the hard way):
- We do NOT predict prices. We hold a diversified basket of quality dividend
  payers and let dividends + long-term drift compound.
- Every result is benchmarked against MASI buy-and-hold. If we don't beat
  (or at least match with less risk) just holding the index, the portfolio
  has no reason to exist.
- We do NOT curve-fit weights until the backtest looks perfect. We test a
  small number of *defensible* allocation rules and report them honestly,
  including the ones that underperform.

Returns modelled:
  total_return = price_return + dividend_return
  Dividends are taken from EarningsExtract (real, verified per-share amounts)
  and assumed paid once per year.

Honest limitations:
  - Only ~18 months of usable casabourse history → results are indicative,
    not statistically robust. Reported with that caveat every time.
  - No transaction costs / taxes modelled (real returns slightly lower).
  - Dividend timing approximated as annual (BVC mostly pays once/year).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from signalrunner import datasource
from signalrunner.models import EarningsExtract, StockFundamentals


# ── Sector classification (for diversification) ────────────────────────────────
SECTORS = {
    "IAM": "telecom",
    "ATW": "banking", "BCP": "banking", "BOA": "banking", "CIH": "banking",
    "LBV": "retail",
    "MNG": "mining",
    "CSR": "agro",
    "LHM": "materials",
    "HPS": "technology",
    "GAZ": "energy",
    "CMA": "materials",   # Ciments du Maroc
}


@dataclass
class Holding:
    ticker: str
    weight: float
    sector: str = ""
    entry_price: float | None = None
    exit_price: float | None = None
    price_return_pct: float | None = None
    dividend_return_pct: float | None = None
    total_return_pct: float | None = None


@dataclass
class PortfolioResult:
    name: str
    start: str
    end: str
    holdings: list[Holding] = field(default_factory=list)
    portfolio_return_pct: float | None = None
    portfolio_dividend_pct: float | None = None
    portfolio_total_pct: float | None = None
    annualized_pct: float | None = None
    masi_return_pct: float | None = None
    excess_vs_masi: float | None = None
    volatility_pct: float | None = None
    sectors_count: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "period": f"{self.start} → {self.end}",
            "total_return_pct": self.portfolio_total_pct,
            "annualized_pct": self.annualized_pct,
            "price_return_pct": self.portfolio_return_pct,
            "dividend_return_pct": self.portfolio_dividend_pct,
            "masi_return_pct": self.masi_return_pct,
            "excess_vs_masi": self.excess_vs_masi,
            "volatility_pct": self.volatility_pct,
            "sectors": self.sectors_count,
            "holdings": [
                {"ticker": h.ticker, "weight": round(h.weight, 3), "sector": h.sector,
                 "total_return_pct": h.total_return_pct,
                 "price_pct": h.price_return_pct, "div_pct": h.dividend_return_pct}
                for h in self.holdings
            ],
            "notes": self.notes,
        }


def backtest_portfolio(name: str, weights: dict[str, float],
                       start: str, end: str) -> PortfolioResult:
    """
    Backtest a weighted buy-and-hold portfolio over [start, end].
    weights: {ticker: weight}; weights are normalised to sum to 1.
    """
    total_w = sum(weights.values())
    weights = {t.upper(): w / total_w for t, w in weights.items()}

    result = PortfolioResult(name=name, start=start, end=end)
    sectors = set()
    weighted_price_return = 0.0
    weighted_div_return = 0.0
    per_holding_returns = []

    for ticker, weight in weights.items():
        sector = SECTORS.get(ticker, "other")
        sectors.add(sector)
        h = Holding(ticker=ticker, weight=weight, sector=sector)

        closes = _load_closes(ticker, start, end)
        if not closes or len(closes) < 2:
            result.notes.append(f"{ticker}: no price data, excluded from return")
            h.total_return_pct = None
            result.holdings.append(h)
            continue

        h.entry_price = closes[0]
        h.exit_price = closes[-1]
        h.price_return_pct = (closes[-1] - closes[0]) / closes[0] * 100

        # Dividend return: annual dividend per share / entry price, × years held
        years = _years_between(start, end)
        div_per_share = _annual_dividend(ticker)
        if div_per_share and closes[0]:
            h.dividend_return_pct = (div_per_share / closes[0] * 100) * years
        else:
            h.dividend_return_pct = 0.0

        h.total_return_pct = h.price_return_pct + h.dividend_return_pct
        weighted_price_return += h.price_return_pct * weight
        weighted_div_return += h.dividend_return_pct * weight
        per_holding_returns.append(h.total_return_pct)
        result.holdings.append(h)

    result.portfolio_return_pct = round(weighted_price_return, 2)
    result.portfolio_dividend_pct = round(weighted_div_return, 2)
    result.portfolio_total_pct = round(weighted_price_return + weighted_div_return, 2)
    result.sectors_count = len(sectors)

    years = _years_between(start, end)
    if years > 0 and result.portfolio_total_pct is not None:
        # annualized = (1+total)^(1/years) - 1
        gr = 1 + result.portfolio_total_pct / 100
        result.annualized_pct = round((gr ** (1 / years) - 1) * 100, 2) if gr > 0 else None

    # Volatility = stdev of per-holding total returns (cross-sectional dispersion proxy)
    if len(per_holding_returns) > 1:
        result.volatility_pct = round(statistics.stdev(per_holding_returns), 2)

    # Benchmark vs MASI
    masi = _masi_return(start, end)
    result.masi_return_pct = masi
    if masi is not None and result.portfolio_total_pct is not None:
        result.excess_vs_masi = round(result.portfolio_total_pct - masi, 2)

    return result


# ── Candidate portfolios (defensible rules, NOT curve-fitted) ───────────────────

def candidate_portfolios() -> dict[str, dict[str, float]]:
    """
    A small set of *defensible* allocation rules. We test each honestly and
    report all of them — including underperformers — rather than tuning until
    one looks perfect.
    """
    return {
        # 1. Equal-weight across sectors (max diversification, no view)
        "Equal-Weight Diversified": {
            "IAM": 1, "ATW": 1, "BCP": 1, "LBV": 1,
            "MNG": 1, "CSR": 1, "LHM": 1, "HPS": 1,
        },
        # 2. Dividend-tilted (overweight the high yielders for the 5% income floor)
        "Dividend Income": {
            "IAM": 3, "ATW": 2, "BCP": 2, "LHM": 2, "GAZ": 2,
            "CIH": 1, "BOA": 1,
        },
        # 3. Blue-chip banks + telecom (the liquid, stable core)
        "Blue-Chip Core": {
            "IAM": 2, "ATW": 2, "BCP": 2, "BOA": 1, "CIH": 1,
        },
        # 4. Balanced: stability core + small growth/mining sleeve
        "Balanced 70/30": {
            "IAM": 2, "ATW": 2, "BCP": 1, "LHM": 1, "GAZ": 1,  # 70% stable
            "MNG": 1, "HPS": 1,                                  # 30% growth/cyclical
        },
    }


def find_best_portfolio(start: str, end: str) -> dict:
    """
    Backtest all candidate portfolios over the period, benchmark each against
    MASI, and return them ranked by total return — with the honesty caveats
    attached. Does NOT curve-fit; reports the full field.
    """
    results = []
    for name, weights in candidate_portfolios().items():
        r = backtest_portfolio(name, weights, start, end)
        results.append(r)

    results.sort(key=lambda r: (r.portfolio_total_pct or -999), reverse=True)

    years = _years_between(start, end)
    out = {
        "period": f"{start} → {end}",
        "years": round(years, 2),
        "masi_return_pct": results[0].masi_return_pct if results else None,
        "portfolios": [r.summary() for r in results],
        "caveats": [
            f"Based on ~{round(years*250)} trading days of casabourse history — "
            "indicative, not statistically robust.",
            "No transaction costs or taxes modelled; real returns slightly lower.",
            "Dividends approximated as annual from verified per-share data.",
            "Past performance does not predict future results. Not financial advice.",
        ],
    }
    return out


# ── Data helpers ────────────────────────────────────────────────────────────────

def _load_closes(ticker: str, start: str, end: str):
    try:
        df = datasource.get_history(ticker, start, end)
    except Exception:
        return None
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


def _annual_dividend(ticker: str) -> float | None:
    """Most recent verified annual dividend per share."""
    ee = (EarningsExtract.objects
          .filter(ticker=ticker.upper(), report_type="annual",
                  dividend_per_share__isnull=False)
          .order_by("-fiscal_year").first())
    if ee and ee.dividend_per_share:
        return ee.dividend_per_share
    sf = (StockFundamentals.objects
          .filter(ticker=ticker.upper(), dividend_per_share__isnull=False)
          .order_by("-date").first())
    return sf.dividend_per_share if sf else None


def _masi_return(start: str, end: str) -> float | None:
    """MASI index return over the period (benchmark). Tries common symbols."""
    for sym in ("MASI", "MASI.MA", "^MASI"):
        closes = _load_closes(sym, start, end)
        if closes and len(closes) > 1 and closes[0]:
            return round((closes[-1] - closes[0]) / closes[0] * 100, 2)
    return None


def _years_between(start: str, end: str) -> float:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    return max((d1 - d0).days / 365.25, 0.01)
