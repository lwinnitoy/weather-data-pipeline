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
import boto3
from botocore.exceptions import ClientError
from io import BytesIO
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import config
import clients
from retry import is_transient_s3_error

logger = logging.getLogger(__name__)

# Storage exception hierarchy
class StorageError(Exception):
    """Base class for storage-related errors.

    Use this for unexpected operational failures (network, permissions,
    decoding/parsing errors). Callers should treat this as fatal for the
    current operation and decide whether to retry or abort.
    """


class NotFoundError(StorageError):
    """Indicates a requested object was not found in storage.

    This is used to distinguish the normal 'missing' case from other
    operational failures. Functions reading data may return ``None`` for
    missing objects or raise this exception depending on the public API
    contract — see function docstrings for details.
    """


class TransientError(StorageError):
    """Indicates a transient/retriable failure (e.g. network timeout).

    Callers should implement retry/backoff when catching this error.
    """


class InvalidDataTypeError(ValueError):
    """Raised when data_type is not one of the supported pipeline values."""


# S3/R2 client is provided by the `clients` module (clients.s3)


def _s3_body_to_bytes(body_obj) -> bytes:
    """Normalize an S3 response `Body` to raw bytes.

    Accepts common shapes returned by boto3/mocks:
    - StreamingBody or file-like objects with `.read()`
    - `bytes` or `bytearray`
    - objects convertible to `bytes`

    Raises `StorageError` for unexpected types so callers get a clear
    operational error instead of a cryptic AttributeError.
    """
    if hasattr(body_obj, "read"):
        return body_obj.read()
    if isinstance(body_obj, (bytes, bytearray)):
        return bytes(body_obj)
    try:
        return bytes(body_obj)
    except Exception as e:
        raise StorageError("Unexpected S3 body type") from e


# =============================================================================
# LOCAL BACKEND
# Uses local filesystem as datalake
# =============================================================================



# PATH BUILDING FUNCTIONS
# These construct paths without touching the filesystem
# =============================================================================

def get_raw_path(city: str, timestamp: datetime, data_type: str = "current") -> Path:
    """
    Build the path for a raw JSON file.
    
    Args:
        city: City name (will be normalized to lowercase, spaces → underscores)
        timestamp: When the data was fetched (used for partitioning)
        data_type: type of weather data i.e forecast/current
    
    Returns:
        Path object like: data_lake/raw/toronto/2025/12/20/1734567890.json
    
    Example:
        >>> get_raw_path("Toronto", datetime(2025, 12, 20, 10, 30, 0))
        Path('data_lake/raw/toronto/2025/12/20/1734693000.json')
    """
    #ensure data type is correct
    if data_type not in ["current", "forecast"]:
        raise InvalidDataTypeError(
            f"Unsupported data_type='{data_type}'. Expected 'current' or 'forecast'."
        )
    
    normalized_city = _normalize_city_name(city)
    unix_ts = int(timestamp.timestamp())

    raw_path = (
        config.DATA_LAKE_ROOT 
        / config.RAW_LAYER_DIR
        / f"{data_type}"
        / normalized_city
        / f"{timestamp.year}"
        / f"{timestamp.month:02d}" 
        / f"{timestamp.day:02d}" 
        / f"{unix_ts}.json"
    )
    return raw_path


