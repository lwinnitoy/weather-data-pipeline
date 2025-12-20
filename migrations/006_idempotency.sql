ALTER TABLE weather_history ADD CONSTRAINT unique_weather_record 
    UNIQUE (city_id, timestamp_utc);

ALTER TABLE weather_forecast ADD CONSTRAINT unique_forecast_record 
    UNIQUE (city_id, forecast_timestamp, timestamp_utc);