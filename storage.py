"""
Storage module: Abstracts data lake operations for raw and staging layers.

This module implements the Repository Pattern - it hides storage implementation
details (local filesystem vs S3/MinIO) behind a clean interface. Other modules
call these functions without knowing where data is actually stored.

Layer Structure:
    Raw Layer (Bronze):     Immutable JSON files, exactly as received from API
    Staging Layer (Silver): Cleaned Parquet files, partitioned for analytics

Path Conventions:
    Raw:     {lake_root}/raw/{city}/{YYYY}/{MM}/{DD}/{timestamp}.json
    Staging: {lake_root}/staging/city={city}/year={YYYY}/month={MM}/data.parquet
"""
import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)


# =============================================================================
# PATH BUILDING FUNCTIONS
# These construct paths without touching the filesystem
# =============================================================================

def get_raw_path(city: str, timestamp: datetime) -> Path:
    """
    Build the path for a raw JSON file.
    
    Args:
        city: City name (will be normalized to lowercase, spaces → underscores)
        timestamp: When the data was fetched (used for partitioning)
    
    Returns:
        Path object like: data_lake/raw/toronto/2025/12/20/1734567890.json
    
    Example:
        >>> get_raw_path("Toronto", datetime(2025, 12, 20, 10, 30, 0))
        Path('data_lake/raw/toronto/2025/12/20/1734693000.json')
    """
    normalized_city = _normalize_city_name(city)
    unix_ts = int(timestamp.timestamp())

    raw_path = (
        config.DATA_LAKE_ROOT 
        / config.RAW_LAYER_DIR
        / normalized_city
        / f"{timestamp.year}"
        / f"{timestamp.month:02d}" 
        / f"{timestamp.day:02d}" 
        / f"{unix_ts}.json"
    )
    return raw_path


def get_staging_path(city: str, year: int, month: int) -> Path:
    """
    Build the path for a staging Parquet file.
    
    Uses Hive-style partitioning (key=value) which tools like Spark understand.
    
    Args:
        city: City name (normalized)
        year: 4-digit year
        month: 1-12
    
    Returns:
        Path object like: data_lake/staging/city=toronto/year=2025/month=12/data.parquet
    """
    normalized_city = _normalize_city_name(city)
    staging_path = (
        config.DATA_LAKE_ROOT
        / config.STAGING_LAYER_DIR
        / f"city={normalized_city}"
        / f"year={year}"
        / f"month={month:02d}"
        / "data.parquet"
    )
    return staging_path


def _normalize_city_name(city: str) -> str:
    """
    Normalize city name for use in file paths.
    
    Converts to lowercase and replaces spaces with underscores.
    This ensures consistent paths regardless of how city names are formatted.
    
    Args:
        city: Raw city name (e.g., "New York", "TORONTO")
    
    Returns:
        Normalized name (e.g., "new_york", "toronto")
    """
    return city.lower().replace(" ", "_")


# =============================================================================
# RAW LAYER OPERATIONS
# Write-once, read-many pattern for preserving API responses
# =============================================================================

def write_raw(city: str, timestamp: datetime, data: dict) -> Path:
    """
    Write raw API response to the data lake.
    
    Creates parent directories if they don't exist.
    File is written atomically (write to temp, then rename).
    
    Args:
        city: City name
        timestamp: When data was fetched
        data: Raw API response dictionary
    
    Returns:
        Path where file was written
    
    Raises:
        IOError: If write fails
    """
    path = get_raw_path(city, timestamp)
    path.parent.mkdir(parents=True, exist_ok=True)  # Create parent directories
    
    # Temp file as sibling with .tmp suffix
    tmp_path = path.with_suffix('.json.tmp')
    
    try:
        # Write to temp file
        with tmp_path.open("w") as f:
            json.dump(data, f)  # json.dump writes directly to file
            f.flush()
        
        # Atomic rename: temp → final
        tmp_path.rename(path)
        logger.debug(f"Wrote raw file: {path}")
        
    except Exception as e:
        # Cleanup temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        logger.error(f"Failed to write raw file: {e}")
        raise  # Re-raise so caller knows it failed
    
    return path


def read_raw_files(city: str, start_date: date, end_date: date) -> List[Dict]:
    """
    Read all raw JSON files for a city within a date range.
    
    Scans partition folders to find matching files.
    
    Args:
        city: City name
        start_date: Inclusive start date
        end_date: Inclusive end date
    
    Returns:
        List of dictionaries, each containing:
        - 'timestamp': When data was fetched
        - 'data': The raw API response
        - 'path': Where file was read from
    
    Example:
        >>> files = read_raw_files("toronto", date(2025, 12, 1), date(2025, 12, 31))
        >>> files[0]
        {'timestamp': datetime(...), 'data': {...}, 'path': Path(...)}
    """
    files_list = []

    normalized_city = _normalize_city_name(city)
    base = (
        config.DATA_LAKE_ROOT
        / config.RAW_LAYER_DIR
        / normalized_city
    )

    current_date = start_date
    while current_date <= end_date:
        cur_path = (
            base 
            / f"{current_date.year}" 
            / f"{current_date.month:02d}" 
            / f"{current_date.day:02d}" 
        )

        # Move increment before any continue statements
        current_date += timedelta(days=1)

        if not cur_path.exists():
            continue

        for path in cur_path.glob("*.json"):
            try:
                with path.open("r") as f:
                    data = json.load(f)  # Parse JSON into dict
                
                # Extract timestamp from filename
                unix_ts = int(path.stem)
                timestamp = datetime.fromtimestamp(unix_ts)
                
                files_list.append({
                    'timestamp': timestamp,
                    'data': data,
                    'path': path
                })
            except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to read {path}: {e}")
                continue

    return files_list


