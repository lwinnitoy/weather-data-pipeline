"""
Extract module: Fetches weather data from OpenWeatherMap API.
"""
import requests
import psycopg2
from typing import Dict, List, Tuple
import logging
import time
import config
import utils
from storage import write_raw, TransientError as StorageTransientError, StorageError
import datetime as datetime
from retry import RetryError, run_with_retry, is_retryable_http_status

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


def extract_openweathermap(data_type: str) -> Dict[int, dict]:
    """
    Extract current weather data or forecast or alerts from OpenWeatherMap API for all configured cities.
    
    Returns:
        Dictionary mapping city_id to weather data: {city_id: api_response}
        Returns empty dict if all cities fail.
    """
    cities = _get_cities_to_fetch()
    city_map = utils._get_city_mapping()
    city_data = {}

    if data_type not in ["current", "forecast"]:
        raise ValueError(f"Invalid data_type: {data_type}. Must be 'current' or 'forecast'.")
    
    endpoint = "weather" if data_type == "current" else "forecast"
    
    api_latencies: List[Tuple[str, float]] = []  # (city_name, latency_ms)
    total_payload_bytes = 0

    for city_name, lat, lon in cities:
        try:
            t0 = time.perf_counter()
            response = _get_response(lat, lon, endpoint)
            latency_ms = (time.perf_counter() - t0) * 1000
            payload_bytes = len(response.content)

            if response.status_code != 200:
                logger.warning(f"API error for {city_name}: {response.status_code} ({latency_ms:.0f} ms)")
                continue  # Skip this city, continue with others

            api_latencies.append((city_name, latency_ms))
            total_payload_bytes += payload_bytes
            logger.info(f"API {city_name}: {latency_ms:.0f} ms, {payload_bytes} bytes")

            city_id = city_map.get(city_name)
            if city_id:
                city_data[city_id] = response.json()

                # Write raw data to data lake with retry on transient storage errors
                try:
                    raw_key = write_raw(city_name, datetime.datetime.now(), response.json(), data_type)
                    if raw_key is None:
                        logger.error(f"Failed to write raw data for {city_name}")
                        continue
                    logger.info(
                        f"Successfully wrote response for {city_name} to raw layer: {raw_key}"
                    )
                except StorageTransientError as e:
                    #retry logic for local transient storage errors (e.g. file system issues)
                    #retry logic for r2 is handled within storage module, so we just log and skip on failure here
                    logger.error(f"Transient storage error writing {city_name}: {e}. Skipping.")
                    continue
                except StorageError as e:
                    logger.error(f"Storage error writing {city_name}: {e}. Skipping.")
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

    if api_latencies:
        avg_ms = sum(ms for _, ms in api_latencies) / len(api_latencies)
        slowest_city, slowest_ms = max(api_latencies, key=lambda x: x[1])
        logger.info(
            f"API_LATENCY_SUMMARY data_type={data_type} cities_ok={len(city_data)} cities_attempted={len(cities)} "
            f"avg_ms={avg_ms:.0f} slowest_city={slowest_city} slowest_ms={slowest_ms:.0f} "
            f"total_payload_kb={total_payload_bytes // 1024}"
        )

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