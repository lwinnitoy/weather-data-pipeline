"""
Extract module: Fetches weather data from OpenWeatherMap API.
"""
import requests
import psycopg2
from typing import Dict, List, Tuple
import logging
import config
from storage import write_raw
import datetime as datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_db_connection():
    """Create and return a database connection."""
    return psycopg2.connect(**config.DATABASE)

def extract_openweathermap(endpoint: str) -> Dict[int, dict]:
    """
    Extract current weather data or forecast or alerts from OpenWeatherMap API for all configured cities.
    
    Returns:
        Dictionary mapping city_id to weather data: {city_id: api_response}
        Returns empty dict if all cities fail.
    """
    cities = _get_cities_to_fetch()
    city_map = _get_city_mapping()
    city_data = {}

    if endpoint not in ["weather", "forecast"]: 
        endpoint = "weather"

    for city_name, lat, lon in cities:
        try:
            url = (f"{config.API_BASE_URL}/{endpoint}?"
                   f"lat={lat}&lon={lon}&appid={config.OPENWEATHERMAP_API_KEY}&units=metric")
            
            response = requests.get(url, timeout=config.API_TIMEOUT)
            
            if response.status_code != 200:
                logger.warning(f"API error for {city_name}: {response.status_code}")
                continue  # Skip this city, continue with others
            
            city_id = city_map.get(city_name)
            if city_id:
                city_data[city_id] = response.json()

                #write raw data to data lake
                if endpoint == "weather":
                    data_type = "current"
                else:
                    data_type = "forecast"
                write_raw(city_name, datetime.datetime.now().timestamp(), response.json(), data_type)
                logger.info(f"Successfully wrote response for {city_name} to raw layer")
            else:
                logger.warning(f"City {city_name} not found in mapping")
                
        except requests.RequestException as e:
            logger.error(f"Network error fetching {city_name}: {e}")
            continue  # Continue with other cities
    
    logger.info(f"Successfully fetched weather for {len(city_data)} cities")

    return city_data


def _get_cities_to_fetch() -> List[Tuple[str, float, float]]:
    """
    Fetch list of cities from database to collect weather data for.
    
    Returns:
        List of tuples: [(city_name, latitude, longitude), ...]
    """
    try:
        with _get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT city_ascii, lat, lng FROM cities")
                cities = cursor.fetchall()
        
        logger.info(f"Fetched {len(cities)} cities from database")
        return cities
        
    except psycopg2.Error as e:
        logger.error(f"Database error fetching cities: {e}")
        logger.error(f"pgcode: {e.pgcode}")
        raise


def _get_city_mapping() -> Dict[str, int]:
    """
    Get mapping of city names to database IDs.
    
    Returns:
        Dictionary: {city_name: city_id}
    """
    try:
        with _get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT id, city_ascii FROM cities")
                return {name: city_id for city_id, name in cursor.fetchall()}
                
    except psycopg2.Error as e:
        logger.error(f"Database error fetching city mapping: {e}")
        raise