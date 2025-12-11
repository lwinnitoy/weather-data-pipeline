/*
first version of create cities query
    id INT PK city id 
    city_name City name 
    country_code ISO-3166-1 (e.g., CA) 
    api_city_id OWM city ID 
    timezone IANA tz (e.g., America/Vancouver)
*/
CREATE TABLE cities (
    id INT PRIMARY KEY,
    city TEXT,
    city_ascii TEXT,
    province_id TEXT,
    province_name TEXT,
    lat TEXT,
    lng TEXT,
    population FLOAT,
    density FLOAT,
    timezone TEXT,
    ranking INT,
    postal TEXT,
    api_city_id TEXT
);