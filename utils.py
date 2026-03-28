import psycopg2
import config
import logging
from typing import Dict
logger = logging.getLogger(__name__)

def _get_city_mapping() -> Dict[str, int]:
    """
    Get mapping of city names to database IDs.
    
    Returns:
        Dictionary: {city_name: city_id}
    """
    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, city_ascii FROM cities")
                return {name: city_id for city_id, name in cursor.fetchall()}
                
    except psycopg2.Error as e:
        logger.error(f"Database error fetching city mapping: {e}")
        raise psycopg2.DatabaseError("Failed to fetch city mapping from database") from e