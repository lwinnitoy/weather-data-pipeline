import os
import importlib
from datetime import datetime, timezone, timedelta

import psycopg2
import pytest


if os.getenv("RUN_INTEGRATION_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Set RUN_INTEGRATION_TESTS=1 to run integration tests",
        allow_module_level=True,
    )


def _connect(db_config):
    return psycopg2.connect(**db_config)


def _truncate_weather_forecast(db_config):
    with _connect(db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE weather_forecast RESTART IDENTITY;")


def _fetch_one(db_config, sql, params):
    with _connect(db_config) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()


@pytest.fixture()
def integration_context(tmp_path, monkeypatch):
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        # Tests can still run if python-dotenv is missing or .env is absent.
        pass

    defaults = {
        "DB_USER": "weather_user",
        "DB_PASSWORD": "weather_pass",
        "DB_HOST": "localhost",
        "DB_PORT": "5432",
        "DB_NAME": "weather_pipeline",
        "PGSSLMODE": "disable",
    }
    for key, value in defaults.items():
        if os.getenv(key) is None:
            monkeypatch.setenv(key, value)

    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("DATA_LAKE_ROOT", str(tmp_path / "data_lake"))

    import config
    import storage
    import staging_transform
    import load
    import utils

    importlib.reload(config)
    importlib.reload(storage)
    importlib.reload(staging_transform)
    importlib.reload(load)
    importlib.reload(utils)

    return {
        "config": config,
        "storage": storage,
        "staging_transform": staging_transform,
        "load": load,
        "utils": utils,
    }


def test_forecast_pipeline_idempotent(integration_context):
    config = integration_context["config"]
    storage = integration_context["storage"]
    staging_transform = integration_context["staging_transform"]
    load = integration_context["load"]
    utils = integration_context["utils"]

    _truncate_weather_forecast(config.DATABASE)

    city_id = utils._get_city_mapping().get("Toronto")
    assert city_id is not None

    fetched_at = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    dt1 = int((fetched_at + timedelta(hours=3)).timestamp())
    dt2 = int((fetched_at + timedelta(hours=6)).timestamp())

    raw = {
        "list": [
            {
                "dt": dt1,
                "main": {"temp": 6.0, "feels_like": 5.0, "pressure": 1008, "humidity": 70},
                "weather": [{"main": "Clouds", "description": "scattered clouds"}],
                "wind": {"speed": 4.1, "deg": 200},
                "clouds": {"all": 40},
            },
            {
                "dt": dt2,
                "main": {"temp": 7.0, "feels_like": 6.0, "pressure": 1006, "humidity": 65},
                "weather": [{"main": "Clouds", "description": "broken clouds"}],
                "wind": {"speed": 3.7, "deg": 210},
                "clouds": {"all": 55},
            },
        ]
    }

    storage.write_raw("Toronto", fetched_at, raw, "forecast")

    processed = staging_transform.process_city_forecast("Toronto")
    assert processed == 1

    inserted = load.load_forecast()
    assert inserted == 2

    count, min_horizon, max_horizon = _fetch_one(
        config.DATABASE,
        "SELECT COUNT(*), MIN(forecast_horizon), MAX(forecast_horizon) "
        "FROM weather_forecast WHERE city_id = %s",
        (city_id,),
    )
    assert count == 2
    assert min_horizon == pytest.approx(3.0)
    assert max_horizon == pytest.approx(6.0)

    load.load_forecast()
    count_after = _fetch_one(
        config.DATABASE,
        "SELECT COUNT(*) FROM weather_forecast WHERE city_id = %s",
        (city_id,),
    )[0]
    assert count_after == 2
