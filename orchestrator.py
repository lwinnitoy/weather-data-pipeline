"""
Orchestrator for weather data pipeline
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Optional, Tuple
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


@dataclass
class RunMetrics:
    run_id: str
    data_type: str
    start_ts: datetime
    end_ts: Optional[datetime] = None
    duration_s: Optional[float] = None
    cities_total: int = 0
    cities_succeeded: int = 0
    cities_failed: int = 0
    records_staged: int = 0
    rows_loaded: int = 0
    errors_count: int = 0
    status: str = "success"


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


def _init_run_metrics(run_id: str, data_type: str) -> RunMetrics:
    return RunMetrics(run_id=run_id, data_type=data_type, start_ts=datetime.now(timezone.utc))


def _finalize_run_metrics(metrics: RunMetrics) -> None:
    metrics.end_ts = datetime.now(timezone.utc)
    metrics.duration_s = (metrics.end_ts - metrics.start_ts).total_seconds()


def _log_run_summary(metrics: RunMetrics) -> None:
    logger.info(
        "RUN_SUMMARY run_id=%s data_type=%s status=%s duration_s=%.2f cities_total=%d cities_succeeded=%d cities_failed=%d records_staged=%d rows_loaded=%d errors=%d start_ts=%s end_ts=%s",
        metrics.run_id,
        metrics.data_type,
        metrics.status,
        metrics.duration_s or 0.0,
        metrics.cities_total,
        metrics.cities_succeeded,
        metrics.cities_failed,
        metrics.records_staged,
        metrics.rows_loaded,
        metrics.errors_count,
        metrics.start_ts.isoformat(),
        metrics.end_ts.isoformat() if metrics.end_ts else "",
    )


def run_pipeline(data_types):
    run_id = _new_run_id()
    _configure_logging(run_id)
    logger.info("Starting pipeline for %s", data_types)
    
    for data_type in data_types:
        metrics = _init_run_metrics(run_id, data_type)
        logger.info("=== Processing %s ===", data_type)
        
        try:
            # Extract
            logger.info("Starting extract...")
            extract_ok, cities_total, cities_succeeded, cities_failed = _run_extract(data_type)
            metrics.cities_total = cities_total
            metrics.cities_succeeded = cities_succeeded
            metrics.cities_failed = cities_failed
            if not extract_ok:
                metrics.errors_count += 1
                metrics.status = "failed"
                logger.error("Extract failed for %s", data_type)
                continue
            logger.info("Extracting complete!")
            
            # Stage
            logger.info("Starting staging...")
            metrics.records_staged = _run_staging(data_type)
            logger.info("Staged %s records", metrics.records_staged)
            
            #Load
            logger.info("Starting load...")
            metrics.rows_loaded = _run_load(data_type)
            logger.info("Loading complete!")
            logger.info("Pipeline complete")
        except (RuntimeError, ValueError, StorageError, OSError) as e:
            metrics.errors_count += 1
            metrics.status = "partial" if (metrics.records_staged or metrics.rows_loaded) else "failed"
            logger.error("Pipeline failed for %s: %s", data_type, e)
        except Exception:
            metrics.errors_count += 1
            metrics.status = "partial" if (metrics.records_staged or metrics.rows_loaded) else "failed"
            logger.exception("Unexpected pipeline failure for %s", data_type)
        finally:
            if metrics.status == "success" and metrics.cities_failed:
                metrics.status = "partial"
            _finalize_run_metrics(metrics)
            _log_run_summary(metrics)

    logger.info("Pipeline terminated")
    return run_id
    
    


def _run_extract(data_type: str) -> Tuple[bool, int, int, int]:
    logger.info("Extracting %s weather data", data_type)
    cities_total = len(utils._get_city_mapping())
    raw = extract_openweathermap(data_type)
    cities_succeeded = len(raw)
    cities_failed = max(cities_total - cities_succeeded, 0)
    success = raw != {}
    return success, cities_total, cities_succeeded, cities_failed


def _run_staging(data_type: str):
    cities = utils._get_city_mapping()
    total = 0
    for city in cities:
        if data_type == "current":
            total += staging_transform.process_city_current(city)
        else:
            total += staging_transform.process_city_forecast(city)
    return total


def _run_load(data_type: str) -> int:
    if data_type == "current":
        return load.load_weather()
    return load.load_forecast()


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