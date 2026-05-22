# tests/test_extract.py
"""
Unit tests for extract module.

Uses mocking to avoid actual API calls and database connections.
Run with: pytest tests/test_extract.py -v
"""
import pytest
from unittest.mock import patch, MagicMock
import requests
import time
import tenacity.nap as tenacity_nap

from retry import RETRY_MAX_ATTEMPTS

from extract import (
    extract_openweathermap,
    _get_cities_to_fetch,
)
from utils import _get_city_mapping


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def mock_cities():
    """Sample cities from database."""
    return [
        ("Toronto", 43.70, -79.42),
        ("Vancouver", 49.28, -123.12),
        ("Montreal", 45.50, -73.57),
    ]


@pytest.fixture
def mock_city_mapping():
    """Sample city name to ID mapping."""
    return {
        "Toronto": 1,
        "Vancouver": 2,
        "Montreal": 3,
    }


@pytest.fixture
def mock_api_response():
    """Sample successful API response."""
    return {
        "main": {"temp": 5.2, "feels_like": 2.1, "pressure": 1015, "humidity": 80},
        "weather": [{"description": "clear sky"}],
        "wind": {"speed": 4.5}
    }


# =============================================================================
# DATABASE HELPER TESTS
# =============================================================================

class TestGetCitiesToFetch:
    """Tests for _get_cities_to_fetch."""
    
    @patch('extract._get_db_connection')
    def test_returns_city_list(self, mock_conn, mock_cities):
        """Test that cities are fetched from database."""
        # Setup mock cursor
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_cities
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        
        # Setup mock connection
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_connection.__enter__ = MagicMock(return_value=mock_connection)
        mock_connection.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = mock_connection
        
        result = _get_cities_to_fetch()
        
        assert len(result) == 3
        assert result[0] == ("Toronto", 43.70, -79.42)
    
    @patch('extract._get_db_connection')
    def test_executes_correct_query(self, mock_conn):
        """Test that correct SQL query is executed."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_connection.__enter__ = MagicMock(return_value=mock_connection)
        mock_connection.__exit__ = MagicMock(return_value=False)
        mock_conn.return_value = mock_connection
        
        _get_cities_to_fetch()
        
        mock_cursor.execute.assert_called_once()
        call_args = mock_cursor.execute.call_args[0][0]
        assert "SELECT" in call_args
        assert "cities" in call_args


class TestGetCityMapping:
    """Tests for _get_city_mapping."""
    
    @patch('utils.psycopg2.connect')
    def test_returns_dict_mapping(self, mock_connect):
        """Test that city mapping is returned as dict."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [(1, "Toronto"), (2, "Vancouver")]
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_connection.__enter__ = MagicMock(return_value=mock_connection)
        mock_connection.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_connection
        
        result = _get_city_mapping()
        
        assert result == {"Toronto": 1, "Vancouver": 2}


# =============================================================================
# API EXTRACTION TESTS
# =============================================================================

