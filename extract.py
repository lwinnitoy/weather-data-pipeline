"""
Extract module: Fetches weather data from OpenWeatherMap API.
"""
import requests
import psycopg2
from typing import Dict, List, Tuple
import logging
import config
import utils
from storage import write_raw, TransientError as StorageTransientError
import datetime as datetime
from retry import RetryError, run_with_retry, is_retryable_http_status

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_db_connection():
    """Create and return a database connection."""
    return psycopg2.connect(**config.DATABASE)

@run_with_retry
def _get_response(lat, lon, endpoint):
    """Fetch weather data from OpenWeatherMap API.
    
    Args:
        lat: Latitude
        lon: Longitude
        endpoint: API endpoint ("weather" or "forecast")
    
    Returns:
        requests.Response object
    
    Raises:
        RetryError: On transient failures (timeout, connection errors, retryable HTTP status)
    """
    try:
        url = (
            f"{config.API_BASE_URL}/{endpoint}?"
            f"lat={lat}&lon={lon}&appid={config.OPENWEATHERMAP_API_KEY}&units=metric"
        )
        response = requests.get(url, timeout=config.API_TIMEOUT)
        
        # Classify HTTP status codes: retry on 408, 429, 5xx
        if is_retryable_http_status(response.status_code):
            raise RetryError(
                f"Retryable HTTP {response.status_code}: {response.text[:100]}"
            )
        
        return response
        
    except (TimeoutError, ConnectionError, ConnectionAbortedError) as e:
        # Transient network errors: retry
        raise RetryError(f"Network error: {e}") from e
    except requests.Timeout as e:
        # requests library timeout: retry
        raise RetryError(f"Request timeout: {e}") from e
    except requests.ConnectionError as e:
        # requests library connection error: retry
        raise RetryError(f"Connection error: {e}") from e


def extract_openweathermap(endpoint: str) -> Dict[int, dict]:
    """
    Extract current weather data or forecast or alerts from OpenWeatherMap API for all configured cities.
    
    Returns:
        Dictionary mapping city_id to weather data: {city_id: api_response}
        Returns empty dict if all cities fail.
    """
    cities = _get_cities_to_fetch()
    city_map = utils._get_city_mapping()
    city_data = {}

    if endpoint not in ["weather", "forecast"]: 
        endpoint = "weather"

    for city_name, lat, lon in cities:
        try:
            response = _get_response(lat, lon, endpoint)
            
            if response.status_code != 200:
                logger.warning(f"API error for {city_name}: {response.status_code}")
                continue  # Skip this city, continue with others
            
            city_id = city_map.get(city_name)
            if city_id:
                city_data[city_id] = response.json()

                # Write raw data to data lake with retry on transient storage errors
                try:
                    if endpoint == "weather":
                        data_type = "current"
                    else:
                        data_type = "forecast"
                    write_raw(city_name, datetime.datetime.now(), response.json(), data_type)
                    logger.info(f"Successfully wrote response for {city_name} to raw layer")
                except StorageTransientError as e:
                    logger.error(f"Transient storage error writing {city_name}: {e}. Skipping.")
                    continue
            else:
                logger.warning(f"City {city_name} not found in mapping")
                
        except RetryError as e:
            logger.error(f"Failed to fetch {city_name} after retries: {e}")
            continue  # Skip this city, continue with others
        except requests.RequestException as e:
            logger.error(f"Non-retryable network error fetching {city_name}: {e}")
            continue  # Continue with other cities
        except Exception as e:
            logger.error(f"Unexpected error fetching {city_name}: {e}", exc_info=True)
            continue

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
        raise psycopg2.DatabaseError("Failed to fetch cities from database") from e