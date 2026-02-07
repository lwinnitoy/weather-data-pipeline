/* migration to drop raw_json columns once they have been backfilled into data lake*/
AlTER TABLE weather_forecast 
ALTER COLUMN raw_json DROP NOT NULL;

AlTER TABLE weather_history 
ALTER COLUMN raw_json DROP NOT NULL;