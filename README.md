# Real-Time Weather Data Pipeline

A portfolio-focused data engineering project that ingests weather data from OpenWeatherMap, persists raw API payloads to a data lake, transforms them into partitioned staging datasets, and loads analytics-ready tables into PostgreSQL.

Current supported data sources:
- Current weather (`/weather`)
- 5-day forecast (`/forecast`)

Weather alerts are intentionally deferred to a future expansion because there is no reliable free API tier that fits project requirements for immediate alerts.

## Current Architecture

Pipeline flow:
1. Extract API data for each configured city from PostgreSQL `cities` table
2. Write raw JSON to data lake (`data_lake/raw/...`)
3. Transform raw files into clean Parquet partitions (`data_lake/staging/...`)
4. Load from staging into PostgreSQL analytics tables with idempotent inserts

Key implementation decisions:
- Storage backend abstraction supports local filesystem and Cloudflare R2 (`STORAGE_BACKEND=local|r2`)
- Incremental processing uses high-water marks per city and partition
- Idempotency is enforced with DB uniqueness constraints plus `ON CONFLICT DO NOTHING`
- Scheduling is currently handled via GitHub Actions workflows

## Features

- Multi-city ingestion driven by `cities` table
- Current weather and forecast ETL paths
- Raw and staging lakehouse layers
- Partitioned Parquet staging outputs
- PostgreSQL load layer with duplicate protection
- Local and R2 object-storage support
- Automated CI test workflow and scheduled pipeline workflows

## Tech Stack

- Python 3.10+
- OpenWeatherMap API
- PostgreSQL (Supabase-compatible)
- pandas
- psycopg2-binary
- boto3 (R2/S3-compatible backend)
- GitHub Actions

## Project Layout

- `extract.py`: API extraction + raw writes
- `staging_transform.py`: raw-to-staging transforms + high-water marks
- `load.py`: staging-to-warehouse load
- `storage.py`: local/R2 storage repository layer
- `orchestrator.py`: CLI orchestration entry point
- `migrations/`: SQL schema and idempotency migrations

## Setup

1. Clone and enter the repo.
2. Create a virtual environment and activate it.
3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Configure environment variables in `.env`:

```bash
API_KEY=your_openweathermap_api_key

DB_USER=your_postgres_user
DB_PASSWORD=your_postgres_password
DB_HOST=your_postgres_host
DB_PORT=5432
DB_NAME=your_postgres_db
PGSSLMODE=require

STORAGE_BACKEND=local
DATA_LAKE_ROOT=data_lake

# Required only when STORAGE_BACKEND=r2
S3_ACCESS_KEY=...
S3_SECRET_KEY=...
S3_ENDPOINT_URL=...
S3_BUCKET_NAME=...
```

## Running the Pipeline

Run both supported data types:

```bash
python orchestrator.py --data-types current forecast
```

Run one data type:

```bash
python orchestrator.py --data-types current
python orchestrator.py --data-types forecast
```

Run tests:

```bash
python -m pytest -q
```

## Automation

- Hourly current-weather pipeline: `.github/workflows/current_pipeline.yml`
- Forecast pipeline every 3 hours: `.github/workflows/forecast_pipeline.yml`
- Test CI on push/PR: `.github/workflows/tests.yml`

## Roadmap

Near-term:
- Add retry/backoff around API and transient storage failures
- Add stricter data validation checks in transform layer
- Add richer run-level metrics and observability

Future expansion:
- Weather alerts ingestion (deferred until a suitable free API is available)
- Airflow/Prefect orchestration
- BI/dashboard layer
 - Persist per-run validation reports and aggregate validation metrics (store JSON reports to `data_lake/validation_reports/`, emit summary metrics for Grafana/Prometheus). This enables historical QA, trend detection, and alerting.



