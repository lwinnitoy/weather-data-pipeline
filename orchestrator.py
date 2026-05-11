"""
Orchestrator for weather data pipeline
"""
from datetime import datetime, timezone
import logging
import uuid

import config
from extract import extract_openweathermap
import staging_transform
import load
import utils
from storage import StorageError

logger = logging.getLogger(__name__)


class _RunIdFilter(logging.Filter):
    """Inject a run_id into log records for per-run tracing."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = self._run_id
        return True


def _get_log_level() -> int:
    level_name = str(config.LOG_LEVEL).upper()
    return getattr(logging, level_name, logging.INFO)


def _configure_logging(run_id: str) -> None:
    level = _get_log_level()
    log_format = "%(asctime)s - %(name)s - %(levelname)s - run_id=%(run_id)s - %(message)s"
    logging.basicConfig(level=level, format=log_format)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(log_format)
    run_id_filter = _RunIdFilter(run_id)
    for handler in root.handlers:
        handler.setFormatter(formatter)
        handler.setLevel(level)
        for existing in list(handler.filters):
            if isinstance(existing, _RunIdFilter):
                handler.removeFilter(existing)
        handler.addFilter(run_id_filter)


def _new_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"{ts}-{suffix}"


def run_pipeline(data_types):
    run_id = _new_run_id()
    _configure_logging(run_id)
    logger.info(f"Starting pipeline for {data_types}")
    
    for data_type in data_types:
        logger.info(f"=== Processing {data_type} ===")
        
        try:
            # Extract
            logger.info("Starting extract...")
            success = _run_extract(data_type)
            if not success:
                logger.error(f"Extract failed for {data_type}")
                continue
            logger.info("Extracting complete!")
            
            # Stage
            logger.info("Starting staging...")
            staged = _run_staging(data_type)
            logger.info(f"Staged {staged} records")
            
            #Load
            logger.info("Starting load...")
            _run_load(data_type)
            logger.info("Loading complete!")
            logger.info("Pipeline complete")
        except (RuntimeError, ValueError, StorageError, OSError) as e:
            logger.error(f"Pipeline failed for {data_type}: {e}")
        except Exception:
            logger.exception(f"Unexpected pipeline failure for {data_type}")

        
    
    logger.info("Pipeline terminated")
    return run_id
    
    


def _run_extract(data_type: str):
    logger.info(f"Extracting {data_type} weather data")
    raw = extract_openweathermap(data_type)
    return False if raw == {} else True


def _run_staging(data_type: str):
    cities = utils._get_city_mapping()
    total = 0
    for city in cities:
        if data_type == "current":
            total += staging_transform.process_city_current(city)
        else:
            total += staging_transform.process_city_forecast(city)
    return total


def _run_load(data_type: str):
    if data_type == "current":
        load.load_weather()
    else:
        load.load_forecast()
    return


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Run weather data pipeline")
    parser.add_argument(
        "--data-types", 
        nargs="+", 
        default=["current", "forecast"],
        choices=["current", "forecast"],
        help="Data types to process (default: both)"
    )
    args = parser.parse_args()
    run_pipeline(args.data_types)