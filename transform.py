"""
Transform module: Converts raw API data to database schema format.
"""
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


def transform_current_weather(city_data: Dict[int, dict]) -> List[Dict]:
    """
    Transform raw weather API data into database record format.
    
    Args:
        city_data: Dictionary mapping city_id to API response data
        
    Returns:
        List of dictionaries ready for database insertion
    """
    records = []
    
    for city_id, data in city_data.items():
        try:
            record = _transform_current_weather_record(city_id, data)
            if record:
                records.append(record)
        except (KeyError, TypeError, IndexError) as e:
            logger.warning(f"Failed to transform data for city_id {city_id}: {e}")
            continue  # Skip this record, continue with others
    
    logger.info(f"Transformed {len(records)} weather records")
    return records

def transform_forecast(city_data: Dict[int, dict]) -> List[Dict]:
    """
    Transform raw forecast API data into database record format.
    
    Args:
        city_data: Dictionary mapping city_id to API response data
        
    Returns:
        List of dictionaries ready for database insertion
    """
    records = []

    for city_id, data in city_data.items():
        try:
            record = _transform_forecast_record(city_id, data)
            if record:
                records.append(record)
        except (KeyError, TypeError, IndexError) as e:
            logger.warning(f"Failed to transform data for city_id {city_id}: {e}")
            continue  # Skip this record, continue with others
        
    logger.info(f"Transformed {len(records)} weather records")
    return records



def _transform_current_weather_record(city_id: int, data: dict) -> Optional[Dict]:
    """
    Transform a single API response into a database record.
    
    Args:
        city_id: Database ID of the city
        data: Raw API response JSON
        
    Returns:
        Dictionary matching weather_history schema, or None if invalid
    """
    # Validate required fields
    if not all(key in data for key in ['main', 'weather', 'wind']):
        logger.warning(f"Missing required fields in data for city_id {city_id}")
        return None
    
    if not data['weather']:
        logger.warning(f"Empty weather array for city_id {city_id}")
        return None
    
    main_data = data['main']
    weather_data = data['weather'][0]
    wind_data = data['wind']
    
    return {
        'city_id': city_id,
        'timestamp_utc': datetime.now(timezone.utc),
        'temp_c': main_data['temp'],
        'feels_like_c': main_data['feels_like'],
        'pressure_hpa': main_data['pressure'],
        'humidity_pct': main_data['humidity'],
        'wind_speed_ms': wind_data['speed'],
        'weather_description': weather_data['description'],
        'raw_json': json.dumps(data)
    }

def _transform_forecast_record(city_id: int, data: dict) -> Optional[Dict]:
    """
    Transform a single API response into a database record.
    
    Args:
        city_id: Database ID of the city
        data: Raw API response JSON
        
    Returns:
        Dictionary matching weather_forecast schema, or None if invalid
    """
    # Validate required fields
    if not all(key in data for key in ['list']):
        logger.warning(f"Missing required fields in data for city_id {city_id}")
        return None
    
    forecast_data = dict(data['list'][0])

    if not all(key in forecast_data for key in ['main', 'weather', 'wind']):
        logger.warning(f"Missing required fields in data for city_id {city_id}")
        return None
    
    if not forecast_data['weather']:
        logger.warning(f"Empty weather array for city_id {city_id}")
        return None
    
    main_data = forecast_data['main']
    weather_data = forecast_data['weather'][0]
    wind_data = forecast_data['wind']
    
    # Parse forecast timestamp and make it timezone-aware (UTC)
    forecast_time = datetime.fromisoformat(forecast_data['dt_txt']).replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    hours_ahead = int((forecast_time - now_utc).total_seconds() // 3600)
    
    return {
        'city_id': city_id,
        'timestamp_utc': now_utc,
        'temp_c': main_data['temp'],
        'feels_like_c': main_data['feels_like'],
        'pressure_hpa': main_data['pressure'],
        'humidity_pct': main_data['humidity'],
        'wind_speed_ms': wind_data['speed'],
        'weather_description': weather_data['description'],
        'raw_json': json.dumps(data),
        'forecast_timestamp': forecast_time,
        'forecast_horizon': hours_ahead
    }