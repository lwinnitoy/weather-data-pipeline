"""
Load module: Inserts weather data into PostgreSQL database.
"""
import psycopg2
from typing import List, Dict, Optional
import logging
import config
import utils
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
import storage
logger = logging.getLogger(__name__)


def load_weather() -> None:
    """
    Load current weather records from Staging layer into PostgreSQL.
    
    Reads clean, transformed data from Staging Parquet files and inserts
    into the weather_history table. City names are mapped to city_ids.
    
    Args:
        records: List of dicts from staging layer with keys:
            city, timestamp, temp_c, feels_like_c, humidity_pct,
            pressure_hpa, wind_speed_ms, wind_deg, weather_main,
            weather_description, clouds_pct, feels_like_delta
        
    Raises:
        psycopg2.Error: If database operation fails
    """
    records = _extract_staging(data_type="current")

    if not records:
        logger.info("No records to load")
        return

    
    insert_sql = """
        INSERT INTO weather_history (
            city_id, timestamp_utc, temp_c, feels_like_c, 
            pressure_hpa, humidity_pct, wind_speed_ms, 
            weather_description, raw_json
        )
        VALUES (
            %(city_id)s, %(timestamp_utc)s, %(temp_c)s, %(feels_like_c)s,
            %(pressure_hpa)s, %(humidity_pct)s, %(wind_speed_ms)s,
            %(weather_description)s, %(raw_json)s::jsonb
        )
        ON CONFLICT (city_id, timestamp_utc) DO NOTHING
    """

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.executemany(insert_sql, records)
        
        logger.info(f"Successfully loaded {len(records)} records")
        
    except psycopg2.Error as e:
        logger.error(f"Failed to load data: {e}")
        logger.error(f"pgcode: {e.pgcode}")
        if hasattr(e, 'diag'):
            logger.error(f"detail: {e.diag.message_primary}")
        raise

def load_forecast() -> None:
    """
    Load forecast records from Staging layer into PostgreSQL.
    
    Reads clean, transformed forecast data from Staging Parquet files
    and inserts into the weather_forecast table. City names are mapped
    to city_ids.
    
    Args:
        records: List of dicts from staging layer with keys:
            city_id, fetched_at, temp_c, feels_like_c, humidity_pct,
            pressure_hpa, wind_speed_ms, wind_deg, weather_main,
            weather_description, clouds_pct, feels_like_delta,
            forecast_for, horizon_hours
        
    Raises:
        psycopg2.Error: If database operation fails
    """
    records = _extract_staging(data_type="forecast")

    if not records:
        logger.info("No records to load")
        return

    insert_sql = """
        INSERT INTO weather_forecast (
            city_id, timestamp_utc, temp_c, feels_like_c, 
            pressure_hpa, humidity_pct, wind_speed_ms, 
            weather_description, raw_json, forecast_timestamp, forecast_horizon
        )
        VALUES (
            %(city_id)s, %(timestamp_utc)s, %(temp_c)s, %(feels_like_c)s,
            %(pressure_hpa)s, %(humidity_pct)s, %(wind_speed_ms)s,
            %(weather_description)s, %(raw_json)s::jsonb, %(forecast_timestamp)s, %(forecast_horizon)s
        )
        ON CONFLICT (city_id, forecast_timestamp, timestamp_utc) DO NOTHING
    """

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.executemany(insert_sql, records)
        
        logger.info(f"Successfully loaded {len(records)} records")
    except psycopg2.Error as e:
        logger.error(f"Failed to load data: {e}")
        logger.error(f"pgcode: {e.pgcode}")
        if hasattr(e, 'diag'):
            logger.error(f"detail: {e.diag.message_primary}")
        raise

def _get_last_loaded_timestamp(city_id: int, data_type: str) -> Optional[datetime]:
    """
    Query DB for most recent loaded record for a city.
    
    Returns:
        datetime if records exist, None if no records yet (first run)
        
    Raises:
        RuntimeError: If DB query fails (prevents silent full reload)
    """
    table = "weather_history" if data_type == "current" else "weather_forecast"
    
    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT MAX(timestamp_utc) FROM %s WHERE city_id = %%s" % table,
                    (city_id,)
                )
                result = cursor.fetchone()[0]
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
        logger.info(f"First run for city_id={city_id}, no previous data")
        return
    
    gap = datetime.now(timezone.utc) - last_loaded
    gap_hours = gap.total_seconds() / 3600
    
    # Warn if gap is larger than expected (48 hours for hourly pipeline)
    if gap_hours > 48:
        logger.warning(
            f"Large gap detected for city_id={city_id}: {gap_hours:.1f} hours since last load. "
            f"Consider investigating missing data."
        )
    
    # Error if timestamp is in the future (data corruption)
    if gap.total_seconds() < 0:
        raise ValueError(f"Last loaded timestamp is in the future for city_id={city_id}: {last_loaded}")
    

def _extract_staging(data_type) -> List[Dict]:
    """extracts records from staging layer"""
    city_map = utils._get_city_mapping()
    now = datetime.now(timezone.utc)
    records = []

    for city in city_map.keys():
        city_id = city_map[city]
        
        try:
            timestamp = _get_last_loaded_timestamp(city_id, data_type)
            _validate_timestamp_gap(timestamp, city_id)
        except RuntimeError as e:
            logger.error(f"Last loaded timestamp error: {e}")
            continue
        
        if timestamp is None:
            raw_files = storage.list_raw_files_after(city, None, data_type)
            if not raw_files:
                logger.warning(f"No raw files found for {city}, skipping")
                continue
            timestamp = datetime.fromtimestamp(int(raw_files[0].stem), tz=timezone.utc)

        # Iterate over partitions after last load
        while timestamp < now:
            df = storage.read_staging(city, timestamp.year, timestamp.month, data_type)
            if df is not None:
                df["city_id"] = city_id
                records.extend(df.to_dict(orient="records"))
            
            # Increment months
            timestamp += relativedelta(months=1)
            
    return records



