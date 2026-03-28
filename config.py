"""Configuration management for weather data pipeline."""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
# Comment out when using GitHub Secrets
load_dotenv()

# Database configuration
DATABASE = {
    'user': os.getenv("DB_USER"),
    'password': os.getenv("DB_PASSWORD"),
    'host': os.getenv("DB_HOST"),
    'port': os.getenv("DB_PORT"),
    'dbname': os.getenv("DB_NAME"),
    'sslmode': os.getenv("PGSSLMODE", "require")
}

# OpenWeatherMap API configuration
OPENWEATHERMAP_API_KEY = os.getenv("API_KEY")
API_BASE_URL = "http://api.openweathermap.org/data/2.5"
API_TIMEOUT = 10  # seconds

# Data Lake configuration
# Uses local filesystem for now; will migrate to S3/MinIO later
DATA_LAKE_ROOT = Path(os.getenv("DATA_LAKE_ROOT", "data_lake"))
RAW_LAYER_DIR = "raw"
STAGING_LAYER_DIR = "staging"
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local")  # "local" or "s3"

# Application settings
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Cloudflare s3 configuration
R2_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
R2_SECRET_KEY = os.getenv("S3_SECRET_KEY")
R2_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
R2_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
