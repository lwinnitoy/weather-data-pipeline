"""
Forecast Pipeline: Orchestrates 5-day weather forecast collection.
"""
import logging
import config
from extract import extract_openweathermap
from transform import transform_forecast
from load import load_forecast

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Execute forecast pipeline."""
    try:
        # Extract
        logger.info("Starting forecast extraction...")
        raw_data = extract_openweathermap("forecast")
        
        if not raw_data:
            logger.warning("No forecast data extracted. Exiting.")
            return
        
        # Transform
        logger.info("Starting transformation...")
        records = transform_forecast(raw_data)
        
        if not records:
            logger.warning("No forecast records after transformation. Exiting.")
            return
        
        # Load
        logger.info("Starting load...")
        load_forecast(records)
        
        logger.info("Forecast pipeline completed successfully")
        
    except Exception as e:
        logger.error(f"Forecast pipeline failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
