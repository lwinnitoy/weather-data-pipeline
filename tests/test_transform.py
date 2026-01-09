# tests/test_transform.py
"""
Unit tests for transform module.

Tests pure transformation functions with no external dependencies.
Run with: pytest tests/test_transform.py -v
"""
import pytest
import json
from datetime import datetime, timezone
from unittest.mock import patch

from transform import (
    transform_current_weather,
    transform_forecast,
    _transform_current_weather_record,
    _transform_forecast_record,
)


# =============================================================================
# FIXTURES - Sample API responses
# =============================================================================

@pytest.fixture
def valid_current_weather_response():
    """Sample valid OpenWeatherMap current weather response."""
    return {
        "coord": {"lon": -79.42, "lat": 43.70},
        "weather": [
            {"id": 800, "main": "Clear", "description": "clear sky", "icon": "01d"}
        ],
        "main": {
            "temp": 5.2,
            "feels_like": 2.1,
            "pressure": 1015,
            "humidity": 80
        },
        "wind": {"speed": 4.5, "deg": 180},
        "name": "Toronto"
    }


@pytest.fixture
def valid_forecast_response():
    """Sample valid OpenWeatherMap 5-day forecast response."""
    return {
        "list": [
            {
                "dt": 1734789600,
                "main": {
                    "temp": 6.5,
                    "feels_like": 3.2,
                    "pressure": 1012,
                    "humidity": 75
                },
                "weather": [
                    {"id": 801, "main": "Clouds", "description": "few clouds", "icon": "02d"}
                ],
                "wind": {"speed": 5.1, "deg": 200},
                "dt_txt": "2025-12-21 12:00:00"
            }
        ],
        "city": {"name": "Toronto"}
    }


@pytest.fixture
def incomplete_response_missing_main():
    """Response missing 'main' field."""
    return {
        "weather": [{"description": "clear sky"}],
        "wind": {"speed": 4.5}
    }


@pytest.fixture
def incomplete_response_empty_weather():
    """Response with empty weather array."""
    return {
        "main": {"temp": 5.2, "feels_like": 2.1, "pressure": 1015, "humidity": 80},
        "weather": [],
        "wind": {"speed": 4.5}
    }


# =============================================================================
# CURRENT WEATHER TRANSFORM TESTS
# =============================================================================

class TestTransformCurrentWeatherRecord:
    """Tests for _transform_current_weather_record helper."""
    
    def test_extracts_all_fields(self, valid_current_weather_response):
        """Test that all required fields are extracted."""
        result = _transform_current_weather_record(1, valid_current_weather_response)
        
        assert result is not None
        assert result['city_id'] == 1
        assert result['temp_c'] == 5.2
        assert result['feels_like_c'] == 2.1
        assert result['pressure_hpa'] == 1015
        assert result['humidity_pct'] == 80
        assert result['wind_speed_ms'] == 4.5
        assert result['weather_description'] == "clear sky"
    
    def test_timestamp_is_utc(self, valid_current_weather_response):
        """Test that timestamp is timezone-aware UTC."""
        result = _transform_current_weather_record(1, valid_current_weather_response)
        
        assert result['timestamp_utc'].tzinfo == timezone.utc
    
    def test_raw_json_preserved(self, valid_current_weather_response):
        """Test that original JSON is preserved."""
        result = _transform_current_weather_record(1, valid_current_weather_response)
        
        parsed = json.loads(result['raw_json'])
        assert parsed['main']['temp'] == 5.2
    
    def test_missing_main_returns_none(self, incomplete_response_missing_main):
        """Test that missing 'main' field returns None."""
        result = _transform_current_weather_record(1, incomplete_response_missing_main)
        assert result is None
    
    def test_empty_weather_array_returns_none(self, incomplete_response_empty_weather):
        """Test that empty weather array returns None."""
        result = _transform_current_weather_record(1, incomplete_response_empty_weather)
        assert result is None
    
    def test_missing_wind_returns_none(self):
        """Test that missing 'wind' field returns None."""
        data = {
            "main": {"temp": 5.2, "feels_like": 2.1, "pressure": 1015, "humidity": 80},
            "weather": [{"description": "clear sky"}]
            # missing 'wind'
        }
        result = _transform_current_weather_record(1, data)
        assert result is None


