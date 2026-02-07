# tests/test_staging_transform.py
"""
Unit tests for staging_transform module.

Tests the Raw → Staging transformation layer.
Run with: pytest tests/test_staging_transform.py -v
"""
import pytest
import pandas as pd
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from staging_transform import (
    transform_current_to_record,
    transform_forecast_to_records,
    merge_with_existing,
    extract_partition_key,
    read_raw_json,
    process_city_current,
    process_city_forecast,
    process_partition_current,
    process_partition_forecast,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_current_raw():
    """Sample raw current weather API response."""
    return {
        "coord": {"lon": -79.3733, "lat": 43.7417},
        "weather": [{"id": 600, "main": "Snow", "description": "light snow", "icon": "13n"}],
        "base": "stations",
        "main": {
            "temp": -2.66,
            "feels_like": -9.61,
            "temp_min": -3.51,
            "temp_max": -1.96,
            "pressure": 1001,
            "humidity": 85
        },
        "visibility": 9656,
        "wind": {"speed": 7.6, "deg": 230, "gust": 0},
        "clouds": {"all": 100},
        "dt": 1770420103,
        "sys": {"type": 2, "id": 2009209, "country": "CA"},
        "timezone": -18000,
        "id": 5941602,
        "name": "Toronto",
        "cod": 200
    }


@pytest.fixture
def sample_forecast_raw():
    """Sample raw forecast API response with 2 forecast items."""
    return {
        "cod": "200",
        "message": 0,
        "cnt": 2,
        "list": [
            {
                "dt": 1770422400,
                "main": {
                    "temp": -2.62,
                    "feels_like": -9.62,
                    "pressure": 1003,
                    "humidity": 85
                },
                "weather": [{"id": 600, "main": "Snow", "description": "light snow"}],
                "clouds": {"all": 100},
                "wind": {"speed": 8.94, "deg": 325},
                "visibility": 2367,
                "pop": 1,
                "dt_txt": "2026-02-07 00:00:00"
            },
            {
                "dt": 1770433200,
                "main": {
                    "temp": -7.39,
                    "feels_like": -14.39,
                    "pressure": 1005,
                    "humidity": 84
                },
                "weather": [{"id": 804, "main": "Clouds", "description": "overcast clouds"}],
                "clouds": {"all": 96},
                "wind": {"speed": 9.77, "deg": 325},
                "visibility": 10000,
                "pop": 0,
                "dt_txt": "2026-02-07 03:00:00"
            }
        ],
        "city": {"id": 5941602, "name": "Toronto"}
    }


@pytest.fixture
def sample_timestamp():
    """Sample UTC timestamp."""
    return datetime(2026, 2, 6, 10, 30, 0, tzinfo=timezone.utc)


# =============================================================================
# TRANSFORM CURRENT TESTS
# =============================================================================

class TestTransformCurrentToRecord:
    """Tests for transform_current_to_record."""
    
    def test_extracts_all_fields(self, sample_current_raw, sample_timestamp):
        """Test that all required fields are extracted."""
        result = transform_current_to_record(sample_current_raw, "Toronto", sample_timestamp)
        
        assert result is not None
        assert result["city"] == "Toronto"
        assert result["timestamp"] == sample_timestamp
        assert result["temp_c"] == -2.66
        assert result["feels_like_c"] == -9.61
        assert result["humidity_pct"] == 85
        assert result["pressure_hpa"] == 1001
        assert result["wind_speed_ms"] == 7.6
        assert result["wind_deg"] == 230
        assert result["weather_main"] == "Snow"
        assert result["weather_description"] == "light snow"
        assert result["clouds_pct"] == 100
    
    def test_calculates_feels_like_delta(self, sample_current_raw, sample_timestamp):
        """Test feels_like_delta calculation."""
        result = transform_current_to_record(sample_current_raw, "Toronto", sample_timestamp)
        
        expected_delta = -9.61 - (-2.66)  # feels_like - temp
        assert result["feels_like_delta"] == pytest.approx(expected_delta)
    
    def test_returns_none_for_missing_main(self, sample_timestamp):
        """Test that missing 'main' returns None."""
        bad_data = {"weather": [{"main": "Clear"}], "wind": {"speed": 5}, "clouds": {"all": 0}}
        result = transform_current_to_record(bad_data, "Toronto", sample_timestamp)
        
        assert result is None
    
    def test_returns_none_for_missing_weather(self, sample_timestamp):
        """Test that missing 'weather' returns None."""
        bad_data = {"main": {"temp": 5, "feels_like": 3, "humidity": 80, "pressure": 1000},
                    "wind": {"speed": 5, "deg": 180}, "clouds": {"all": 0}}
        result = transform_current_to_record(bad_data, "Toronto", sample_timestamp)
        
        assert result is None
    
    def test_returns_none_for_empty_weather_list(self, sample_timestamp):
        """Test that empty weather list returns None."""
        bad_data = {"main": {"temp": 5, "feels_like": 3, "humidity": 80, "pressure": 1000},
                    "weather": [], "wind": {"speed": 5, "deg": 180}, "clouds": {"all": 0}}
        result = transform_current_to_record(bad_data, "Toronto", sample_timestamp)
        
        assert result is None
    
    def test_returns_none_for_none_input(self, sample_timestamp):
        """Test that None input returns None."""
        result = transform_current_to_record(None, "Toronto", sample_timestamp)
        
        assert result is None


# =============================================================================
# TRANSFORM FORECAST TESTS
# =============================================================================

class TestTransformForecastToRecords:
    """Tests for transform_forecast_to_records."""
    
    def test_returns_list_of_records(self, sample_forecast_raw, sample_timestamp):
        """Test that function returns a list."""
        result = transform_forecast_to_records(sample_forecast_raw, "Toronto", sample_timestamp)
        
        assert isinstance(result, list)
        assert len(result) == 2  # Our fixture has 2 forecast items
    
    def test_extracts_all_fields(self, sample_forecast_raw, sample_timestamp):
        """Test that all required fields are extracted from first record."""
        result = transform_forecast_to_records(sample_forecast_raw, "Toronto", sample_timestamp)
        
        first = result[0]
        assert first["city"] == "Toronto"
        assert first["fetched_at"] == sample_timestamp
        assert first["temp_c"] == -2.62
        assert first["feels_like_c"] == -9.62
        assert first["humidity_pct"] == 85
        assert first["pressure_hpa"] == 1003
        assert first["wind_speed_ms"] == 8.94
        assert first["wind_deg"] == 325
        assert first["weather_main"] == "Snow"
        assert first["weather_description"] == "light snow"
        assert first["clouds_pct"] == 100
    
    def test_calculates_forecast_for(self, sample_forecast_raw, sample_timestamp):
        """Test that forecast_for timestamp is extracted."""
        result = transform_forecast_to_records(sample_forecast_raw, "Toronto", sample_timestamp)
        
        first = result[0]
        expected_dt = datetime.fromtimestamp(1770422400, tz=timezone.utc)
        assert first["forecast_for"] == expected_dt
    
    def test_calculates_horizon_hours(self, sample_forecast_raw, sample_timestamp):
        """Test horizon_hours calculation."""
        result = transform_forecast_to_records(sample_forecast_raw, "Toronto", sample_timestamp)
        
        first = result[0]
        assert "horizon_hours" in first
        assert isinstance(first["horizon_hours"], int)
    
    def test_returns_empty_list_for_missing_list_key(self, sample_timestamp):
        """Test that missing 'list' returns empty list."""
        bad_data = {"cod": "200"}
        result = transform_forecast_to_records(bad_data, "Toronto", sample_timestamp)
        
        assert result == []
    
    def test_returns_empty_list_for_none_input(self, sample_timestamp):
        """Test that None input returns empty list."""
        result = transform_forecast_to_records(None, "Toronto", sample_timestamp)
        
        assert result == []


# =============================================================================
# MERGE WITH EXISTING TESTS
# =============================================================================

class TestMergeWithExisting:
    """Tests for merge_with_existing."""
    
    def test_returns_new_df_when_existing_is_none(self):
        """Test that new_df is returned when no existing data."""
        new_df = pd.DataFrame([
            {"city": "Toronto", "timestamp": datetime.now(timezone.utc), "temp_c": 5}
        ])
        
        result = merge_with_existing(None, new_df, "current")
        
        assert len(result) == 1
        pd.testing.assert_frame_equal(result.reset_index(drop=True), new_df.reset_index(drop=True))
    
    def test_concatenates_dataframes(self):
        """Test that existing and new are concatenated."""
        ts1 = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 2, 6, 11, 0, 0, tzinfo=timezone.utc)
        
        existing = pd.DataFrame([{"city": "Toronto", "timestamp": ts1, "temp_c": 5}])
        new_df = pd.DataFrame([{"city": "Toronto", "timestamp": ts2, "temp_c": 6}])
        
        result = merge_with_existing(existing, new_df, "current")
        
        assert len(result) == 2
    
    def test_deduplicates_current_by_city_timestamp(self):
        """Test that duplicates are removed for current weather."""
        ts = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        
        existing = pd.DataFrame([{"city": "Toronto", "timestamp": ts, "temp_c": 5}])
        new_df = pd.DataFrame([{"city": "Toronto", "timestamp": ts, "temp_c": 6}])  # Same timestamp
        
        result = merge_with_existing(existing, new_df, "current")
        
        assert len(result) == 1
        assert result.iloc[0]["temp_c"] == 6  # Keeps last (new)
    
    def test_deduplicates_forecast_by_city_fetched_at_forecast_for(self):
        """Test that duplicates are removed for forecast."""
        ts = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        forecast_for = datetime(2026, 2, 7, 12, 0, 0, tzinfo=timezone.utc)
        
        existing = pd.DataFrame([{"city": "Toronto", "fetched_at": ts, "forecast_for": forecast_for, "temp_c": 5}])
        new_df = pd.DataFrame([{"city": "Toronto", "fetched_at": ts, "forecast_for": forecast_for, "temp_c": 6}])
        
        result = merge_with_existing(existing, new_df, "forecast")
        
        assert len(result) == 1
        assert result.iloc[0]["temp_c"] == 6


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================

