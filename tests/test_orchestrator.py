# tests/test_orchestrator.py
"""
Unit tests for orchestrator module.

Tests pipeline orchestration with mocked dependencies.
Run with: pytest tests/test_orchestrator.py -v
"""
import logging
import pytest
from unittest.mock import patch, MagicMock, call

from orchestrator import (
    run_pipeline,
    _run_extract,
    _run_staging,
    _run_load,
)


# =============================================================================
# RUN EXTRACT TESTS
# =============================================================================

class TestRunExtract:
    """Tests for _run_extract helper."""
    
    @patch('orchestrator.utils._get_city_mapping')
    @patch('orchestrator.extract_openweathermap')
    def test_returns_true_on_success(self, mock_extract, mock_city_map):
        """Test that True is returned when extraction succeeds."""
        mock_city_map.return_value = {"Toronto": 1, "Montreal": 2}
        mock_extract.return_value = {1: {"data": "weather"}}
        
        success, total, succeeded, failed = _run_extract("current")
        
        assert success is True
        assert total == 2
        assert succeeded == 1
        assert failed == 1
        mock_extract.assert_called_once_with("current")
    
    @patch('orchestrator.utils._get_city_mapping')
    @patch('orchestrator.extract_openweathermap')
    def test_returns_false_on_empty_result(self, mock_extract, mock_city_map):
        """Test that False is returned when extraction returns empty dict."""
        mock_city_map.return_value = {"Toronto": 1, "Montreal": 2}
        mock_extract.return_value = {}
        
        success, total, succeeded, failed = _run_extract("current")
        
        assert success is False
        assert total == 2
        assert succeeded == 0
        assert failed == 2
    
    @patch('orchestrator.utils._get_city_mapping')
    @patch('orchestrator.extract_openweathermap')
    def test_passes_current_to_extract(self, mock_extract, mock_city_map):
        """Test that 'current' is passed to extract_openweathermap."""
        mock_city_map.return_value = {"Toronto": 1}
        mock_extract.return_value = {"data": "test"}
        
        _run_extract("current")
        
        mock_extract.assert_called_once_with("current")
    
    @patch('orchestrator.utils._get_city_mapping')
    @patch('orchestrator.extract_openweathermap')
    def test_passes_forecast_to_extract(self, mock_extract, mock_city_map):
        """Test that 'forecast' is passed to extract_openweathermap."""
        mock_city_map.return_value = {"Toronto": 1}
        mock_extract.return_value = {"data": "test"}
        
        _run_extract("forecast")
        
        mock_extract.assert_called_once_with("forecast")


# =============================================================================
# RUN STAGING TESTS
# =============================================================================

class TestRunStaging:
    """Tests for _run_staging helper."""
    
    @patch('orchestrator.staging_transform')
    @patch('orchestrator.utils._get_city_mapping')
    def test_processes_each_city_current(self, mock_city_map, mock_staging):
        """Test that each city is processed for current weather."""
        mock_city_map.return_value = {"Toronto": 1, "Montreal": 2}
        mock_staging.process_city_current.return_value = 5
        
        result = _run_staging("current")
        
        assert result == 10  # 5 records per city × 2 cities
        assert mock_staging.process_city_current.call_count == 2
        mock_staging.process_city_current.assert_any_call("Toronto")
        mock_staging.process_city_current.assert_any_call("Montreal")
    
    @patch('orchestrator.staging_transform')
    @patch('orchestrator.utils._get_city_mapping')
    def test_processes_each_city_forecast(self, mock_city_map, mock_staging):
        """Test that each city is processed for forecast."""
        mock_city_map.return_value = {"Toronto": 1}
        mock_staging.process_city_forecast.return_value = 40
        
        result = _run_staging("forecast")
        
        assert result == 40
        mock_staging.process_city_forecast.assert_called_once_with("Toronto")
    
    @patch('orchestrator.staging_transform')
    @patch('orchestrator.utils._get_city_mapping')
    def test_returns_zero_when_no_cities(self, mock_city_map, mock_staging):
        """Test that 0 is returned when no cities exist."""
        mock_city_map.return_value = {}
        
        result = _run_staging("current")
        
        assert result == 0


