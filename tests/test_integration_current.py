import os
import importlib
from datetime import datetime, timezone

import psycopg2
import pytest


if os.getenv("RUN_INTEGRATION_TESTS", "").lower() not in ("1", "true", "yes"):
    pytest.skip(
        "Set RUN_INTEGRATION_TESTS=1 to run integration tests",
        allow_module_level=True,
    )


def _connect(db_config):
    return psycopg2.connect(**db_config)


def _truncate_weather_history(db_config):
    with _connect(db_config) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE weather_history RESTART IDENTITY;")


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


def test_current_pipeline_idempotent(integration_context):
    config = integration_context["config"]
    storage = integration_context["storage"]
    staging_transform = integration_context["staging_transform"]
    load = integration_context["load"]
    utils = integration_context["utils"]

    _truncate_weather_history(config.DATABASE)

    city_id = utils._get_city_mapping().get("Toronto")
    assert city_id is not None

    timestamp = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
    raw = {
        "main": {"temp": 5.0, "feels_like": 4.0, "pressure": 1015, "humidity": 80},
        "weather": [{"main": "Clear", "description": "clear sky"}],
        "wind": {"speed": 3.2, "deg": 180},
        "clouds": {"all": 10},
    }

    storage.write_raw("Toronto", timestamp, raw, "current")

    processed = staging_transform.process_city_current("Toronto")
    assert processed == 1

    load.load_weather()

    count, temp_c = _fetch_one(
        config.DATABASE,
        "SELECT COUNT(*), MAX(temp_c) FROM weather_history WHERE city_id = %s",
        (city_id,),
    )
    assert count == 1
    assert temp_c == pytest.approx(5.0)

    load.load_weather()
    count_after = _fetch_one(
        config.DATABASE,
        "SELECT COUNT(*) FROM weather_history WHERE city_id = %s",
        (city_id,),
    )[0]
    assert count_after == 1
