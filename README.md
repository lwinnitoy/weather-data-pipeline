# Real-Time Weather Data Pipeline

A beginner-friendly data engineering project that collects real-time weather data using the OpenWeatherMap API, processes it with Python, and stores it in a cloud-hosted PostgreSQL database. This project demonstrates a basic ETL (Extract, Transform, Load) pipeline and includes optional data analysis tools for querying and visualizing the stored data.

---

## Features

- Collects real-time weather data from a public API
- Cleans and processes the data using Python
- Stores structured weather data in a PostgreSQL database
- Automates data collection with GitHub Actions (or cron)
- Optional: Visualizes data using Streamlit or Jupyter

---

## Tech Stack

- **Python** (3.8+)
- **OpenWeatherMap API** – Data source
- **PostgreSQL** – Cloud-hosted via [Render](https://render.com) or [Supabase](https://supabase.com)
- **pandas** – For data processing
- **SQLAlchemy / psycopg2** – For DB connection
- **GitHub Actions** – For automation (optional)
- **Streamlit** – Optional dashboard for visualization

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
    DB_URL=your_postgresql_connection_string

---

## Usage

1. Run the ETL script manually:
    python etl_pipeline.py

2. Launch the Streamlit dashboard:
    streamlit run dashboard.py



