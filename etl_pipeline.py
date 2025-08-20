import requests
import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv
import os
from datetime import datetime

load_dotenv()

API_KEY = os.getenv("API_KEY")
DB_URL = os.getenv("DB_URL")
CITY = "Victoria,CA"



def extract():
    url = f"http://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={API_KEY}&unit=metric"
    response = requests.get(url)
    data = response.json()
    return data

def transform(data):
    # Extract relevant data
    main_data = data['main']
    weather_data = data['weather'][0]
    wind_data = data['wind']

    # Create a DataFrame
    df = pd.DataFrame({
        'city': [CITY],
        'temperature_c': [main_data['temp']],
        #'pressure': [main_data['pressure']],
        'humidity_percent': [main_data['humidity']],
        'weather_description': [weather_data['description']],
        'wind_speed': [wind_data['speed']],
        'timestamp_utc': [datetime.now()]
    })

    return df

def load(df):
    # Create a database connection
    engine = create_engine(DB_URL)
    # Load the DataFrame into the database
    df.to_sql('weather_history', con=engine, if_exists='append', index=False)

def main():
    raw_data = extract()
    df = transform(raw_data)
    print(df)
    load(df)


if __name__ == "__main__":
    main()
    print("ETL pipeline executed successfully.")