def get_staging_path(city: str, year: int, month: int, data_type: str = "current") -> Path:
    """
    Build the path for a staging Parquet file.
    
    Uses Hive-style partitioning (key=value) which tools like Spark understand.
    
    Args:
        city: City name (normalized)
        year: 4-digit year
        month: 1-12
        data_type: type of weather data i.e forecast/current
    
    Returns:
        Path object like: data_lake/staging/city=toronto/year=2025/month=12/data.parquet
    """
    #ensure data type is correct
    if data_type not in ["current", "forecast"]:
        raise InvalidDataTypeError(
            f"Unsupported data_type='{data_type}'. Expected 'current' or 'forecast'."
        )

    normalized_city = _normalize_city_name(city)
    staging_path = (
        config.DATA_LAKE_ROOT
        / config.STAGING_LAYER_DIR
        / f"{data_type}"
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



# RAW LAYER OPERATIONS
# Write-once, read-many pattern for preserving API responses
# =============================================================================

def write_raw(city: str, timestamp: datetime, data: dict, data_type: str = "current") -> Optional[Path | str]:
    """
    Write raw API response to the data lake.
    
    Creates parent directories if they don't exist.
    File is written atomically (write to temp, then rename).
    
    Args:
        city: City name
        timestamp: When data was fetched
        data: Raw API response dictionary
        data_type: type of weather data i.e forecast/current
    
    Returns:
        Path where file was written
    
    Raises:
        IOError: If write fails
    """
    if config.STORAGE_BACKEND == "r2":
        return _write_raw_r2(city, timestamp, data, data_type)
    
    path = get_raw_path(city, timestamp, data_type)
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
        
    except (OSError, TypeError, ValueError) as e:
        # Cleanup temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        logger.error(f"Failed to write raw file {path}: {e}")
        raise StorageError(f"Failed to write raw file: {path}") from e
    
    return path


def read_raw_files(city: str, start_date: date, end_date: date, data_type: str = "current") -> List[Dict]:
    """
    Read all raw JSON files for a city within a date range.
    
    Scans partition folders to find matching files.
    
    Args:
        city: City name
        start_date: Inclusive start date
        end_date: Inclusive end date
        data_type: type of weather data i.e forecast/current
    
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
        / data_type
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


def list_raw_files_after(city: str, after_timestamp: Optional[datetime] = None, data_type: str = "current") -> List[Path]:
    """
    List raw files for a city, optionally filtered by timestamp.
    
    Used for incremental processing - find files newer than last processed.
    
    Args:
        city: City name
        after_timestamp: Only return files with timestamp > this value
                        If None, returns all files for the city
        data_type: type of weather data i.e forecast/current
    
    Returns:
        List of Paths to raw JSON files, sorted by timestamp ascending
    """
    if config.STORAGE_BACKEND == "r2":
        return _list_raw_files_after_r2(city, after_timestamp, data_type)
    
    paths = []
    normalized_city = _normalize_city_name(city)
    base = (
        config.DATA_LAKE_ROOT
        / config.RAW_LAYER_DIR
        / data_type
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
        # This would be extremely inefficient but since scale is small it's applicable.
        after_unix = int(after_timestamp.timestamp())

        for p in base.rglob("*.json"):
            if after_unix < int(p.stem):
                paths.append(p)

    paths.sort(key=lambda p: int(p.stem))
    return paths




# STAGING LAYER OPERATIONS
# Parquet files optimized for analytics
# =============================================================================

def write_staging(city: str, year: int, month: int, df: pd.DataFrame, data_type: str = "current") -> Optional[Path | str]:
    """
    Write a DataFrame to the staging layer as Parquet.
    
    Overwrites existing file for the partition (full partition overwrite pattern).
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
        df: DataFrame with transformed weather data
        data_type: type of weather data i.e forecast/current
    
    Returns:
        Path where Parquet file was written
    """
    if config.STORAGE_BACKEND == "r2":
        return _write_staging_r2(city, year, month, df, data_type)
    
    path = get_staging_path(city, year, month, data_type)
    path.parent.mkdir(parents=True, exist_ok=True)  # Create parent directories
    
    # Temp file as sibling with .tmp suffix
    tmp_path = path.with_suffix('.parquet.tmp')
    
    try:
        # Pandas handles Parquet writing
        df.to_parquet(tmp_path, index=False)

        # Atomic rename
        tmp_path.rename(path)
        logger.debug(f"Wrote staging file: {path}")
        
    except (OSError, ValueError, ImportError) as e:
        # Cleanup temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        logger.error(f"Failed to write staging file {path}: {e}")
        raise StorageError(f"Failed to write staging file: {path}") from e
    
    return path
    



def read_staging(city: str, year: int, month: int, data_type: str = "current") -> Optional[pd.DataFrame]:
    """
    Read staging Parquet file for a partition.
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
        data_type: type of weather data i.e forecast/current
    
    Returns:
        DataFrame if file exists, None otherwise
    """
    if config.STORAGE_BACKEND == "r2":
        return _read_staging_r2(city, year, month, data_type)
    
    path = get_staging_path(city, year, month, data_type)
    
    if not path.exists():
        return None
    
    try:
        df = pd.read_parquet(path)
        logger.debug(f"Read staging file: {path}")
        return df
    except FileNotFoundError:
        # File may disappear between exists() and read_parquet() under concurrent writes.
        return None
    except (OSError, ValueError, ImportError) as e:
        logger.error(f"Failed to read staging file {path}: {e}")
        return None


# HIGH-WATER MARK TRACKING
# Enables incremental processing by tracking last processed timestamp
# =============================================================================

def get_high_water_mark(city: str, year: int, month: int, data_type: str = "current") -> Optional[datetime]:
    """
    Get the timestamp of the last processed raw file for a partition.
    
    The high-water mark is stored in a .last_processed file alongside the Parquet.
    
    Args:
        city: City name
        year: 4-digit year  
        month: 1-12
        data_type: type of weather data i.e forecast/current
    
    Returns:
        Datetime of last processed file, or None if never processed
    """
    if config.STORAGE_BACKEND == "r2":
        return _get_high_water_mark_r2(city, year, month, data_type)
    
    base = get_staging_path(city, year, month, data_type)
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
        
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Failed to get high-water mark at {base}: {e}")
        return None
    
    

def set_high_water_mark(city: str, year: int, month: int, timestamp: datetime, data_type: str = "current") -> Optional[Path]:
    """
    Update the high-water mark after processing.
    
    Called after successfully writing staging Parquet.
    
    Args:
        city: City name
        year: 4-digit year
        month: 1-12
        timestamp: Timestamp of the newest raw file that was processed
        data_type: type of weather data i.e forecast/current
    """
    if config.STORAGE_BACKEND == "r2":
        return _set_high_water_mark_r2(city, year, month, timestamp, data_type)
    
    path = get_staging_path(city, year, month, data_type).parent / ".last_processed.json"
    path.parent.mkdir(parents=True, exist_ok=True) #ensure the parent exits

    try:
        with path.open("w") as file:
            json.dump({"timestamp": timestamp.timestamp()}, file)
    
    except (OSError, TypeError, ValueError) as e:
        logger.error(f"Failed to set high-water mark at {path}: {e}")
        raise StorageError("Failed to serialize high-water mark") from e

    return path


# =============================================================================
# CLOUD BACKEND (for R2/S3)
# =============================================================================

# KEY-BUILDING HELPERS
# return string keys, not Path objects
# =============================================================================

# --- Key-building helpers (return string keys, not Path objects) ---
def _get_raw_key(city: str, timestamp: datetime, data_type: str) -> Optional[str]:
    """Return S3/R2 key for raw JSON file (e.g. 'raw/current/toronto/2026/02/06/123456.json')"""

    #ensure data type is correct
    if data_type not in ["current", "forecast"]:
        raise InvalidDataTypeError(
            f"Unsupported data_type='{data_type}'. Expected 'current' or 'forecast'."
        )
    
    return f"{config.RAW_LAYER_DIR}/{data_type}/{_normalize_city_name(city)}/{timestamp.year}/{timestamp.month:02d}/{timestamp.day:02d}/{int(timestamp.timestamp())}.json"



def _get_staging_key(city: str, year: int, month: int, data_type: str) -> Optional[str]:
    """Return S3/R2 key for staging Parquet file (e.g. 'staging/current/city=toronto/year=2026/month=02/data.parquet')"""
    
    if data_type not in ["current", "forecast"]:
        raise InvalidDataTypeError(
            f"Unsupported data_type='{data_type}'. Expected 'current' or 'forecast'."
        )
    
    return f"{config.STAGING_LAYER_DIR}/{data_type}/city={_normalize_city_name(city)}/year={year}/month={month:02d}/data.parquet"



def _get_hwm_key(city: str, year: int, month: int, data_type: str) -> Optional[str]:
    """Return S3/R2 key for high-water mark file (e.g. 'staging/current/city=toronto/year=2026/month=02/.last_processed.json')"""
   
    if data_type not in ["current", "forecast"]:
        raise InvalidDataTypeError(
            f"Unsupported data_type='{data_type}'. Expected 'current' or 'forecast'."
        )
    
    return f"{config.STAGING_LAYER_DIR}/{data_type}/city={_normalize_city_name(city)}/year={year}/month={month:02d}/.last_processed.json"


# R2 BACKEND READ/WRITES
# Uses boto3
# =============================================================================

def _write_raw_r2(city: str, timestamp: datetime, data: dict, data_type: str) -> Optional[str]:
    """Write raw JSON to R2. Use _get_raw_key and boto3 put_object."""
    
    key = _get_raw_key(city, timestamp, data_type)
    try:
        clients.s3.put_object(Bucket=config.R2_BUCKET_NAME, Key=key, Body=json.dumps(data))
        logger.info(f"Wrote raw file to R2: {key}")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to upload raw file to R2 key={key}: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (TypeError, ValueError) as e:
        logger.error(f"Failed to upload file to R2: error: {e}")
        raise StorageError("Failed to serialize raw payload") from e
    return key




def _write_staging_r2(city: str, year: int, month: int, df: pd.DataFrame, data_type: str) -> Optional[str]:
    """Write Parquet to R2. Use _get_staging_key, BytesIO, and boto3 put_object."""
    
    key = _get_staging_key(city, year, month, data_type)
    try:
        # Write DataFrame to in-memory bytes buffer
        buffer = BytesIO()
        df.to_parquet(buffer, index=False)
        buffer.seek(0)  # Reset buffer position
        clients.s3.put_object(Bucket=config.R2_BUCKET_NAME, Key=key, Body=buffer.getvalue())
        logger.info(f"Wrote staging file to R2: {key}")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to upload staging file to R2 key={key}: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (ValueError, OSError, ImportError) as e:
        logger.error(f"Failed to upload file to R2: error: {e}")
        raise StorageError("Failed to encode parquet payload") from e
    return key




def _read_staging_r2(city, year, month, data_type) -> Optional[pd.DataFrame]:
    """Read Parquet from R2. Use _get_staging_key, boto3 get_object, BytesIO, pd.read_parquet."""
    
    key = _get_staging_key(city, year, month, data_type)
    try:
        response = clients.s3.get_object(Bucket=config.R2_BUCKET_NAME, Key=key)
        body = _s3_body_to_bytes(response["Body"])
        buffer = BytesIO(body)
        buffer.seek(0)
        return pd.read_parquet(buffer)
    
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code in ("NoSuchKey", "404"):
            return None
        logger.error(f"Failed to read staging file from R2 key={key}: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (ValueError, OSError, ImportError) as e:
        logger.error(f"Failed to read files from R2: error: {e}")
        raise StorageError("Failed to decode object") from e



def _list_raw_files_after_r2(city: str, after_timestamp: Optional[datetime], data_type: str) -> List[Path]:
    """List raw files in R2. Use paginator, filter by timestamp, return Path-like objects with .stem property."""
    paginator = clients.s3.get_paginator("list_objects_v2")

    objects = []
    try:
        prefix = f"{config.RAW_LAYER_DIR}/{data_type}/{_normalize_city_name(city)}/"
        for page in paginator.paginate(Bucket=config.R2_BUCKET_NAME, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key:
                    continue

                pkey = Path(key)
                try:
                    ts = int(pkey.stem)
                except ValueError:
                    logger.debug(f"Skipping non-timestamp key: {key}")
                    continue

                if after_timestamp is None or ts > int(after_timestamp.timestamp()):
                    objects.append(pkey)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to list raw files from R2: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (ValueError, OSError) as e:
        logger.error(f"Failed to list raw files from R2: error: {e}")
        raise StorageError("Failed to decode object") from e

    #safer to explicitly sort by int stem
    objects.sort(key=lambda pkey: int(pkey.stem))
    return objects



def _get_high_water_mark_r2(city, year, month, data_type) -> Optional[datetime]:
    """Get HWM from R2. Use _get_hwm_key, boto3 get_object, json.loads."""
    hwm_key = _get_hwm_key(city, year, month, data_type)

    try:
        response = clients.s3.get_object(Bucket=config.R2_BUCKET_NAME, Key=hwm_key)
        body = _s3_body_to_bytes(response["Body"])
        data = json.loads(body)
        ts = data.get("timestamp")
        if ts is None:
            return None
        return datetime.fromtimestamp(ts)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code in ("NoSuchKey", "404"):
            return None
        logger.error(f"Failed to read HWM from R2: {hwm_key}: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"Failed to read file: {hwm_key}, error: {e}")
        raise StorageError("Failed to decode object") from e

    

def _set_high_water_mark_r2(city: str, year: int, month: int, timestamp: datetime, data_type: str) -> Optional[Path]:
    """Set HWM in R2. Use _get_hwm_key, json.dumps, boto3 put_object."""
    hwm_key = _get_hwm_key(city, year, month, data_type)

    try:
        clients.s3.put_object(Bucket=config.R2_BUCKET_NAME, Key=hwm_key, Body=json.dumps({"timestamp": timestamp.timestamp()}))
        logger.info(f"Wrote HWM to R2: {hwm_key}")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        logger.error(f"Failed to upload HWM to R2: {error_code}: {e}")
        if is_transient_s3_error(error_code):
            raise TransientError(f"Transient S3 error: {error_code}") from e
        else:
            raise StorageError(f"Non-transient S3 error: {error_code}") from e
    except (TypeError, ValueError) as e:
        logger.error(f"Failed to upload file to R2: error: {e}")
        raise StorageError("Failed to serialize high-water mark") from e
    
    if not hwm_key:
        return None
    else:
        return Path(hwm_key)
    

