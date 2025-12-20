"""
Current Weather Pipeline: Orchestrates hourly weather data collection.
"""
import logging
import config
from extract import extract_openweathermap
from transform import transform_current_weather
from load import load_weather

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Execute current weather pipeline."""
    try:
        # Extract
        logger.info("Starting current weather extraction...")
        raw_data = extract_openweathermap("weather")
        
        if not raw_data:
            logger.warning("No data extracted. Exiting.")
            return
        
        # Transform
        logger.info("Starting transformation...")
        records = transform_current_weather(raw_data)
        
        if not records:
            logger.warning("No records after transformation. Exiting.")
            return
        
        # Load
        logger.info("Starting load...")
        load_weather(records)
        
        logger.info("Current weather pipeline completed successfully")
        
    except Exception as e:
        logger.error(f"Current weather pipeline failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
