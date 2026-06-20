"""
Load module: Inserts weather data into PostgreSQL database.
"""
import psycopg2
import psycopg2.extras
from typing import List, Dict, Optional
import logging
import time
import config
import utils
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from storage import list_raw_files_after, read_staging

logger = logging.getLogger(__name__)


def load_weather() -> int:
    """
    Load current weather records from Staging layer into PostgreSQL.

    Reads clean, transformed data from Staging Parquet files and inserts
    into the weather_history table. City names are mapped to city_ids.

    Raises:
        psycopg2.Error: If database operation fails
    """
    insert_sql = """
        INSERT INTO weather_history (
            city_id, timestamp_utc, temp_c, feels_like_c,
            pressure_hpa, humidity_pct, wind_speed_ms,
            weather_description, raw_json
        )
        VALUES %s
        ON CONFLICT (city_id, timestamp_utc) DO NOTHING
    """

    total = 0
    t0_total = time.perf_counter()

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            records_by_city = _extract_staging(conn, data_type="current")

            if not records_by_city:
                logger.info("No records to load")
                return 0

            with conn.cursor() as cursor:
                for city, records in records_by_city.items():
                    t0 = time.perf_counter()
                    values = [
                        (r["city_id"], r["timestamp_utc"], r["temp_c"], r["feels_like_c"],
                         r["pressure_hpa"], r["humidity_pct"], r["wind_speed_ms"],
                         r["weather_description"], None)
                        for r in records
                    ]
                    psycopg2.extras.execute_values(cursor, insert_sql, values)
                    elapsed = time.perf_counter() - t0
                    logger.info("Loaded batch: city=%s rows=%d duration=%.2fs", city, len(records), elapsed)
                    total += len(records)

    except psycopg2.Error as e:
        logger.error(f"Failed to load data: {e}")
        logger.error(f"pgcode: {e.pgcode}")
        if hasattr(e, 'diag'):
            logger.error(f"detail: {e.diag.message_primary}")
        raise psycopg2.DatabaseError("Failed to load current weather records") from e

    elapsed_total = time.perf_counter() - t0_total
    logger.info("Successfully loaded %d total records in %.2fs", total, elapsed_total)
    return total


def load_forecast() -> int:
    """
    Load forecast records from Staging layer into PostgreSQL.

    Reads clean, transformed forecast data from Staging Parquet files
    and inserts into the weather_forecast table. City names are mapped
    to city_ids.

    Raises:
        psycopg2.Error: If database operation fails
    """
    insert_sql = """
        INSERT INTO weather_forecast (
            city_id, timestamp_utc, temp_c, feels_like_c,
            pressure_hpa, humidity_pct, wind_speed_ms,
            weather_description, raw_json, forecast_timestamp, forecast_horizon
        )
        VALUES %s
        ON CONFLICT (city_id, forecast_timestamp, timestamp_utc) DO NOTHING
    """

    total = 0
    t0_total = time.perf_counter()

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            records_by_city = _extract_staging(conn, data_type="forecast")

            if not records_by_city:
                logger.info("No records to load")
                return 0

            with conn.cursor() as cursor:
                for city, records in records_by_city.items():
                    t0 = time.perf_counter()
                    values = [
                        (r["city_id"], r["timestamp_utc"], r["temp_c"], r["feels_like_c"],
                         r["pressure_hpa"], r["humidity_pct"], r["wind_speed_ms"],
                         r["weather_description"], None, r["forecast_timestamp"], r["forecast_horizon"])
                        for r in records
                    ]
                    psycopg2.extras.execute_values(cursor, insert_sql, values)
                    elapsed = time.perf_counter() - t0
                    logger.info("Loaded batch: city=%s rows=%d duration=%.2fs", city, len(records), elapsed)
                    total += len(records)

    except psycopg2.Error as e:
        logger.error(f"Failed to load data: {e}")
        logger.error(f"pgcode: {e.pgcode}")
        if hasattr(e, 'diag'):
            logger.error(f"detail: {e.diag.message_primary}")
        raise psycopg2.DatabaseError("Failed to load forecast weather records") from e

    elapsed_total = time.perf_counter() - t0_total
    logger.info("Successfully loaded %d total records in %.2fs", total, elapsed_total)
    return total


