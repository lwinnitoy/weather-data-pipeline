"""
Staging Transform Module: Bridges Raw (Bronze) and Staging (Silver) layers.

Reads raw JSON files, transforms to clean schema, writes Parquet.
Handles both current weather and forecast data with separate schemas.

Current Weather Staging Schema:
    city, timestamp, temp_c, feels_like_c, humidity_pct, pressure_hpa,
    wind_speed_ms, wind_deg, weather_main, weather_description, 
    clouds_pct, feels_like_delta

Forecast Staging Schema:
    (same as current) + forecast_for, horizon_hours
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import pandas as pd

from storage import (
    list_raw_files_after,
    read_staging,
    write_staging,
    get_high_water_mark,
    set_high_water_mark,
    _read_raw_json_r2
)
import config
from validators import engine as validation_engine



logger = logging.getLogger(__name__)


# =============================================================================
# PUBLIC API - Entry points for orchestration
# =============================================================================

def process_city_current(city: str) -> int:
    """
    Process all current weather raw files for a city.
    
    Groups files by partition (year/month) and processes each.
    
    Args:
        city: City name
    
    Returns:
        Total number of records processed
    """
    #collect all files for city
    files = list_raw_files_after(city=city, after_timestamp=None, data_type="current")
    
    if not files:
        return 0

    partition_dict = defaultdict(list)

    #group by partition
    for path in files:
        key = extract_partition_key(datetime.fromtimestamp(int(path.stem), tz=timezone.utc))
        partition_dict[key].append(path)

    #process each partition
    tot_recs = 0
    for partition in partition_dict:
        tot_recs += process_partition_current(city, partition[0], partition[1])

    return tot_recs


def process_partition_current(city: str, year: int, month: int) -> int:
    """
    Process current weather data for a single partition.
    
    Reads new raw files, transforms, merges with existing, writes staging.
    
    Args:
        city: City name
        year: Partition year
        month: Partition month
    
    Returns:
        Number of new records processed
    """
    data_type = "current"
    hwm = get_high_water_mark(city, year, month, data_type)

    #list will be sorted by timstamp asc
    files = list_raw_files_after(city, after_timestamp=hwm, data_type=data_type)

    # Filter to only files in this partition
    filtered_list = [
    path for path in files if (ts := datetime.fromtimestamp(int(path.stem), tz=timezone.utc)).year == year and ts.month == month
    ]
    
    if not filtered_list:
        return 0
    
    #collect files in partition
    records = []
    for path in filtered_list:
        raw = read_raw_json(path)
        if not raw:
            continue
        ts = datetime.fromtimestamp(int(path.stem), tz=timezone.utc)
        record = transform_current_to_record(city=city, raw_data=raw, timestamp=ts)
        if record:
            records.append(record)
    
    #write parquet updated file to staging layer
    new_df = pd.DataFrame(records)
    old_df = read_staging(city, year, month, data_type)
    merged = merge_with_existing(old_df, new_df, data_type)

    # Run validation gates scoped to this project
    try:
        report = validation_engine.run_validations(merged, data_type=data_type, city=city, year=year, month=month)
        if report.failures:
            for f in report.failures:
                logger.warning("Validation %s: %s - %s", f.check, f.severity, f.message)
            if config.VALIDATION_FAIL_ON_ERROR and any(f.severity == "error" for f in report.failures):
                raise RuntimeError(f"Validation failed for {city} {year}-{month} {data_type}")
    except Exception:
        logger.exception("Validation runner raised an exception")
        if config.VALIDATION_FAIL_ON_ERROR:
            raise
    write_staging(city, year, month, merged, data_type)

    #update high water-mark
    newest_ts = datetime.fromtimestamp(int(filtered_list[-1].stem), tz=timezone.utc)
    set_high_water_mark(city, year, month, newest_ts, data_type)
    return len(filtered_list)


def process_city_forecast(city: str) -> int:
    """
    Process all forecast raw files for a city.
    
    Args:
        city: City name
    
    Returns:
        Total number of records processed
    """
    data_type = "forecast"
    #collect all files for city
    files = list_raw_files_after(city=city, after_timestamp=None, data_type=data_type)
    
    if not files:
        return 0

    partition_dict = defaultdict(list)

    #group by partition
    for path in files:
        key = extract_partition_key(datetime.fromtimestamp(int(path.stem), tz=timezone.utc))
        partition_dict[key].append(path)

    #process each partition
    tot_recs = 0
    for partition in partition_dict:
        tot_recs += process_partition_forecast(city, partition[0], partition[1])

    return tot_recs


def process_partition_forecast(city: str, year: int, month: int) -> int:
    """
    Process forecast data for a single partition.
    
    Args:
        city: City name  
        year: Partition year
        month: Partition month
    
    Returns:
        Number of new records processed
    """
    data_type = "forecast"
    hwm = get_high_water_mark(city, year, month, data_type)

    #list will be sorted by timstamp asc
    files = list_raw_files_after(city, after_timestamp=hwm, data_type=data_type)

    # Filter to only files in this partition
    filtered_list = [
    path for path in files if (ts := datetime.fromtimestamp(int(path.stem), tz=timezone.utc)).year == year and ts.month == month
    ]
    
    if not filtered_list:
        return 0
    
    #collect files in partition
    records = []
    for path in filtered_list:
        raw = read_raw_json(path)
        if not raw:
            continue
        ts = datetime.fromtimestamp(int(path.stem), tz=timezone.utc)
        record = transform_forecast_to_records(city=city, raw_data=raw, fetched_at=ts)
        if record:
            records.extend(record)
    
    #write parquet updated file to staging layer
    new_df = pd.DataFrame(records)
    old_df = read_staging(city, year, month, data_type)
    merged = merge_with_existing(old_df, new_df, data_type)

    # Run validation gates scoped to this project
    try:
        report = validation_engine.run_validations(merged, data_type=data_type, city=city, year=year, month=month)
        if report.failures:
            for f in report.failures:
                logger.warning("Validation %s: %s - %s", f.check, f.severity, f.message)
            if config.VALIDATION_FAIL_ON_ERROR and any(f.severity == "error" for f in report.failures):
                raise RuntimeError(f"Validation failed for {city} {year}-{month} {data_type}")
    except Exception:
        logger.exception("Validation runner raised an exception")
        if config.VALIDATION_FAIL_ON_ERROR:
            raise
    write_staging(city, year, month, merged, data_type)

    #update high water-mark
    newest_ts = datetime.fromtimestamp(int(filtered_list[-1].stem), tz=timezone.utc)
    set_high_water_mark(city, year, month, newest_ts, data_type)
    return len(filtered_list)


# =============================================================================
# TRANSFORM FUNCTIONS - Convert raw JSON to staging schema
# =============================================================================

def transform_current_to_record(raw_data: dict, city: str, timestamp: datetime) -> Optional[Dict]:
    """
    Transform a single current weather API response to staging schema.
    
    Args:
        raw_data: Raw OpenWeatherMap current weather response
        city: City name (not in API response, must be provided)
        timestamp: When the data was fetched
    
    Returns:
        Dict matching staging schema, or None if invalid data
    
    Example raw_data structure:
        {
            "main": {"temp": 5.2, "feels_like": 2.1, "pressure": 1015, "humidity": 80},
            "weather": [{"main": "Clear", "description": "clear sky"}],
            "wind": {"speed": 4.5, "deg": 180},
            "clouds": {"all": 0}
        }
    """
    try:
        main = raw_data["main"]
        wind = raw_data["wind"]
        weather = raw_data["weather"][0]
        
        record = {
            "city": city,
            "timestamp": timestamp,
            "temp_c": main["temp"],
            "feels_like_c": main["feels_like"],
            "humidity_pct": main["humidity"],
            "pressure_hpa": main["pressure"],
            "wind_speed_ms": wind["speed"],
            "wind_deg": wind["deg"],
            "weather_main": weather["main"],
            "weather_description": weather["description"],
            "clouds_pct": raw_data["clouds"]["all"],
            "feels_like_delta": main["feels_like"] - main["temp"]
        }

        return record
    
    except (KeyError, TypeError, IndexError):
        return None


def transform_forecast_to_records(raw_data: dict, city: str, fetched_at: datetime) -> List[Dict]:
    """
    Transform a forecast API response to staging schema.
    
    Note: Returns a LIST because one forecast API call contains ~40 forecasts.
    
    Args:
        raw_data: Raw OpenWeatherMap forecast response
        city: City name
        fetched_at: When the forecast was fetched
    
    Returns:
        List of dicts matching forecast staging schema
    
    Example raw_data structure:
        {
            "list": [
                {
                    "dt": 1734789600,
                    "main": {"temp": 6.5, ...},
                    "weather": [...],
                    "wind": {...},
                    "dt_txt": "2025-12-21 12:00:00"
                },
                ...
            ]
        }
    """
    records = []
    try:
        for data in raw_data["list"]:
            main = data["main"]
            wind = data["wind"]
            weather = data["weather"][0]

            horizon = datetime.fromtimestamp(data["dt"], tz=timezone.utc) - fetched_at
            horizon_hours = int(horizon.total_seconds() // 3600)
            
            rec = {
                "city": city,
                "fetched_at": fetched_at,
                "temp_c": main["temp"],
                "feels_like_c": main["feels_like"],
                "humidity_pct": main["humidity"],
                "pressure_hpa": main["pressure"],
                "wind_speed_ms": wind["speed"],
                "wind_deg": wind["deg"],
                "weather_main": weather["main"],
                "weather_description": weather["description"],
                "clouds_pct": data["clouds"]["all"],
                "feels_like_delta": main["feels_like"] - main["temp"],
                "forecast_for": datetime.fromtimestamp(data["dt"], tz=timezone.utc),
                "horizon_hours": horizon_hours
            }
            records.append(rec)

        return records
    except (KeyError, TypeError, IndexError):
        return []



# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def merge_with_existing(existing_df: Optional[pd.DataFrame], new_df: pd.DataFrame, data_type: str = "current") -> pd.DataFrame:
    """
    Merge new records with existing staging data.
    
    Handles deduplication based on (city, timestamp) for current weather
    or (city, timestamp, forecast_for) for forecasts.
    
    Args:
        existing_df: Current staging data (None if first run)
        new_df: New records to add
    
    Returns:
        Merged DataFrame with duplicates removed
    """
    if data_type == "current":
        subset = ['city', 'timestamp']
    else:  # forecast
        subset = ['city', 'fetched_at', 'forecast_for']
    
    # Single dedup with correct columns
    combined = pd.concat([existing_df, new_df], ignore_index=True)
    return combined.drop_duplicates(subset=subset, keep='last')
    


def extract_partition_key(timestamp: datetime) -> Tuple[int, int]:
    """
    Extract (year, month) partition key from a timestamp.
    
    Args:
        timestamp: Datetime to extract partition from
    
    Returns:
        Tuple of (year, month)
    """
    return (timestamp.year, timestamp.month)


def read_raw_json(path: Path) -> Optional[dict]:
    """
    Read a raw JSON file from the data lake.
    
    Args:
        path: Path to JSON file
    
    Returns:
        Parsed JSON dict, or None if read fails
    """
    if config.STORAGE_BACKEND=="r2":
        return _read_raw_json_r2(path)
    try:
        with path.open("r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to read {path}: {e}")
        return None

