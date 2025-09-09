import requests
import pandas as pd
from datetime import datetime
import psycopg2
from io import StringIO
from dotenv import load_dotenv
import os


#comment the line below loads env vars from .env file
#comment it out to load env variables from github secrets
#load_dotenv()

# loading openweathermap variables from github secrets
API_KEY = os.getenv("API_KEY")
CITY = "Victoria,CA"

# Fetch variables supabase variables from github secrets
USER = os.getenv("DB_USER")
PASSWORD = os.getenv("DB_PASSWORD")
HOST = os.getenv("DB_HOST")
PORT = os.getenv("DB_PORT")
DBNAME = os.getenv("DB_NAME")



def extract():
    url = f"http://api.openweathermap.org/data/2.5/weather?q={CITY}&appid={API_KEY}&units=metric"
    response = requests.get(url)
    data = response.json()
    return data

def transform(data):

    #catch api errors
    if "main" not in data:
        print("API Error:", data)  # Log the whole response for debugging
        return None
     
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

    # Guard: nothing to load
    if df is None or df.empty:
        print("No data to load.")
        return

    # Prepare DataFrame as CSV in-memory
    cols = list(df.columns)
    csv_buffer = StringIO()
    df.to_csv(csv_buffer, index=False, header=True)
    csv_buffer.seek(0)

    try:
        # Log target (redact password)
        try:
            print(f"Connecting to host={HOST} port={PORT} db={DBNAME} user={USER}")
        except Exception:
            pass

        # Connect using separate environment variables (no DATABASE_URL)
        connection = psycopg2.connect(
            user=USER,
            password=PASSWORD,
            host=HOST,
            port=PORT,
            dbname=DBNAME,
            sslmode=os.getenv("PGSSLMODE", "require")
        )
        print("Connection successful!")

        with connection:
            with connection.cursor() as cursor:
                columns_sql = ','.join([f'"{c}"' for c in cols])
                copy_sql = f"COPY weather_history ({columns_sql}) FROM STDIN WITH CSV HEADER"
                cursor.copy_expert(copy_sql, csv_buffer)

        print("Data loaded successfully using COPY.")
        connection.close()
        print("Connection closed.")

    except Exception as e:
        print("Failed to connect or load data:")
        print(repr(e))
        try:
            if isinstance(e, psycopg2.Error):
                pgcode = getattr(e, "pgcode", None)
                diag = getattr(e, "diag", None)
                print("pgcode:", pgcode)
                if diag:
                    print("message_primary:", getattr(diag, "message_primary", None))
                    print("detail:", getattr(diag, "detail", None))
        except Exception:
            pass
        raise

def main():
    raw_data = extract()
    df = transform(raw_data)
    load(df)


if __name__ == "__main__":
    try:
        main()
        print("ETL pipeline executed successfully.")
    except Exception as e:
        print("ETL pipeline failed:", repr(e))
        try:
            if isinstance(e, psycopg2.Error):
                print("pgcode:", getattr(e, "pgcode", None))
                diag = getattr(e, "diag", None)
                if diag:
                    print("message_primary:", getattr(diag, "message_primary", None))
                    print("detail:", getattr(diag, "detail", None))
        except Exception:
            pass
        raise