/*
first version of weather forcast table
    id Unique row 
    city_id Reference to cities 
    timestamp_utc timestamp 
    timestamp_local Local timestamp 
    temp_c Temperature 
    feels_like_c Feels-like 
    pressure_hpa Pressure 
    humidity_pct Humidity 
    wind_speed_ms Wind speed 
    weather_description Summary 
    raw_json Raw payload
    forecast_timestamp forecasted time 
    forecast_horizon Hours ahead
*/
CREATE TABLE weather_forecast(
    id BIGSERIAL PRIMARY KEY,
    city_id INT REFERENCES cities(id),
    timestamp_utc TIMESTAMPTZ NOT NULL,
    temp_c FLOAT,
    feels_like_c FLOAT,
    pressure_hpa INT, 
    humidity_pct INT,
    wind_speed_ms FLOAT, 
    weather_description  TEXT, 
    raw_json JSONB,
    forecast_timestamp TIMESTAMP NOT NULL,
    forecast_horizon FLOAT
);