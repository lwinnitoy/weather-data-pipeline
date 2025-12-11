"""
Main ETL Pipeline: Orchestrates extraction, transformation, and loading of weather data.
"""
import logging
import config
from extract import extract_openweathermap
from transform import transform
from load import load

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main() -> None:
    """Execute the complete ETL pipeline for weather data."""
    try:
        # Extract
        logger.info("Starting extraction...")
        raw_data = extract_openweathermap()
        
        if not raw_data:
            logger.warning("No data extracted. Exiting.")
            return
        
        # Transform
        logger.info("Starting transformation...")
        records = transform(raw_data)
        
        if not records:
            logger.warning("No records after transformation. Exiting.")
            return
        
        # Load
        logger.info("Starting load...")
        load(records)
        
        logger.info("ETL pipeline completed successfully")
        
    except Exception as e:
        logger.error(f"ETL pipeline failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()



