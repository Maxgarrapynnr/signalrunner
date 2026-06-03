# SignalRunner

A self-hosted trading-signal platform for the Bourse de Casablanca (BVC).
Define a strategy, run it on a schedule against BVC market data, and receive
buy/sell alerts on Telegram.

> **Not financial advice.** SignalRunner generates alerts from user-defined rules.
> It is a personal tooling project with no guarantee of data accuracy or
> signal profitability.

## Features

- **Three strategy kinds** — simple rules (price/% thresholds), technical
  indicators (RSI, MA crossover, MACD), and custom uploaded Python strategies.
- **BVC market data** via the `casabourse` library; shared snapshot cache so
  many strategies share one provider pull per evaluation window.
- **Scheduled + on-demand** evaluation (django-q2 scheduler, gated to market hours).
- **Telegram alerts** with buy/sell direction, ticker, price, and the reason the
  strategy fired. Retry with exponential backoff.
- **Full evaluation history** — every run's status, indicator values computed,
  signals emitted, and a prefixed log.
- **Encrypted secrets** (Fernet) for the Telegram bot token and any provider keys.
- Single Docker container, SQLite, no Redis.

## Quick start (Docker)

```bash
git clone https://github.com/Maxgarrapynnr/signalrunner.git
cd signalrunner
cp .env.example .env
# Fill in SECRET_KEY and ENCRYPTION_KEY — see commands below
docker compose up -d
```

**Generate the required keys:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"        # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

**Create your admin account:**
```bash
docker compose exec web python manage.py createsuperuser
```

Open `http://localhost:8000` and sign in.

## Telegram setup

1. Create a bot via [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your chat ID via [@userinfobot](https://t.me/userinfobot).
3. Go to `/admin/signalrunner/secret/` and add two secrets:
   - `TELEGRAM_BOT_TOKEN` — your bot token
   - `TELEGRAM_CHAT_ID` — your chat ID

## Deploying to Coolify

See the [DocRunner Coolify notes](https://github.com/hassancs91/PyRunner) for
the same gotchas: use `docker-compose.yaml` (not `.yml`), `expose` not `ports`,
and fill the environment variables in Coolify's dashboard.

## Strategy kinds

**Simple rule** — triggers when a field crosses a threshold:
```
pct_change >= 3.0  → buy
price < 100        → sell
```

**Indicator** — RSI, moving-average crossover, or MACD:
```
RSI(14) < 30  → buy (oversold)
MA(20) crosses above MA(50)  → buy (golden cross)
```

**Custom Python** — upload a strategy that receives `quotes` and appends to `signals`:
```python
for ticker, q in quotes.items():
    if q.get('pct_change', 0) > 5:
        signals.append({'ticker': ticker, 'direction': 'buy',
                        'reason': {'pct': q['pct_change']}})
```
Runs in a sandboxed subprocess with a 30-second timeout.

## Architecture

```
trigger (schedule / on-demand) → Evaluation (QUEUED)
  → [django-q2 worker]
      → datasource.py  (casabourse + snapshot cache)
      → tasks.py       (rule / indicator / custom engine)
      → Signal rows
      → delivery.py    (Telegram, with retry)
```

Built on PyRunner's architecture (github.com/hassancs91/PyRunner) — Django +
django-q2 + SQLite + single container.