def _get_last_loaded_timestamp(cursor, city_id: int, data_type: str) -> Optional[datetime]:
    """
    Query DB for most recent loaded record for a city.

    Returns:
        datetime if records exist, None if no records yet (first run)

    Raises:
        RuntimeError: If DB query fails (prevents silent full reload)
    """
    table = "weather_history" if data_type == "current" else "weather_forecast"

    try:
        cursor.execute(
            "SELECT MAX(timestamp_utc) FROM %s WHERE city_id = %%s" % table,
            (city_id,)
        )
        result = cursor.fetchone()[0]
        if result is not None and result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result

    except psycopg2.Error as e:
        logger.error(f"DB error getting last loaded timestamp: {e}")
        raise RuntimeError(f"Failed to query last loaded timestamp for city_id={city_id}") from e


def _validate_timestamp_gap(last_loaded: Optional[datetime], city_id: int) -> None:
    """
    Warn if timestamp gap is suspiciously large.

    This catches cases where:
    - Pipeline hasn't run in a long time
    - Timestamp is somehow in the future
    - Data might be missing
    """
    if last_loaded is None:
        return

    gap = datetime.now(timezone.utc) - last_loaded
    gap_hours = gap.total_seconds() / 3600

    if gap_hours > 48:
        logger.warning(
            f"Large gap detected for city_id={city_id}: {gap_hours:.1f} hours since last load. "
            f"Consider investigating missing data."
        )

    if gap.total_seconds() < 0:
        raise ValueError(f"Last loaded timestamp is in the future for city_id={city_id}: {last_loaded}")


def _extract_staging(conn, data_type) -> Dict[str, List[Dict]]:
    """Extracts records from staging layer grouped by city, reusing the provided connection."""
    city_map = utils._get_city_mapping()
    now = datetime.now(timezone.utc)
    end_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    records_by_city = {}

    def _month_start(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)

    with conn.cursor() as db_cursor:
        for city in city_map.keys():
            city_id = city_map[city]

            try:
                timestamp = _get_last_loaded_timestamp(db_cursor, city_id, data_type)
                _validate_timestamp_gap(timestamp, city_id)
            except RuntimeError as e:
                logger.error(f"Last loaded timestamp error: {e}")
                continue

            if timestamp is None:
                logger.info("last_loaded: city=%s city_id=%d status=first_run", city, city_id)
                raw_files = list_raw_files_after(city, None, data_type)
                if not raw_files:
                    logger.warning(f"No raw files found for {city}, skipping")
                    continue
                timestamp = datetime.fromtimestamp(int(raw_files[0].stem), tz=timezone.utc)
            else:
                logger.info("last_loaded: city=%s city_id=%d timestamp=%s", city, city_id, timestamp.isoformat())

            city_records = []
            month = _month_start(timestamp)
            while month <= end_month:
                df = read_staging(city, month.year, month.month, data_type)
                if df is not None:
                    df["city_id"] = city_id
                    if "timestamp" in df.columns:
                        df = df.rename(columns={"timestamp": "timestamp_utc"})
                    if "fetched_at" in df.columns:
                        df = df.rename(columns={"fetched_at": "timestamp_utc"})
                    if "forecast_for" in df.columns:
                        df = df.rename(columns={"forecast_for": "forecast_timestamp", "horizon_hours": "forecast_horizon"})
                    city_records.extend(df.to_dict(orient="records"))

                month += relativedelta(months=1)

            if city_records:
                records_by_city[city] = city_records

    return records_by_city
