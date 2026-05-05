"""
Backfill Script: Migrate raw_json from PostgreSQL to Raw Data Lake Layer.

This is a one-time migration script to transition from the old architecture
(raw JSON stored in DB) to the new lakehouse architecture (raw JSON in files).

Usage:
    python scripts/backfill_raw_layer.py --dry-run    # Preview what would be written
    python scripts/backfill_raw_layer.py              # Execute backfill

Order of operations:
    1. Stop old pipeline (cron/scheduler)
    2. Run this script
    3. Run staging_transform on backfilled data
    4. Deploy updated load.py
    5. Start new pipeline
"""
import argparse
import logging
import sys
from pathlib import Path

import psycopg2

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from storage import write_raw

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_city_id_to_name_mapping() -> dict[int, str]:
    """
    Get reverse mapping of city_id to city_name.
    
    Returns:
        Dict mapping city_id -> city_name
    """
    with psycopg2.connect(**config.DATABASE) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, city_ascii FROM cities")
            return {city_id: name for city_id, name in cursor.fetchall()}


def fetch_raw_json_records(data_type: str, batch_size: int = 1000):
    """
    Generator that yields raw_json records from DB in batches.
    
    Args:
        data_type: "current" or "forecast"
        batch_size: Number of records per batch
        
    Yields:
        Tuples of (city_id, timestamp, raw_json)
    """
    table = "weather_history" if data_type == "current" else "weather_forecast"
    
    # Get total count for progress logging
    with psycopg2.connect(**config.DATABASE) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE raw_json IS NOT NULL")
            total = cursor.fetchone()[0]
            logger.info(f"Found {total} {data_type} records with raw_json")
    
    # Fetch in batches using server-side cursor
    with psycopg2.connect(**config.DATABASE) as conn:
        with conn.cursor(name='backfill_cursor') as cursor:
            cursor.itersize = batch_size
            
            if data_type == "current":
                cursor.execute("""
                    SELECT city_id, timestamp_utc, raw_json 
                    FROM weather_history 
                    WHERE raw_json IS NOT NULL
                    ORDER BY timestamp_utc
                """)
            else:  # forecast
                cursor.execute("""
                    SELECT city_id, timestamp_utc, raw_json 
                    FROM weather_forecast 
                    WHERE raw_json IS NOT NULL
                    ORDER BY timestamp_utc
                """)
            
            for row in cursor:
                yield row


def backfill_data_type(data_type: str, city_map: dict[int, str], dry_run: bool) -> dict:
    """
    Backfill all records of a given data type.
    
    Args:
        data_type: "current" or "forecast"
        city_map: Mapping of city_id -> city_name
        dry_run: If True, don't write files
        
    Returns:
        Stats dict with counts
    """
    stats = {
        "processed": 0,
        "written": 0,
        "skipped_no_city": 0,
        "errors": 0
    }
    
    for city_id, timestamp_utc, raw_json in fetch_raw_json_records(data_type):
        stats["processed"] += 1
        
        # Get city name from ID
        city_name = city_map.get(city_id)
        if not city_name:
            logger.warning(f"No city name for city_id={city_id}, skipping")
            stats["skipped_no_city"] += 1
            continue
        
        # Convert timestamp to unix
        unix_ts = timestamp_utc.timestamp()
        
        if dry_run:
            if stats["processed"] <= 5:  # Show first few
                logger.info(f"[DRY RUN] Would write: {city_name}/{data_type}/{unix_ts}.json")
            stats["written"] += 1
        else:
            try:
                write_raw(city_name, unix_ts, raw_json, data_type)
                stats["written"] += 1
            except Exception as e:
                logger.error(f"Failed to write {city_name}/{unix_ts}: {e}")
                stats["errors"] += 1
        
        # Progress logging
        if stats["processed"] % 1000 == 0:
            logger.info(f"Progress: {stats['processed']} records processed")
    
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill raw JSON from DB to data lake")
    parser.add_argument("--dry-run", action="store_true", 
                        help="Preview what would be written without writing files")
    parser.add_argument("--data-type", choices=["current", "forecast", "both"], 
                        default="both", help="Which data type to backfill")
    args = parser.parse_args()
    
    if args.dry_run:
        logger.info("=== DRY RUN MODE - No files will be written ===")
    
    # Get city mapping
    logger.info("Fetching city ID to name mapping...")
    city_map = get_city_id_to_name_mapping()
    logger.info(f"Found {len(city_map)} cities")
    
    # Determine which data types to process
    data_types = ["current", "forecast"] if args.data_type == "both" else [args.data_type]
    
    # Process each data type
    all_stats = {}
    for data_type in data_types:
        logger.info(f"\n{'='*50}")
        logger.info(f"Backfilling {data_type} weather data...")
        logger.info(f"{'='*50}")
        
        stats = backfill_data_type(data_type, city_map, args.dry_run)
        all_stats[data_type] = stats
        
        logger.info(f"\n{data_type.upper()} Summary:")
        logger.info(f"  Processed: {stats['processed']}")
        logger.info(f"  Written:   {stats['written']}")
        logger.info(f"  Skipped:   {stats['skipped_no_city']}")
        logger.info(f"  Errors:    {stats['errors']}")
    
    # Final summary
    logger.info(f"\n{'='*50}")
    logger.info("BACKFILL COMPLETE")
    logger.info(f"{'='*50}")
    
    if args.dry_run:
        logger.info("\nThis was a dry run. Run without --dry-run to execute.")
    else:
        logger.info("\nNext steps:")
        logger.info("1. Run staging_transform to process backfilled data")
        logger.info("2. Deploy updated load.py (NULL for raw_json)")
        logger.info("3. Start new pipeline")


if __name__ == "__main__":
    main()
