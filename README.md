# Real-Time Weather Data Pipeline

A beginner-friendly data engineering project that collects real-time weather data using the OpenWeatherMap API, processes it with Python, and stores it in a cloud-hosted PostgreSQL database. This project demonstrates a basic ETL (Extract, Transform, Load) pipeline and includes optional data analysis tools for querying and visualizing the stored data.

---

## Features

- Collects real-time weather data from OpenWeatherMap API
- Extracts weather metrics: temperature, pressure, humidity, wind speed, and weather description
- Transforms data using pandas with timezone-aware timestamps (UTC and Pacific Time)
- Loads data into PostgreSQL database using efficient COPY command
- Stores data for Victoria, BC, Canada
- Includes error handling for API failures and database connection issues
- Supports environment variable configuration via .env file or GitHub Secrets

---

## Tech Stack

- **Python** (3.8+)
- **OpenWeatherMap API** – Data source
- **PostgreSQL** – Cloud-hosted via [Supabase](https://supabase.com)
- **pandas** – For data processing
- **psycopg2-binary** – For DB connection
- **python-dotenv** – For environment variable management
- **GitHub Actions** – For automation (optional)

---

## Installation

1. Clone this repo:
   ```bash
   git clone https://github.com/lwinnitoy/weather-data-pipeline.git
   cd weather-data-pipeline

2. Create and activate a virtual environment:
   python -m venv venv
   source venv/bin/activate  (On Windows: venv\Scripts\activate)

3. Install dependencies:
    pip install -r requirements.txt

4. Set up environment variables in a .env file:
    API_KEY=your_openweathermap_api_key
    DB_USER=your_postgres_username
    DB_PASSWORD=your_postgres_password
    DB_HOST=your_postgres_host
    DB_PORT=your_postgres_port
    DB_NAME=your_database_name
    PGSSLMODE=require

---

## Usage

Run the ETL script manually:
```bash
python etl_pipeline.py
```

The script will:
- Extract weather data from OpenWeatherMap API for Victoria, CA
- Transform the data (temperature, pressure, humidity, wind speed, weather description)
- Load the data into a PostgreSQL database table named `weather_history`

Note: The dashboard is not yet implemented.



