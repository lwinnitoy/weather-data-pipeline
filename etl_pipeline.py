import requests
import pandas as pd
from datetime import datetime
import psycopg2
from io import StringIO
from dotenv import load_dotenv
import os

load_dotenv()

API_KEY = os.getenv("API_KEY")
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
        #'wind_speed': [wind_data['speed']],
        'timestamp_utc': [datetime.now()]
    })

    return df

def load(df):
    #comment the line below to load env variables from github secrets
    #load_dotenv()


    # Fetch variables from github secrets
    USER = os.getenv("DB_USER")
    PASSWORD = os.getenv("DB_PASSWORD")
    HOST = os.getenv("DB_HOST")
    PORT = os.getenv("DB_PORT")
    DBNAME = os.getenv("DBNAME")

    # Prepare DataFrame as CSV in-memory
    cols = list(df.columns)
    csv_buffer = StringIO()
    df.to_csv(csv_buffer, index=False, header=True)
    csv_buffer.seek(0)

    try:
        # Connect to the database and use COPY for fast bulk load
        connection = psycopg2.connect(
            user=USER,
            password=PASSWORD,
            host=HOST,
            port=PORT,
            dbname=DBNAME,
        )
        print("Connection successful!")

        with connection:
            with connection.cursor() as cursor:
                # Quote column names to be safe
                columns_sql = ','.join([f'"{c}"' for c in cols])
                copy_sql = f"COPY weather_history ({columns_sql}) FROM STDIN WITH CSV HEADER"
                cursor.copy_expert(copy_sql, csv_buffer)

        print("Data loaded successfully using COPY.")
        connection.close()
        print("Connection closed.")

    except Exception as e:
        print(f"Failed to connect or load data: {e}")

def main():
    raw_data = extract()
    df = transform(raw_data)
    #print(df)
    load(df)


if __name__ == "__main__":
    main()
    print("ETL pipeline executed successfully.")