class TestExtractPartitionKey:
    """Tests for extract_partition_key."""
    
    def test_extracts_year_month(self):
        """Test that year and month are extracted."""
        ts = datetime(2026, 2, 15, 10, 30, 0, tzinfo=timezone.utc)
        
        result = extract_partition_key(ts)
        
        assert result == (2026, 2)
    
    def test_handles_december(self):
        """Test December extraction."""
        ts = datetime(2025, 12, 31, 23, 59, 0, tzinfo=timezone.utc)
        
        result = extract_partition_key(ts)
        
        assert result == (2025, 12)
    
    def test_handles_january(self):
        """Test January extraction."""
        ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        
        result = extract_partition_key(ts)
        
        assert result == (2026, 1)


class TestReadRawJson:
    """Tests for read_raw_json."""
    
    def test_reads_valid_json(self, tmp_path):
        """Test reading a valid JSON file."""
        json_file = tmp_path / "test.json"
        json_file.write_text('{"key": "value", "number": 42}')
        
        result = read_raw_json(json_file)
        
        assert result == {"key": "value", "number": 42}
    
    def test_returns_none_for_invalid_json(self, tmp_path):
        """Test that invalid JSON returns None."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text('not valid json {{{')
        
        result = read_raw_json(bad_file)
        
        assert result is None
    
    def test_returns_none_for_missing_file(self, tmp_path):
        """Test that missing file returns None."""
        missing = tmp_path / "missing.json"
        
        result = read_raw_json(missing)
        
        assert result is None


# =============================================================================
# PROCESS CITY TESTS (Integration-style with mocks)
# =============================================================================

class TestProcessCityCurrent:
    """Tests for process_city_current."""
    
    @patch('staging_transform.list_raw_files_after')
    def test_returns_zero_when_no_files(self, mock_list_files):
        """Test that 0 is returned when no raw files exist."""
        mock_list_files.return_value = []
        
        result = process_city_current("Toronto")
        
        assert result == 0
    
    @patch('staging_transform.set_high_water_mark')
    @patch('staging_transform.write_staging')
    @patch('staging_transform.read_staging')
    @patch('staging_transform.get_high_water_mark')
    @patch('staging_transform.list_raw_files_after')
    @patch('staging_transform.read_raw_json')
    def test_processes_files_and_updates_hwm(
        self, mock_read_json, mock_list_files, mock_get_hwm, 
        mock_read_staging, mock_write_staging, mock_set_hwm
    ):
        """Test full processing flow."""
        # Setup mocks
        mock_path = MagicMock()
        mock_path.stem = "1770420621"  # Unix timestamp
        mock_list_files.return_value = [mock_path]
        mock_get_hwm.return_value = None
        mock_read_staging.return_value = None
        mock_read_json.return_value = {
            "main": {"temp": 5, "feels_like": 3, "humidity": 80, "pressure": 1000},
            "weather": [{"main": "Clear", "description": "clear sky"}],
            "wind": {"speed": 5, "deg": 180},
            "clouds": {"all": 0}
        }
        
        result = process_city_current("Toronto")
        
        assert result == 1
        mock_write_staging.assert_called_once()
        mock_set_hwm.assert_called_once()


class TestProcessCityForecast:
    """Tests for process_city_forecast."""
    
    @patch('staging_transform.list_raw_files_after')
    def test_returns_zero_when_no_files(self, mock_list_files):
        """Test that 0 is returned when no raw files exist."""
        mock_list_files.return_value = []
        
        result = process_city_forecast("Toronto")
        
        assert result == 0
