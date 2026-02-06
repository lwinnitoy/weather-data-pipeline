/* migration to drop raw_json columns once they have been backfilled into data lake*/

AlTER TABLE weather_forecast DROP COLUMN raw_json

AlTER TABLE weather_history DROP COLUMN raw_json