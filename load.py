"""
Load module: Inserts weather data into PostgreSQL database.
"""
import psycopg2
from typing import List, Dict
import logging
import config

logger = logging.getLogger(__name__)


def load(records: List[Dict]) -> None:
    """
    Load weather records into PostgreSQL database.
    
    Args:
        records: List of dictionaries matching weather_history schema
        
    Raises:
        psycopg2.Error: If database operation fails
    """
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