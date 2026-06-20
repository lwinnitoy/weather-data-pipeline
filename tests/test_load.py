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
import pandas as pd

from load import (
    load_weather,
    load_forecast,
    _get_last_loaded_timestamp,
    _validate_timestamp_gap,
    _extract_staging,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sample_staging_records():
    """Sample current weather records as returned by _extract_staging (post-rename)."""
    return [
        {
            'city': 'Toronto',
            'city_id': 1,
            'timestamp_utc': datetime.now(timezone.utc),
            'temp_c': 5.2,
            'feels_like_c': 2.1,
            'pressure_hpa': 1015,
            'humidity_pct': 80,
            'wind_speed_ms': 4.5,
            'wind_deg': 180,
            'weather_main': 'Clear',
            'weather_description': 'clear sky',
            'clouds_pct': 0,
            'feels_like_delta': -3.1
        }
    ]


@pytest.fixture
def sample_forecast_records():
    """Sample forecast records as returned by _extract_staging (post-rename)."""
    return [
        {
            'city': 'Toronto',
            'city_id': 1,
            'timestamp_utc': datetime.now(timezone.utc),
            'temp_c': 5.2,
            'feels_like_c': 2.1,
            'pressure_hpa': 1015,
            'humidity_pct': 80,
            'wind_speed_ms': 4.5,
            'weather_description': 'clear sky',
            'forecast_timestamp': datetime.now(timezone.utc),
            'forecast_horizon': 24,
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
# GET LAST LOADED TIMESTAMP TESTS
# =============================================================================

class TestGetLastLoadedTimestamp:
    """Tests for _get_last_loaded_timestamp.

    The function now accepts a cursor directly rather than opening its own
    connection, so tests pass a mock cursor instead of mocking psycopg2.connect.
    """

    def test_returns_timestamp_when_data_exists(self):
        """Test that timestamp is returned when records exist."""
        mock_cursor = MagicMock()
        expected_ts = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
        mock_cursor.fetchone.return_value = (expected_ts,)

        result = _get_last_loaded_timestamp(mock_cursor, city_id=1, data_type="current")

        assert result == expected_ts

    def test_returns_none_for_no_data(self):
        """Test that None is returned when no records exist (first run)."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (None,)

        result = _get_last_loaded_timestamp(mock_cursor, city_id=1, data_type="current")

        assert result is None

    def test_raises_runtime_error_on_db_failure(self):
        """Test that RuntimeError is raised when DB query fails."""
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = psycopg2.Error("Query failed")

        with pytest.raises(RuntimeError):
            _get_last_loaded_timestamp(mock_cursor, city_id=1, data_type="current")

    def test_queries_correct_table_for_current(self):
        """Test that weather_history table is queried for current."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (None,)

        _get_last_loaded_timestamp(mock_cursor, city_id=1, data_type="current")

        sql = mock_cursor.execute.call_args[0][0]
        assert "weather_history" in sql

    def test_queries_correct_table_for_forecast(self):
        """Test that weather_forecast table is queried for forecast."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (None,)

        _get_last_loaded_timestamp(mock_cursor, city_id=1, data_type="forecast")

        sql = mock_cursor.execute.call_args[0][0]
        assert "weather_forecast" in sql


# =============================================================================
# VALIDATE TIMESTAMP GAP TESTS
# =============================================================================

class TestValidateTimestampGap:
    """Tests for _validate_timestamp_gap."""

    def test_no_error_for_none_timestamp(self):
        """Test that None timestamp (first run) doesn't raise."""
        # Should not raise
        _validate_timestamp_gap(None, city_id=1)

    def test_no_error_for_recent_timestamp(self):
        """Test that recent timestamp doesn't raise."""
        recent = datetime.now(timezone.utc)

        # Should not raise
        _validate_timestamp_gap(recent, city_id=1)

    def test_raises_for_future_timestamp(self):
        """Test that future timestamp raises ValueError."""
        from datetime import timedelta
        future = datetime.now(timezone.utc) + timedelta(hours=1)

        with pytest.raises(ValueError, match="future"):
            _validate_timestamp_gap(future, city_id=1)

    def test_warns_for_large_gap(self, caplog):
        """Test that large gap logs a warning."""
        from datetime import timedelta
        import logging

        old = datetime.now(timezone.utc) - timedelta(hours=72)

        with caplog.at_level(logging.WARNING):
            _validate_timestamp_gap(old, city_id=1)

        assert "Large gap detected" in caplog.text


# =============================================================================
# EXTRACT STAGING TESTS
# =============================================================================

class TestExtractStaging:
    """Tests for _extract_staging.

    The function now takes a connection as its first argument and returns a
    dict keyed by city name instead of a flat list.
    """

    @patch('load.read_staging')
    @patch('load.list_raw_files_after')
    @patch('load._validate_timestamp_gap')
    @patch('load._get_last_loaded_timestamp')
    @patch('load.utils._get_city_mapping')
    def test_returns_empty_dict_when_no_cities(self, mock_city_map, *args):
        """Test that empty dict is returned when no cities."""
        mock_city_map.return_value = {}

        result = _extract_staging(MagicMock(), "current")

        assert result == {}

    @patch('load.read_staging')
    @patch('load.list_raw_files_after')
    @patch('load._validate_timestamp_gap')
    @patch('load._get_last_loaded_timestamp')
    @patch('load.utils._get_city_mapping')
    def test_adds_city_id_to_records(
        self, mock_city_map, mock_get_ts, mock_validate, mock_list_raw, mock_read_staging
    ):
        """Test that city_id is added to records from staging."""
        mock_city_map.return_value = {"Toronto": 1}
        mock_get_ts.return_value = datetime(2026, 2, 1, tzinfo=timezone.utc)
        mock_validate.return_value = None

        staging_df = pd.DataFrame([{
            'city': 'Toronto',
            'timestamp': datetime(2026, 2, 6, tzinfo=timezone.utc),
            'temp_c': 5.0
        }])
        mock_read_staging.return_value = staging_df

        result = _extract_staging(MagicMock(), "current")

        if result:
            records = list(result.values())[0]
            assert all('city_id' in r for r in records)


# =============================================================================
# LOAD WEATHER TESTS
# =============================================================================

class TestLoadWeather:
    """Tests for load_weather function."""

    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_does_nothing_when_no_records(self, mock_extract, mock_connect, mock_db_connection):
        """Test that empty records dict doesn't attempt DB insert."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_extract.return_value = {}

        load_weather()

    @patch('psycopg2.extras.execute_values')
    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_inserts_records(self, mock_extract, mock_connect, mock_execute_values, mock_db_connection, sample_staging_records):
        """Test that records are inserted into database."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_extract.return_value = {"Toronto": sample_staging_records}

        load_weather()

        mock_execute_values.assert_called_once()

    @patch('psycopg2.extras.execute_values')
    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_sql_contains_required_columns(self, mock_extract, mock_connect, mock_execute_values, mock_db_connection, sample_staging_records):
        """Test that INSERT statement has required columns."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_extract.return_value = {"Toronto": sample_staging_records}

        load_weather()

        sql = mock_execute_values.call_args[0][1]
        assert "city_id" in sql
        assert "timestamp_utc" in sql
        assert "temp_c" in sql
        assert "weather_history" in sql
        assert "ON CONFLICT" in sql

    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_database_error_is_raised(self, mock_extract, mock_connect, sample_staging_records):
        """Test that database errors are propagated."""
        mock_extract.return_value = {"Toronto": sample_staging_records}
        mock_connect.side_effect = psycopg2.Error("Connection failed")

        with pytest.raises(psycopg2.Error):
            load_weather()


# =============================================================================
# LOAD FORECAST TESTS
# =============================================================================

class TestLoadForecast:
    """Tests for load_forecast function."""

    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_does_nothing_when_no_records(self, mock_extract, mock_connect, mock_db_connection):
        """Test that empty records dict doesn't attempt DB insert."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_extract.return_value = {}

        load_forecast()

    @patch('psycopg2.extras.execute_values')
    @patch('load.psycopg2.connect')
    @patch('load._extract_staging')
    def test_sql_contains_forecast_columns(self, mock_extract, mock_connect, mock_execute_values, mock_db_connection, sample_forecast_records):
        """Test that INSERT has forecast-specific columns."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_extract.return_value = {"Toronto": sample_forecast_records}

        load_forecast()

        sql = mock_execute_values.call_args[0][1]
        assert "forecast_timestamp" in sql
        assert "forecast_horizon" in sql
        assert "weather_forecast" in sql
