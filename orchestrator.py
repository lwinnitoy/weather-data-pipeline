"""
Orchestrator for weather data pipeline
"""
from extract import extract_openweathermap
import staging_transform
import load
import logging
import utils

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def run_pipeline(data_types):
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
        except Exception as e:
            logger.error(f"Pipline failed for {data_type}: {e}")
    
    logger.info("Pipeline complete")


def _run_extract(data_type: str):
    logger.info(f"Extracting {data_type} weather data")
    endpoint = "weather" if data_type == "current" else "forecast"
    raw = extract_openweathermap(endpoint)
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