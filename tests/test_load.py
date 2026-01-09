# tests/test_load.py
"""
Unit tests for load module.

Uses mocking to avoid actual database connections.
Run with: pytest tests/test_load.py -v
"""
import pytest
from unittest.mock import patch, MagicMock, call
import psycopg2
from datetime import datetime, timezone

from load import load_weather, load_forecast


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_weather_records():
    """Sample weather records for insertion."""
    return [
        {
            'city_id': 1,
            'timestamp_utc': datetime.now(timezone.utc),
            'temp_c': 5.2,
            'feels_like_c': 2.1,
            'pressure_hpa': 1015,
            'humidity_pct': 80,
            'wind_speed_ms': 4.5,
            'weather_description': 'clear sky',
            'raw_json': '{"test": "data"}'
        },
        {
            'city_id': 2,
            'timestamp_utc': datetime.now(timezone.utc),
            'temp_c': 3.1,
            'feels_like_c': 0.5,
            'pressure_hpa': 1012,
            'humidity_pct': 75,
            'wind_speed_ms': 6.2,
            'weather_description': 'few clouds',
            'raw_json': '{"test": "data2"}'
        }
    ]


@pytest.fixture
def sample_forecast_records():
    """Sample forecast records for insertion."""
    return [
        {
            'city_id': 1,
            'timestamp_utc': datetime.now(timezone.utc),
            'temp_c': 6.5,
            'feels_like_c': 3.2,
            'pressure_hpa': 1012,
            'humidity_pct': 75,
            'wind_speed_ms': 5.1,
            'weather_description': 'few clouds',
            'raw_json': '{"test": "forecast"}',
            'forecast_timestamp': datetime(2025, 12, 21, 12, 0, 0, tzinfo=timezone.utc),
            'forecast_horizon': 24
        }
    ]


@pytest.fixture
def mock_db_connection():
    """Create a mock database connection with cursor."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    
    return mock_conn, mock_cursor


# =============================================================================
# LOAD WEATHER TESTS
# =============================================================================

class TestLoadWeather:
    """Tests for load_weather function."""
    
    @patch('load.psycopg2.connect')
    def test_inserts_records(self, mock_connect, sample_weather_records, mock_db_connection):
        """Test that records are inserted into database."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        
        load_weather(sample_weather_records)
        
        # executemany should be called with records
        mock_cursor.executemany.assert_called_once()
        call_args = mock_cursor.executemany.call_args
        assert len(call_args[0][1]) == 2  # 2 records
    
    @patch('load.psycopg2.connect')
    def test_sql_contains_required_columns(self, mock_connect, sample_weather_records, mock_db_connection):
        """Test that INSERT statement has all required columns."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        
        load_weather(sample_weather_records)
        
        sql = mock_cursor.executemany.call_args[0][0]
        
        # Check required columns are in SQL
        assert "city_id" in sql
        assert "timestamp_utc" in sql
        assert "temp_c" in sql
        assert "weather_history" in sql
        assert "ON CONFLICT" in sql
    
    @patch('load.psycopg2.connect')
    def test_empty_records_does_nothing(self, mock_connect):
        """Test that empty list doesn't attempt database operation."""
        load_weather([])
        
        # Should not even connect to database
        mock_connect.assert_not_called()
    
    @patch('load.psycopg2.connect')
    def test_database_error_is_raised(self, mock_connect, sample_weather_records):
        """Test that database errors are propagated."""
        mock_connect.side_effect = psycopg2.Error("Connection failed")
        
        with pytest.raises(psycopg2.Error):
            load_weather(sample_weather_records)


# =============================================================================
# LOAD FORECAST TESTS
# =============================================================================

class TestLoadForecast:
    """Tests for load_forecast function."""
    
    @patch('load.psycopg2.connect')
    def test_inserts_forecast_records(self, mock_connect, sample_forecast_records, mock_db_connection):
        """Test that forecast records are inserted."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        
        load_forecast(sample_forecast_records)
        
        mock_cursor.executemany.assert_called_once()
    
    @patch('load.psycopg2.connect')
    def test_sql_contains_forecast_columns(self, mock_connect, sample_forecast_records, mock_db_connection):
        """Test that INSERT has forecast-specific columns."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        
        load_forecast(sample_forecast_records)
        
        sql = mock_cursor.executemany.call_args[0][0]
        
        # Forecast-specific columns
        assert "forecast_timestamp" in sql
        assert "forecast_horizon" in sql
        assert "weather_forecast" in sql
    
    @patch('load.psycopg2.connect')
    def test_empty_forecast_does_nothing(self, mock_connect):
        """Test that empty list doesn't attempt database operation."""
        load_forecast([])
        
        mock_connect.assert_not_called()
    
    @patch('load.psycopg2.connect')
    def test_forecast_database_error_is_raised(self, mock_connect, sample_forecast_records):
        """Test that database errors are propagated."""
        mock_connect.side_effect = psycopg2.Error("Connection failed")
        
        with pytest.raises(psycopg2.Error):
            load_forecast(sample_forecast_records)