class TestExtractOpenweathermap:
    """Tests for extract_openweathermap."""
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_successful_extraction(self, mock_get, mock_cities_fetch, mock_mapping,
                                   mock_cities, mock_city_mapping, mock_api_response):
        """Test successful extraction for all cities."""
        mock_cities_fetch.return_value = mock_cities
        mock_mapping.return_value = mock_city_mapping
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_api_response
        mock_get.return_value = mock_response
        
        result = extract_openweathermap("current")
        
        assert len(result) == 3
        assert 1 in result  # Toronto
        assert 2 in result  # Vancouver
        assert 3 in result  # Montreal
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_handles_api_error(self, mock_get, mock_cities_fetch, mock_mapping,
                               mock_cities, mock_city_mapping):
        """Test that API errors for one city don't fail the whole batch."""
        mock_cities_fetch.return_value = mock_cities
        mock_mapping.return_value = mock_city_mapping
        
        # First call succeeds, second fails, third succeeds
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = {"main": {}, "weather": [], "wind": {}}
        
        error_response = MagicMock()
        error_response.status_code = 404
        error_response.text = "not found"
        
        mock_get.side_effect = [success_response, error_response, success_response]
        
        result = extract_openweathermap("current")
        
        # 2 cities succeeded (1 failed with 404 error)
        assert len(result) == 2
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_handles_network_timeout(self, mock_get, mock_cities_fetch, mock_mapping,
                                     mock_cities, mock_city_mapping, mock_api_response):
        """Test that network timeouts are handled gracefully."""
        mock_cities_fetch.return_value = mock_cities
        mock_mapping.return_value = mock_city_mapping
        
        # First times out, others succeed
        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = mock_api_response
        
        mock_get.side_effect = [
            requests.RequestException("Timeout"),
            success_response,
            success_response,
        ]
        
        result = extract_openweathermap("current")
        
        # 2 cities succeeded (1 timed out)
        assert len(result) == 2

    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_retries_on_retryable_status(self, mock_get, mock_cities_fetch, mock_mapping,
                                         mock_cities, mock_city_mapping, mock_api_response, monkeypatch):
        """Test that retryable HTTP status codes trigger retries and can succeed."""
        mock_cities_fetch.return_value = [mock_cities[0]]
        mock_mapping.return_value = {"Toronto": 1}

        sleeps = []
        sleep_recorder = lambda s: sleeps.append(s)
        monkeypatch.setattr(tenacity_nap, "sleep", sleep_recorder)
        monkeypatch.setattr(time, "sleep", sleep_recorder)

        retry_response = MagicMock()
        retry_response.status_code = 503
        retry_response.text = "server error"

        success_response = MagicMock()
        success_response.status_code = 200
        success_response.json.return_value = mock_api_response

        mock_get.side_effect = [retry_response, retry_response, success_response]

        result = extract_openweathermap("current")

        assert mock_get.call_count == 3
        assert result == {1: mock_api_response}

    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_retries_exhausted_on_retryable_status(self, mock_get, mock_cities_fetch, mock_mapping,
                                                   mock_cities, mock_city_mapping, monkeypatch):
        """Test that retries exhaust and the city is skipped for retryable errors."""
        mock_cities_fetch.return_value = [mock_cities[0]]
        mock_mapping.return_value = {"Toronto": 1}

        sleeps = []
        sleep_recorder = lambda s: sleeps.append(s)
        monkeypatch.setattr(tenacity_nap, "sleep", sleep_recorder)
        monkeypatch.setattr(time, "sleep", sleep_recorder)

        retry_response = MagicMock()
        retry_response.status_code = 503
        retry_response.text = "server error"

        mock_get.return_value = retry_response

        result = extract_openweathermap("current")

        assert mock_get.call_count == RETRY_MAX_ATTEMPTS
        assert result == {}
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_invalid_data_type_raises(self, mock_get, mock_cities_fetch, mock_mapping,
                                          mock_cities, mock_city_mapping, mock_api_response):
        """Test that invalid data types raise ValueError."""
        mock_cities_fetch.return_value = [mock_cities[0]]  # Just one city
        mock_mapping.return_value = mock_city_mapping
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_api_response
        mock_get.return_value = mock_response
        
        with pytest.raises(ValueError):
            extract_openweathermap("invalid_endpoint")

        mock_get.assert_not_called()
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    @patch('extract.requests.get')
    def test_forecast_endpoint(self, mock_get, mock_cities_fetch, mock_mapping,
                               mock_cities, mock_city_mapping, mock_api_response):
        """Test that forecast endpoint is used when specified."""
        mock_cities_fetch.return_value = [mock_cities[0]]
        mock_mapping.return_value = mock_city_mapping
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_api_response
        mock_get.return_value = mock_response
        
        extract_openweathermap("forecast")
        
        call_url = mock_get.call_args[0][0]
        assert "/forecast?" in call_url
    
    @patch('utils._get_city_mapping')
    @patch('extract._get_cities_to_fetch')
    def test_empty_cities_returns_empty_dict(self, mock_cities_fetch, mock_mapping):
        """Test that no cities produces empty result."""
        mock_cities_fetch.return_value = []
        mock_mapping.return_value = {}
        
        result = extract_openweathermap("current")
        
        assert result == {}
