# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all unit tests
python -m pytest -q

# Run a single test file
python -m pytest tests/test_load.py -v

# Run a single test by name
python -m pytest tests/test_monitor.py::TestCheckDataFreshness::test_normalizes_naive_datetime -v

# Run integration tests (requires Docker Postgres — see docker-compose.yml)
docker compose up -d
RUN_INTEGRATION_TESTS=1 pytest tests/test_integration_current.py tests/test_integration_forecast.py -v

# Run the full pipeline
python orchestrator.py --data-types current forecast

# Run one data type
python orchestrator.py --data-types forecast

# Run monitoring checks
python monitor.py --data-types current forecast

# Generate static dashboard HTML
python dashboard.py --output docs/index.html
```

## Architecture

The pipeline is a classic lakehouse ETL with three layers:

```
OpenWeatherMap API
    → extract.py       (raw JSON per city → data_lake/raw/{type}/{city}/{Y}/{M}/{D}/{ts}.json)
    → staging_transform.py  (raw JSON → data_lake/staging/{type}/city={city}/year={Y}/month={MM}/data.parquet)
    → load.py          (staging Parquet → PostgreSQL: weather_history / weather_forecast)
```

`orchestrator.py` drives the sequence and emits a structured `RUN_SUMMARY` log line per data type. Each stage is fault-isolated per city — one city failing does not abort others.

### Storage abstraction (`storage.py`)

All file I/O goes through `storage.py`, which switches between local filesystem and Cloudflare R2 based on `STORAGE_BACKEND` env var (`local` | `r2`). Callers never touch boto3 or file paths directly. The exception hierarchy is `StorageError > TransientError / NotFoundError` — only `TransientError` triggers retry.

### Retry (`retry.py`)

The `@run_with_retry` decorator (Tenacity-based) retries on `RetryError` with exponential backoff (4 attempts, 0.5 s base, 2× multiplier, 2 s cap). Applied to `_get_response()` in extract and all R2 operations in storage. Retryable HTTP statuses: 408, 429, 5xx. Transient S3 codes are listed in `TRANSIENT_S3_ERROR_CODES`.

### Incremental processing

`staging_transform.py` tracks a high-water mark (`.last_processed.json` alongside each Parquet partition) per city per month. Only raw files newer than the mark are re-processed. `load.py` similarly queries `MAX(timestamp_utc)` per city before reading staging to avoid reloading already-loaded months.

### Idempotency

Enforced at two levels:
- Staging: `merge_with_existing()` deduplicates on `(city, timestamp)` for current, `(city, fetched_at, forecast_for)` for forecast before writing Parquet.
- DB: `ON CONFLICT DO NOTHING` backed by `UNIQUE` constraints on `(city_id, timestamp_utc)` and `(city_id, forecast_timestamp, timestamp_utc)`.

### Validation (`validators/engine.py`)

`staging_transform.py` runs `run_validations()` after transforming each city's data. Rules are declared in `Documentation/validation_rules.json` (required columns, min row count, null rate thresholds, uniqueness keys). `VALIDATION_FAIL_ON_ERROR=true` makes failures raise; default is log-and-continue.

### Database connections

All `psycopg2.connect()` calls use `**config.DATABASE`, which includes `connect_timeout=30s`, `statement_timeout=300s`, and `lock_timeout=30s`. These are overridable via `DB_CONNECT_TIMEOUT`, `DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS` env vars.

### Monitoring (`monitor.py`)

Runs independently of the pipeline (hourly via `monitor.yml`, 15 min after each pipeline). Checks freshness (>120 min stale = warning) and anomaly detection (0 rows or >10× 7-day rolling baseline = warning). Sends a Gmail summary when warnings are found — silently skips if `GMAIL_USER` / `GMAIL_APP_PASSWORD` are not set.

## Key Conventions

- **City names vs IDs**: `cities` table maps `city_ascii` → `id`. `utils._get_city_mapping()` returns `{city_name: city_id}`. Build the inverse (`{id: name}`) when going the other direction (as in monitor/dashboard). Mixing these up causes silent KeyErrors.
- **`data_type` values**: always the string `"current"` or `"forecast"` — anything else raises `InvalidDataTypeError` in storage.
- **Datetime timezone safety**: psycopg2 can return naive datetimes from Postgres. Always normalize with `.replace(tzinfo=timezone.utc)` if `dt.tzinfo is None` before comparing to timezone-aware datetimes.
- **Test mocking pattern**: mock `psycopg2.connect` at the module level (`@patch('load.psycopg2.connect')`), not the global one. Use the `mock_db_connection` fixture from `tests/test_load.py` as the standard pattern.

## Scope Boundaries

- **Weather alerts are explicitly out of scope** — no suitable free API tier exists. Do not add an alerts path.
- **Airflow/Prefect is a future concern** — orchestration is GitHub Actions only.
- **`clients.py`** owns the boto3 S3 client singleton. Tests monkeypatch `clients.s3` rather than patching boto3 directly.
