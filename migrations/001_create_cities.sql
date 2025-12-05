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
    city_name TEXT,
    country_code CHAR(2),
    api_city_id INT,
    timezone TEXT
);