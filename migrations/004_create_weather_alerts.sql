/*
Stores severe weather alerts from OneCall API
Fist iteration of table
    event Alert name 
    sender Issuing agency 
    start_utc Start 
    end_utc End 
    severity Advisory/Watch/Warning 
    description  Alert summary 
*/
CREATE TABLE weather_alerts(
    event TEXT, 
    sender TEXT, 
    start_utc TIMESTAMPTZ, 
    end_utc TIMESTAMPTZ,
    severity TEXT, 
    description TEXT
);