"""
signalrunner/fundamentals.py

Fundamental data pipeline for BVC stocks:

1. refresh_market_data(tickers)   — pull price/market-cap from casabourse
2. fetch_ammc_pdfs(tickers)       — find latest earnings PDFs on AMMC
3. parse_earnings_pdf(url)        — extract key numbers from a PDF
4. compute_scores(tickers)        — produce composite fundamental scores
5. refresh_all(tickers)           — run the full pipeline (scheduled daily)

Honest limitations:
- PDF extraction uses keyword matching on French text. Confidence varies.
- BVC does not expose P/E or EPS via API — these come from PDFs only.
- Data quality is flagged per snapshot ('full' / 'partial' / 'price_only').
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from io import BytesIO

import requests
from django.utils import timezone

from signalrunner import datasource
from signalrunner.models import (
    EarningsExtract, FundamentalScore, StockFundamentals,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
}
REQUEST_TIMEOUT = 30

# BVC market average benchmarks for scoring (update annually)
MARKET_AVG_YIELD = 4.5    # % — MASI average dividend yield
MARKET_AVG_PE = 19.0      # MASI average P/E


# ── 1. Market data refresh ────────────────────────────────────────────────────

def refresh_market_data(tickers: list[str]) -> dict[str, StockFundamentals]:
    """Pull live price + market cap from casabourse and upsert StockFundamentals."""
    today = date.today()
    results = {}

    # Get live quotes
    try:
        quotes = datasource.get_quotes(tickers, force_refresh=True)
    except Exception as exc:
        print(f"[fundamentals] market data fetch failed: {exc}")
        quotes = {}

    # Get full market data for market cap and 52w range
    market_data = _fetch_full_market_data()

    for ticker in tickers:
        ticker = ticker.upper()
        q = quotes.get(ticker, {})
        md = market_data.get(ticker, {})

        fund, _ = StockFundamentals.objects.get_or_create(
            ticker=ticker, date=today,
            defaults={"name": md.get("name", ticker)}
        )
        fund.price = q.get("price") or md.get("price")
        fund.market_cap = md.get("market_cap")
        fund.shares_outstanding = md.get("shares")
        fund.week_52_high = md.get("high_52w")
        fund.week_52_low = md.get("low_52w")
        fund.name = md.get("name", ticker)

        # Try to fill computed ratios from existing earnings data
        _fill_from_earnings(fund)
        fund.compute_ratios()
        fund.save()
        results[ticker] = fund
        print(f"[fundamentals] {ticker}: price={fund.price} mktcap={fund.market_cap}")

    return results


def _fetch_full_market_data() -> dict[str, dict]:
    """Get market cap, 52w range, shares from casabourse live data."""
    try:
        import casabourse as cb
        df = cb.get_live_market_data()
        if df is None:
            return {}
    except Exception:
        return {}

    result = {}
    for row in df.to_dict("records"):
        ticker = str(row.get("Symbole", "") or "").strip().upper()
        if not ticker:
            continue
        result[ticker] = {
            "name": str(row.get("Libellé FR") or row.get("Instrument", ticker)),
            "price": _to_float(row.get("Dernier cours")),
            "market_cap": _to_float(row.get("Capitalisation")),
            "shares": _to_float(row.get("Nombre de titres")),
            "high_52w": _to_float(row.get("+ haut jour")),  # daily high as proxy
            "low_52w": _to_float(row.get("+ bas jour")),
        }
    return result


def _fill_from_earnings(fund: StockFundamentals) -> None:
    """Fill EPS/dividend from the most recent EarningsExtract for this ticker."""
    latest = EarningsExtract.objects.filter(
        ticker=fund.ticker, report_type="annual"
    ).order_by("-fiscal_year").first()
    if not latest:
        return
    if fund.eps is None:
        fund.eps = latest.eps
    if fund.dividend_per_share is None:
        fund.dividend_per_share = latest.dividend_per_share
    if fund.revenue is None:
        fund.revenue = latest.revenue
    if fund.net_income is None:
        fund.net_income = latest.net_income
    if fund.fiscal_year is None:
        fund.fiscal_year = latest.fiscal_year
    fund.data_quality = "full" if latest.eps and latest.dividend_per_share else "partial"


# ── 2. AMMC PDF discovery ─────────────────────────────────────────────────────

def fetch_ammc_pdfs(tickers: list[str]) -> dict[str, list[dict]]:
    """
    Search AMMC publications page for the latest annual report PDF per ticker.
    Returns {ticker: [{"title":..., "pdf_url":..., "date":...}, ...]}
    """
    results = {}
    for ticker in tickers:
        ticker = ticker.upper()
        try:
            pdfs = _search_ammc_for_ticker(ticker)
            results[ticker] = pdfs
            if pdfs:
                print(f"[fundamentals] {ticker}: found {len(pdfs)} PDF(s) on AMMC")
            else:
                print(f"[fundamentals] {ticker}: no PDFs found on AMMC")
        except Exception as exc:
            print(f"[fundamentals] {ticker}: AMMC search failed: {exc}")
            results[ticker] = []
    return results


def _search_ammc_for_ticker(ticker: str) -> list[dict]:
    """
    Fetch the AMMC publications page filtered by company and look for
    annual report / états financiers PDFs.
    """
    # AMMC publications page for emetteurs
    url = "https://www.ammc.ma/fr/publications-des-emetteurs"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    html = resp.text

    # Find links that contain the ticker or company name context
    # and point to PDFs (états financiers, rapport annuel)
    company_name = _ticker_to_company_fragment(ticker)
    pdfs = []

    # Pattern: PDF links near the company name
    pdf_pattern = re.compile(
        r'href="([^"]+\.pdf[^"]*)"[^>]*>([^<]*(?:financier|rapport|résultat|annual)[^<]*)',
        re.IGNORECASE
    )

    # Also look for standard AMMC media PDF URLs
    media_pattern = re.compile(
        r'(https://media\.casablanca-bourse\.com[^\s"\'<>]+\.pdf)',
        re.IGNORECASE
    )

    # Find context blocks containing company name
    if company_name:
        name_lower = company_name.lower()
        html_lower = html.lower()
        idx = 0
        while True:
            pos = html_lower.find(name_lower, idx)
            if pos == -1:
                break
            # Extract a window around this mention and look for PDF links
            window = html[max(0, pos - 200):pos + 800]
            for m in pdf_pattern.finditer(window):
                href, title = m.group(1), m.group(2).strip()
                if not href.startswith("http"):
                    href = "https://www.ammc.ma" + href
                pdfs.append({"title": title, "pdf_url": href, "date": ""})
            for m in media_pattern.finditer(window):
                href = m.group(1)
                pdfs.append({"title": f"BVC PDF {ticker}", "pdf_url": href, "date": ""})
            idx = pos + 1

    # Deduplicate by URL
    seen = set()
    unique = []
    for p in pdfs:
        if p["pdf_url"] not in seen:
            seen.add(p["pdf_url"])
            unique.append(p)
    return unique[:5]  # max 5 most recent


def _ticker_to_company_fragment(ticker: str) -> str:
    """Map BVC ticker to a company name fragment for AMMC search."""
    mapping = {
        "IAM": "itissalat", "ATW": "attijariwafa", "BCP": "populaire",
        "BOA": "africa", "CIH": "immobilier", "LBV": "label vie",
        "MNG": "managem", "CSR": "cosumar", "LHM": "holcim",
        "HPS": "hightech payment",
    }
    return mapping.get(ticker.upper(), "")


# ── 3. PDF parser ─────────────────────────────────────────────────────────────

def parse_earnings_pdf(ticker: str, pdf_url: str, fiscal_year: int | None = None,
                       report_type: str = "annual") -> EarningsExtract | None:
    """
    Download and parse an AMMC earnings PDF.
    Extracts: revenue, net income, EPS (BNA), dividend per share.
    Uses keyword matching on French financial statement text.
    Returns an EarningsExtract instance (not yet saved).
    """
    print(f"[fundamentals] parsing PDF: {pdf_url}")
    try:
        resp = requests.get(pdf_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[fundamentals] PDF download failed: {exc}")
        return None

    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            text = "\n".join(
                page.extract_text() or "" for page in pdf.pages[:20]
            )
    except Exception as exc:
        print(f"[fundamentals] PDF parse failed: {exc}")
        return None

    if not text.strip():
        print(f"[fundamentals] PDF has no extractable text (scanned image?)")
        return None

    # Detect fiscal year from text if not provided
    if fiscal_year is None:
        fiscal_year = _extract_fiscal_year(text)

    extract = EarningsExtract(
        ticker=ticker.upper(),
        report_type=report_type,
        fiscal_year=fiscal_year or date.today().year - 1,
        pdf_url=pdf_url,
        raw_text_snippet=text[:500],
    )

    extract.revenue = _extract_financial(text, [
        "chiffre d'affaires", "produits d'exploitation", "revenus",
        "produit net bancaire", "total produits", "chiffre d affaires",
    ])
    extract.net_income = _extract_financial(text, [
        "résultat net", "résultat de l'exercice", "bénéfice net",
        "résultat net part du groupe", "résultat net consolidé",
    ])
    extract.eps = _extract_financial(text, [
        "bna", "bénéfice net par action", "résultat net par action",
        "bénéfice par action",
    ])
    extract.dividend_per_share = _extract_financial(text, [
        "dividende par action", "dividende unitaire", "dividende brut",
        "dividende de", "dividende proposé",
    ])
    extract.total_assets = _extract_financial(text, [
        "total bilan", "total actif", "total des actifs",
    ])
    extract.equity = _extract_financial(text, [
        "capitaux propres", "fonds propres", "capitaux propres part du groupe",
    ])

    # Assess confidence
    filled = sum(1 for v in [extract.revenue, extract.net_income,
                              extract.eps, extract.dividend_per_share] if v)
    extract.extraction_confidence = "high" if filled >= 3 else "medium" if filled >= 1 else "low"

    print(f"[fundamentals] {ticker} {fiscal_year}: "
          f"revenue={extract.revenue} net_income={extract.net_income} "
          f"eps={extract.eps} div={extract.dividend_per_share} "
          f"confidence={extract.extraction_confidence}")
    return extract


def _extract_financial(text: str, keywords: list[str]) -> float | None:
    """
    Find a financial figure in French PDF text by looking for a keyword
    followed by a number (MAD thousands or millions).
    Returns the value in MAD (or None if not found).
    """
    text_lower = text.lower()
    for kw in keywords:
        idx = text_lower.find(kw.lower())
        if idx == -1:
            continue
        # Look for a number within 200 chars after the keyword
        window = text[idx:idx + 200]
        # Matches numbers like: 1 234 567 or 1,234,567 or 1.234.567
        nums = re.findall(
            r'(-?\s*\d[\d\s]{0,15}\d)\s*(?:000|MDH|MMAD|KMAD|%)?',
            window
        )
        for n in nums:
            v = _to_float(n)
            if v is not None and abs(v) > 0.01:
                return v
    return None


def _extract_fiscal_year(text: str) -> int | None:
    """Extract the fiscal year from PDF text."""
    m = re.search(r'exercice\s+(?:clos\s+le\s+\d{1,2}[/\-]\d{1,2}[/\-])?(\d{4})', text.lower())
    if m:
        return int(m.group(1))
    # Look for recent years
    for year in range(date.today().year, date.today().year - 5, -1):
        if str(year) in text:
            return year
    return None


# ── 4. Fundamental scoring ────────────────────────────────────────────────────

def compute_scores(tickers: list[str]) -> list[FundamentalScore]:
    """
    Compute composite fundamental scores for all tickers.
    Scores are relative (each stock vs the BVC universe), not absolute.
    """
    today = date.today()
    fund_data = {}
    for ticker in tickers:
        f = StockFundamentals.objects.filter(ticker=ticker).order_by("-date").first()
        if f:
            fund_data[ticker] = f

    scores = []
    for ticker in tickers:
        ticker = ticker.upper()
        f = fund_data.get(ticker)
        score, _ = FundamentalScore.objects.get_or_create(ticker=ticker)
        score.date = today
        summary = {}

        # ── Yield score (0–10) ────────────────────────────────────────────────
        yield_score = 0.0
        if f and f.dividend_yield:
            # 5 = market average, 10 = 2× market average
            yield_score = min(10.0, (f.dividend_yield / MARKET_AVG_YIELD) * 5)
            summary["dividend_yield"] = f"{f.dividend_yield:.1f}%"
            summary["yield_score_note"] = (
                "above market avg" if f.dividend_yield > MARKET_AVG_YIELD
                else "below market avg"
            )
        score.yield_score = round(yield_score, 2)

        # ── Value score (0–10) — lower P/E = better ───────────────────────────
        value_score = 5.0  # default: neutral
        if f and f.pe_ratio and f.pe_ratio > 0:
            # 5 = market P/E, 10 = half market P/E (very cheap), 0 = 2× market P/E
            value_score = max(0.0, min(10.0, (MARKET_AVG_PE / f.pe_ratio) * 5))
            summary["pe_ratio"] = f"{f.pe_ratio:.1f}×"
            summary["value_note"] = (
                "cheap vs market" if f.pe_ratio < MARKET_AVG_PE else "expensive vs market"
            )
        score.value_score = round(value_score, 2)

        # ── Consistency score — dividend payment history ───────────────────────
        years_with_dividend = EarningsExtract.objects.filter(
            ticker=ticker, report_type="annual",
            dividend_per_share__gt=0,
        ).count()
        consistency_score = min(10.0, years_with_dividend * 2.5)  # 4 years = 10
        summary["dividend_years"] = years_with_dividend
        score.consistency_score = round(consistency_score, 2)

        # ── Momentum score — 3-month price performance ────────────────────────
        momentum_score = 5.0
        try:
            from signalrunner import datasource as ds
            end = today.isoformat()
            start = (today - timedelta(days=120)).isoformat()
            hist = ds.get_history(ticker, start, end)
            if hist is not None:
                closes = [float(x) for x in hist["close"].tolist() if x]
                if len(closes) >= 63:
                    mom = (closes[-1] - closes[-63]) / closes[-63] * 100
                    # 5 = flat, 10 = +20%, 0 = -20%
                    momentum_score = max(0.0, min(10.0, 5 + (mom / 20) * 5))
                    summary["momentum_3m"] = f"{mom:+.1f}%"
        except Exception:
            pass
        score.momentum_score = round(momentum_score, 2)

        # ── Composite ─────────────────────────────────────────────────────────
        # Weights: yield 35%, value 30%, consistency 25%, momentum 10%
        total = (
            score.yield_score * 0.35 +
            score.value_score * 0.30 +
            score.consistency_score * 0.25 +
            score.momentum_score * 0.10
        )
        score.total_score = round(total, 2)
        score.summary = summary
        score.save()
        scores.append(score)
        print(f"[fundamentals] {ticker}: score={score.total_score:.1f} "
              f"(yield={score.yield_score} value={score.value_score} "
              f"consistency={score.consistency_score} mom={score.momentum_score})")

    # Assign ranks
    ranked = sorted(scores, key=lambda s: s.total_score, reverse=True)
    for i, s in enumerate(ranked, 1):
        s.rank = i
        s.save(update_fields=["rank"])

    return ranked


# ── 5. Full pipeline ──────────────────────────────────────────────────────────

def refresh_all(tickers: list[str] | None = None) -> None:
    """
    Run the full fundamentals pipeline for all tracked tickers.
    Scheduled to run once per trading day at 16:00 Casablanca time.
    """
    from signalrunner.models import Strategy
    if tickers is None:
        # Gather all unique tickers from enabled strategies
        all_tickers: set[str] = set()
        for st in Strategy.objects.filter(enabled=True):
            all_tickers.update(t.upper() for t in (st.tickers or []))
        tickers = list(all_tickers)

    if not tickers:
        print("[fundamentals] no tickers to process")
        return

    print(f"[fundamentals] refreshing {len(tickers)} tickers: {tickers}")

    # Step 1: market data
    refresh_market_data(tickers)

    # Step 2: AMMC PDF search + parse (only for tickers missing EPS/dividend)
    missing = [
        t for t in tickers
        if not EarningsExtract.objects.filter(ticker=t, report_type="annual").exists()
    ]
    if missing:
        print(f"[fundamentals] fetching AMMC PDFs for: {missing}")
        pdf_map = fetch_ammc_pdfs(missing)
        for ticker, pdfs in pdf_map.items():
            for pdf in pdfs[:1]:  # try first PDF only
                extract = parse_earnings_pdf(ticker, pdf["pdf_url"])
                if extract and extract.extraction_confidence != "low":
                    try:
                        extract.save()
                        _fill_from_earnings(
                            StockFundamentals.objects.filter(
                                ticker=ticker).order_by("-date").first()
                        )
                    except Exception as exc:
                        print(f"[fundamentals] save failed for {ticker}: {exc}")

    # Step 3: scoring
    compute_scores(tickers)
    print("[fundamentals] pipeline complete")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace("\u202f", "").replace(",", ".")
    s = s.replace("\xa0", "")
    try:
        return float(s)
    except ValueError:
        return None
