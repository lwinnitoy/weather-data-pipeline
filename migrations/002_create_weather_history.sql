/*
first version of create cities query
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
*/
CREATE TABLE weather_history(
    id BIGSERIAL PRIMARY KEY,
    city_id INT REFERENCES cities(id),
    timestamp_utc TIMESTAMPTZ NOT NULL,
    temp_c FLOAT,
    feels_like_c FLOAT,
    pressure_hpa INT, 
    humidity_pct INT,
    wind_speed_ms FLOAT, 
    weather_description  TEXT, 
    raw_json JSONB
);