# =============================================================================
# RUN LOAD TESTS
# =============================================================================

class TestRunLoad:
    """Tests for _run_load helper."""
    
    @patch('orchestrator.load')
    def test_calls_load_weather_for_current(self, mock_load):
        """Test that load_weather is called for current data type."""
        mock_load.load_weather.return_value = 10
        _run_load("current")
        
        mock_load.load_weather.assert_called_once()
        mock_load.load_forecast.assert_not_called()
    
    @patch('orchestrator.load')
    def test_calls_load_forecast_for_forecast(self, mock_load):
        """Test that load_forecast is called for forecast data type."""
        mock_load.load_forecast.return_value = 20
        _run_load("forecast")
        
        mock_load.load_forecast.assert_called_once()
        mock_load.load_weather.assert_not_called()


# =============================================================================
# RUN PIPELINE TESTS
# =============================================================================

class TestRunPipeline:
    """Tests for run_pipeline main function."""
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_runs_all_steps_for_each_data_type(self, mock_extract, mock_staging, mock_load):
        """Test that all steps run for each data type."""
        mock_extract.return_value = (True, 2, 2, 0)
        mock_staging.return_value = 10
        mock_load.return_value = 9
        
        run_pipeline(["current", "forecast"])
        
        # Extract called for both
        assert mock_extract.call_count == 2
        mock_extract.assert_any_call("current")
        mock_extract.assert_any_call("forecast")
        
        # Staging called for both
        assert mock_staging.call_count == 2
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_skips_staging_when_extract_fails(self, mock_extract, mock_staging, mock_load):
        """Test that staging is skipped when extraction fails."""
        mock_extract.return_value = (False, 2, 0, 2)
        
        run_pipeline(["current"])
        
        mock_extract.assert_called_once()
        mock_staging.assert_not_called()
        mock_load.assert_not_called()
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_continues_on_error_for_one_data_type(self, mock_extract, mock_staging, mock_load):
        """Test that pipeline continues if one data type fails."""
        # First call fails, second succeeds
        mock_extract.side_effect = [(False, 2, 0, 2), (True, 2, 2, 0)]
        mock_staging.return_value = 10
        mock_load.return_value = 9
        
        run_pipeline(["current", "forecast"])
        
        # Both extracts attempted
        assert mock_extract.call_count == 2
        # Only forecast staging runs (current failed)
        assert mock_staging.call_count == 1
        mock_staging.assert_called_once_with("forecast")
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_handles_exception_gracefully(self, mock_extract, mock_staging, mock_load):
        """Test that exceptions don't crash the pipeline."""
        mock_extract.return_value = (True, 2, 2, 0)
        mock_staging.side_effect = Exception("Staging failed!")
        
        # Should not raise
        run_pipeline(["current"])
        
        mock_extract.assert_called_once()
        mock_staging.assert_called_once()
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_single_data_type(self, mock_extract, mock_staging, mock_load):
        """Test running with single data type."""
        mock_extract.return_value = (True, 2, 2, 0)
        mock_staging.return_value = 5
        mock_load.return_value = 5
        
        run_pipeline(["current"])
        
        mock_extract.assert_called_once_with("current")
        mock_staging.assert_called_once_with("current")
    
    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_empty_data_types_does_nothing(self, mock_extract, mock_staging, mock_load):
        """Test that empty data types list does nothing."""
        run_pipeline([])
        
        mock_extract.assert_not_called()
        mock_staging.assert_not_called()
        mock_load.assert_not_called()

    @patch('orchestrator._run_load')
    @patch('orchestrator._run_staging')
    @patch('orchestrator._run_extract')
    def test_emits_run_summary_log(self, mock_extract, mock_staging, mock_load, caplog):
        """Test that a run summary log line is emitted."""
        mock_extract.return_value = (True, 1, 1, 0)
        mock_staging.return_value = 3
        mock_load.return_value = 3

        with caplog.at_level(logging.INFO):
            run_pipeline(["current"])

        assert any("RUN_SUMMARY" in record.message for record in caplog.records)