def list_raw_files_after(city: str, after_timestamp: Optional[datetime] = None) -> List[Path]:
    """
    List raw files for a city, optionally filtered by timestamp.
    
    Used for incremental processing - find files newer than last processed.
    
    Args:
        city: City name
        after_timestamp: Only return files with timestamp > this value
                        If None, returns all files for the city
    
    Returns:
        List of Paths to raw JSON files, sorted by timestamp ascending
    """
    paths = []
    normalized_city = _normalize_city_name(city)
    base = (
        config.DATA_LAKE_ROOT
        / config.RAW_LAYER_DIR
        / normalized_city
    )

    #check if city has not data yet
    if not base.exists():
        return []

    #no tiestamp filter
    if after_timestamp is None:
        for p in base.rglob("*.json"):
            paths.append(p)
    else:
        #filtered output
        # Compares every file with after_timestamp in unix. 
        # This would extremely inefficient but since scale is small it's applicable.
        after_unix = int(after_timestamp.timestamp())

        for p in base.rglob("*.json"):
            if after_unix < int(p.stem):
                paths.append(p)

    paths.sort(key=lambda p: p.name)
    return paths


# =============================================================================
# STAGING LAYER OPERATIONS
# Parquet files optimized for analytics
# =============================================================================

def write_staging(city: str, year: int, month: int, df: pd.DataFrame) -> Path:
    """
    Write a DataFrame to the staging layer as Parquet.
    
    Overwrites existing file for the partition (full partition overwrite pattern).
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
        df: DataFrame with transformed weather data
    
    Returns:
        Path where Parquet file was written
    """
    path = get_staging_path(city, year, month)
    path.parent.mkdir(parents=True, exist_ok=True)  # Create parent directories
    
    # Temp file as sibling with .tmp suffix
    tmp_path = path.with_suffix('.parquet.tmp')
    
    try:
        # Pandas handles Parquet writing
        df.to_parquet(tmp_path, index=False)

        # Atomic rename
        tmp_path.rename(path)
        logger.debug(f"Wrote staging file: {path}")
        
    except Exception as e:
        # Cleanup temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        logger.error(f"Failed to write staging file: {e}")
        raise  # Re-raise so caller knows it failed
    
    return path
    



def read_staging(city: str, year: int, month: int) -> Optional[pd.DataFrame]:
    """
    Read staging Parquet file for a partition.
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
    
    Returns:
        DataFrame if file exists, None otherwise
    """
    path = get_staging_path(city, year, month)
    
    if not path.exists():
        return None
    
    try:
        df = pd.read_parquet(path)
        logger.debug(f"Read staging file: {path}")
        return df
    except Exception as e:
        logger.error(f"Failed to read staging file {path}: {e}")
        return None


# =============================================================================
# HIGH-WATER MARK TRACKING
# Enables incremental processing by tracking last processed timestamp
# =============================================================================

def get_high_water_mark(city: str, year: int, month: int) -> Optional[datetime]:
    """
    Get the timestamp of the last processed raw file for a partition.
    
    The high-water mark is stored in a .last_processed file alongside the Parquet.
    
    Args:
        city: City name
        year: 4-digit year  
        month: 1-12
    
    Returns:
        Datetime of last processed file, or None if never processed
    """
    base = get_staging_path(city, year, month)
    path = base.parent / ".last_processed.json"

    if not path.exists():
        return None
    
    try:
        with path.open("r") as f:
            raw = json.load(f)
            ts = raw.get("timestamp", None)

            if ts is None:
                return None
            
            return datetime.fromtimestamp(ts)
        
    except Exception as e:
        logger.error(f"Failed to get high-water mark at {base}: {e}")
        return None
    
    

def set_high_water_mark(city: str, year: int, month: int, timestamp: datetime) -> None:
    """
    Update the high-water mark after processing.
    
    Called after successfully writing staging Parquet.
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
        timestamp: Timestamp of the newest raw file that was processed
    """
    path = get_staging_path(city, year, month).parent / ".last_processed.json"
    path.parent.mkdir(parents=True, exist_ok=True) #ensure the parent exits

    try:
        with path.open("w") as file:
            json.dump({"timestamp": timestamp.timestamp()}, file)
    
    except Exception as e:
        logger.error(f"Failed to set high-water mark at {path}: {e}")

    return None


# if __name__ == "__main__":
#     print(_normalize_city_name("New York"))