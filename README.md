# Real-Time Weather Data Pipeline

A portfolio data engineering project that ingests weather data from OpenWeatherMap, persists raw API payloads to a data lake, transforms them into partitioned Parquet staging datasets, loads analytics-ready tables into PostgreSQL, monitors data freshness and anomalies, and publishes a live static dashboard to Cloudflare Pages.

**Live dashboard**: deployed via Cloudflare Pages (see [Automation](#automation))

Supported data sources:
- Current weather (`/weather`) — ingested hourly
- 5-day / 3-hour forecast (`/forecast`) — ingested every 3 hours

Weather alerts are intentionally deferred: no suitable free API tier exists for this use case.

---

## Architecture

```
OpenWeatherMap API
    → extract.py          (raw JSON per city → data_lake/raw/{type}/{city}/{Y}/{M}/{D}/{ts}.json)
    → staging_transform.py (raw JSON → data_lake/staging/{type}/city={city}/year={Y}/month={MM}/data.parquet)
    → load.py             (staging Parquet → PostgreSQL: weather_history / weather_forecast)

monitor.py   — runs independently (hourly); checks freshness and anomalies; emails alerts
dashboard.py — generates a self-contained static HTML page; deployed to Cloudflare Pages daily
```

`orchestrator.py` drives the Extract → Transform → Load sequence and emits a structured `RUN_SUMMARY` log line per data type. Each stage is fault-isolated per city — one city failing does not abort others.

Key design decisions:

- **Storage abstraction** (`storage.py`): all file I/O goes through a single interface that switches between local filesystem and Cloudflare R2 based on `STORAGE_BACKEND=local|r2`. Callers never touch boto3 or file paths directly.
- **Retry** (`retry.py`): `@run_with_retry` (Tenacity) retries transient API and storage failures with exponential backoff — 4 attempts, 0.5 s base, 2× multiplier, 2 s cap.
- **Incremental processing**: `staging_transform.py` tracks a high-water mark per city/month; only new raw files are re-processed. `load.py` queries `MAX(timestamp_utc)` per city before reading staging to skip already-loaded months.
- **Idempotency**: staging deduplicates on `(city, timestamp)` / `(city, fetched_at, forecast_for)` before writing Parquet; the DB enforces `UNIQUE` constraints with `ON CONFLICT DO NOTHING`.
- **Validation** (`validators/engine.py`): rules are declared in `Documentation/validation_rules.json` (required columns, min row count, null rate thresholds, uniqueness keys) and run after each city's transform.
- **DB timeouts**: all `psycopg2.connect()` calls include `connect_timeout=30 s`, `statement_timeout=5 min`, `lock_timeout=30 s` via `config.DATABASE` to prevent silent TCP hangs.

---

## Features

- Multi-city ingestion from a `cities` DB table (35 cities seeded)
- Current weather and forecast ETL paths, independently scheduled
- Raw JSON → partitioned Parquet → PostgreSQL lakehouse layers
- Duplicate protection at both staging (dedup) and DB (`ON CONFLICT DO NOTHING`) layers
- Declarative data validation with configurable null rate and uniqueness thresholds
- Local filesystem and Cloudflare R2 storage backends with a unified interface
- Hourly freshness and anomaly monitoring with Gmail email alerts
- Self-contained static HTML dashboard (no JS dependencies) with inline SVG charts deployed to Cloudflare Pages: multi-city temperature ribbon, cross-city anomaly band, forecast-accuracy-by-horizon (MAE), temperature ranking, ingestion volume, and a per-city freshness table
- 149 unit tests + Docker-gated integration tests (current + forecast); CI on every push / PR

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.10+ |
| Data source | OpenWeatherMap API |
| Analytics DB | PostgreSQL (Supabase-compatible) |
| Staging format | Parquet (via pandas) |
| Object storage | Cloudflare R2 / local filesystem (boto3) |
| Retry | Tenacity |
| Scheduling | GitHub Actions |
| Dashboard hosting | Cloudflare Pages |
| Alerting | Gmail SMTP (smtplib) |

---

## Project Layout

```
orchestrator.py          # CLI entry point; drives E→T→L per data type
extract.py               # OpenWeatherMap API extraction with retry
staging_transform.py     # Raw JSON → Parquet; high-water marks; validation
load.py                  # Staging → PostgreSQL; idempotent bulk inserts
monitor.py               # Freshness + anomaly checks; Gmail alerting
dashboard.py             # Static HTML dashboard generator
storage.py               # Local / R2 storage repository
retry.py                 # @run_with_retry decorator (Tenacity)
config.py                # Environment-based configuration (DB, R2, API key)
utils.py                 # Shared helpers (city mapping)
clients.py               # Singleton boto3 S3 client
validators/
  engine.py              # Declarative validation runner
  Documentation/
    validation_rules.json
migrations/              # SQL schema and seed migrations
.github/workflows/
  current_pipeline.yml   # Hourly current weather pipeline
  forecast_pipeline.yml  # 3-hourly forecast pipeline
  monitor.yml            # Hourly monitoring + anomaly checks
  publish_dashboard.yml  # Daily dashboard build + Cloudflare Pages deploy
  tests.yml              # Unit + integration tests on push/PR
tests/                   # 149 unit tests + 2 Docker-gated integration tests
```

---

## Setup

### 1. Clone and install

```bash
git clone <repo-url>
cd weather-data-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Create a `.env` file (or export in your shell):

```bash
# OpenWeatherMap
API_KEY=your_openweathermap_api_key

# PostgreSQL
DB_USER=your_postgres_user
DB_PASSWORD=your_postgres_password
DB_HOST=your_postgres_host
DB_PORT=5432
DB_NAME=your_postgres_db
PGSSLMODE=require          # use 'disable' with local Docker Postgres

# Optional DB timeout overrides (defaults shown)
DB_CONNECT_TIMEOUT=30
DB_STATEMENT_TIMEOUT_MS=300000
DB_LOCK_TIMEOUT_MS=30000

# Storage backend
STORAGE_BACKEND=local      # or 'r2'
DATA_LAKE_ROOT=data_lake

# Required when STORAGE_BACKEND=r2
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT_URL=...
R2_BUCKET_NAME=...

# Optional: Gmail alerting (monitor.py skips email silently if unset)
GMAIL_USER=your_gmail_address@gmail.com
GMAIL_APP_PASSWORD=your_app_password   # generate at myaccount.google.com/apppasswords
ALERT_EMAIL_TO=recipient@example.com   # defaults to GMAIL_USER if unset or empty
```

### 3. Apply database migrations

```bash
psql $DATABASE_URL -f migrations/001_create_tables.sql
# ... run all migrations in order
```

### 4. Local Postgres via Docker (optional)

```bash
docker compose up -d
# then set DB_* vars to match docker-compose.yml and PGSSLMODE=disable
```

---

## Running

```bash
# Full pipeline (both data types)
python orchestrator.py --data-types current forecast

# One data type
python orchestrator.py --data-types current
python orchestrator.py --data-types forecast

# Run monitoring checks (freshness + anomaly; emails if GMAIL_USER is set)
python monitor.py --data-types current forecast

# Generate the static dashboard HTML
python dashboard.py --output docs/index.html
```

---

## Tests

```bash
# All unit tests
python -m pytest -q

# Single file
python -m pytest tests/test_monitor.py -v

# Integration tests (requires Docker Postgres running)
docker compose up -d
RUN_INTEGRATION_TESTS=1 pytest tests/test_integration_current.py tests/test_integration_forecast.py -v
```

---

## Automation

All scheduled workflows run against the production PostgreSQL instance using GitHub Actions secrets.

| Workflow | Schedule | What it does |
|----------|----------|-------------|
| `current_pipeline.yml` | Every hour at :17 (`17 * * * *`) | Runs `orchestrator.py --data-types current` |
| `forecast_pipeline.yml` | Every 3 hours at :17 (`17 */3 * * *`) | Runs `orchestrator.py --data-types forecast` |
| `monitor.yml` | :50 past every hour | Runs `monitor.py`; emails alert if freshness/anomaly warnings found |
| `publish_dashboard.yml` | Daily at 07:00 UTC | Generates dashboard HTML and deploys to Cloudflare Pages via `wrangler` |
| `tests.yml` | On push / PR to main or dev | Runs full unit test suite; integration tests on PR, push to main/dev, and nightly |

> **Note on scheduling:** GitHub-hosted scheduled workflows are best-effort — runs are routinely delayed 20–40 min and frequently dropped entirely under load, so the pipeline can go several hours between successful runs even when nothing is broken. The cron minute is offset to `:17` to dodge top-of-hour congestion, and the freshness thresholds in `monitor.py` / `dashboard.py` are deliberately loose (6 h current, 12 h forecast) so this normal scheduler behaviour does not raise false alarms. For strict cadence, an external trigger (self-hosted runner or a cron service hitting `workflow_dispatch`) would be required.

### GitHub Actions secrets required

**Pipeline** (all workflow jobs): `USER`, `PASSWORD`, `HOST`, `PORT`, `DBNAME`, `STORAGE_BACKEND`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `R2_ENDPOINT_URL`, `API_KEY`

**Monitor** (monitor.yml): `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `ALERT_EMAIL_TO`

**Dashboard** (publish_dashboard.yml): `CF_PAGES_PROJECT_NAME`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`

---

## Roadmap

Future expansion:
- Schema drift detection (compare incoming API fields against a versioned expected schema)
- Airflow/Prefect orchestration (when scale or scheduling complexity demands it)
- Weather alerts ingestion (deferred until a suitable free API tier is available)