class TestTransformCurrentWeather:
    """Tests for transform_current_weather batch function."""
    
    def test_transforms_multiple_cities(self, valid_current_weather_response):
        """Test batch transformation of multiple cities."""
        city_data = {
            1: valid_current_weather_response,
            2: valid_current_weather_response,
            3: valid_current_weather_response,
        }
        
        records = transform_current_weather(city_data)
        
        assert len(records) == 3
        assert records[0]['city_id'] == 1
        assert records[1]['city_id'] == 2
        assert records[2]['city_id'] == 3
    
    def test_skips_invalid_records(self, valid_current_weather_response, incomplete_response_missing_main):
        """Test that invalid records are skipped, not failing the whole batch."""
        city_data = {
            1: valid_current_weather_response,
            2: incomplete_response_missing_main,  # Invalid
            3: valid_current_weather_response,
        }
        
        records = transform_current_weather(city_data)
        
        # Only 2 valid records
        assert len(records) == 2
        city_ids = [r['city_id'] for r in records]
        assert 1 in city_ids
        assert 3 in city_ids
        assert 2 not in city_ids
    
    def test_empty_input_returns_empty_list(self):
        """Test that empty input produces empty output."""
        records = transform_current_weather({})
        assert records == []
    
    def test_all_invalid_returns_empty_list(self, incomplete_response_missing_main):
        """Test that all-invalid input produces empty output."""
        city_data = {
            1: incomplete_response_missing_main,
            2: incomplete_response_missing_main,
        }
        
        records = transform_current_weather(city_data)
        assert records == []


# =============================================================================
# FORECAST TRANSFORM TESTS
# =============================================================================

class TestTransformForecastRecord:
    """Tests for _transform_forecast_record helper."""
    
    def test_extracts_all_fields(self, valid_forecast_response):
        """Test that all required fields are extracted."""
        result = _transform_forecast_record(1, valid_forecast_response)
        
        assert result is not None
        assert result['city_id'] == 1
        assert result['temp_c'] == 6.5
        assert result['feels_like_c'] == 3.2
        assert result['pressure_hpa'] == 1012
        assert result['humidity_pct'] == 75
        assert result['wind_speed_ms'] == 5.1
        assert result['weather_description'] == "few clouds"
    
    def test_forecast_timestamp_parsed(self, valid_forecast_response):
        """Test that forecast timestamp is correctly parsed."""
        result = _transform_forecast_record(1, valid_forecast_response)
        
        assert result['forecast_timestamp'] == datetime(2025, 12, 21, 12, 0, 0, tzinfo=timezone.utc)
    
    def test_forecast_horizon_calculated(self, valid_forecast_response):
        """Test that hours ahead is calculated."""
        result = _transform_forecast_record(1, valid_forecast_response)
        
        # forecast_horizon should be an integer representing hours
        assert isinstance(result['forecast_horizon'], int)
    
    def test_missing_list_returns_none(self):
        """Test that missing 'list' field returns None."""
        data = {"city": {"name": "Toronto"}}
        result = _transform_forecast_record(1, data)
        assert result is None


class TestTransformForecast:
    """Tests for transform_forecast batch function."""
    
    def test_transforms_multiple_cities(self, valid_forecast_response):
        """Test batch transformation."""
        city_data = {
            1: valid_forecast_response,
            2: valid_forecast_response,
        }
        
        records = transform_forecast(city_data)
        
        assert len(records) == 2
    
    def test_skips_invalid_records(self, valid_forecast_response):
        """Test that invalid records are skipped."""
        city_data = {
            1: valid_forecast_response,
            2: {"invalid": "data"},  # Missing 'list'
        }
        
        records = transform_forecast(city_data)
        
        assert len(records) == 1
        assert records[0]['city_id'] == 1
