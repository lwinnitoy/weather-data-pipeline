# tests/test_storage.py
"""
Unit tests for storage module.

Run with: pytest tests/test_storage.py -v
"""
import pytest
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path

# Import modules under test
from storage import (
    write_staging, 
    read_staging,
    write_raw,
    read_raw_files,
    list_raw_files_after,
    get_raw_path,
    get_staging_path,
    _normalize_city_name,
    get_high_water_mark,
    set_high_water_mark,
)


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================

class TestNormalizeCityName:
    """Tests for _normalize_city_name helper."""
    
    def test_lowercase(self):
        assert _normalize_city_name("Toronto") == "toronto"
        assert _normalize_city_name("VANCOUVER") == "vancouver"
    
    def test_spaces_to_underscores(self):
        assert _normalize_city_name("New York") == "new_york"
        assert _normalize_city_name("Los Angeles") == "los_angeles"
    
    def test_combined(self):
        assert _normalize_city_name("NEW YORK") == "new_york"


class TestGetRawPath:
    """Tests for get_raw_path."""
    
    def test_path_structure(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 12, 20, 10, 30, 0)
        path = get_raw_path("Toronto", ts)
        
        # Check path components
        assert "raw" in path.parts
        assert "toronto" in path.parts
        assert "2025" in path.parts
        assert "12" in path.parts
        assert "20" in path.parts
        assert path.suffix == ".json"
    
    def test_zero_padding(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 3, 5, 10, 0, 0)  # March 5th
        path = get_raw_path("Toronto", ts)
        
        # Should be 03 and 05, not 3 and 5
        assert "03" in path.parts
        assert "05" in path.parts


class TestGetStagingPath:
    """Tests for get_staging_path."""
    
    def test_hive_style_partitioning(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        path = get_staging_path("Toronto", 2025, 12)
        
        # Check Hive-style partition names
        assert "city=toronto" in str(path)
        assert "year=2025" in str(path)
        assert "month=12" in str(path)
        assert path.name == "data.parquet"


# =============================================================================
# STAGING LAYER TESTS
# =============================================================================

class TestWriteStaging:
    """Tests for write_staging."""
    
    def test_creates_file(self, tmp_path, monkeypatch):
        """Test that write_staging creates a Parquet file."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        df = pd.DataFrame({
            'timestamp': [datetime.now()],
            'temp': [5.2],
            'humidity': [80]
        })
        
        path = write_staging("Toronto", 2025, 12, df)
        
        assert path.exists()
        assert path.suffix == '.parquet'
    
    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        """Test that parent directories are created automatically."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        df = pd.DataFrame({'temp': [5.2]})
        path = write_staging("Toronto", 2025, 12, df)
        
        assert path.parent.exists()
    
    def test_roundtrip(self, tmp_path, monkeypatch):
        """Test that data survives write → read cycle."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        original_df = pd.DataFrame({
            'temp': [5.2, 4.8, 4.1],
            'humidity': [80, 82, 85]
        })
        
        write_staging("Toronto", 2025, 12, original_df)
        loaded_df = read_staging("Toronto", 2025, 12)
        
        pd.testing.assert_frame_equal(original_df, loaded_df)
    
    def test_empty_dataframe(self, tmp_path, monkeypatch):
        """Test that empty DataFrames are handled without crashing."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        # Empty DF with schema (columns but no rows)
        df = pd.DataFrame({'temp': [], 'humidity': []})
        
        # Should not raise an exception
        path = write_staging("Toronto", 2025, 12, df)
        assert path.exists()
        
        # Reading back should give empty DataFrame
        loaded_df = read_staging("Toronto", 2025, 12)
        assert len(loaded_df) == 0
    
    def test_overwrite_existing(self, tmp_path, monkeypatch):
        """Test that existing files are overwritten by new files."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        original_df = pd.DataFrame({
            'temp': [5.2, 4.8, 4.1],
            'humidity': [80, 82, 85]
        })
        
        new_df = pd.DataFrame({
            'temp': [10.0],
            'humidity': [50]
        })
        
        orig_path = write_staging("Toronto", 2025, 12, original_df)
        new_path = write_staging("Toronto", 2025, 12, new_df)
        
        # Same partition = same path
        assert orig_path == new_path
        
        # Data should be the new data
        loaded_df = read_staging("Toronto", 2025, 12)
        pd.testing.assert_frame_equal(loaded_df, new_df)
    
    def test_city_name_normalized(self, tmp_path, monkeypatch):
        """Test that city names are normalized in path."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        df = pd.DataFrame({'temp': [5.2]})
        
        path = write_staging("NEW YORK", 2025, 12, df)
        
        assert "new_york" in str(path)
        assert "NEW YORK" not in str(path)


class TestReadStaging:
    """Tests for read_staging."""
    
    def test_nonexistent_file_returns_none(self, tmp_path, monkeypatch):
        """Test that reading non-existent partition returns None."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        result = read_staging("Toronto", 2025, 12)
        
        assert result is None
    
    def test_reads_correct_partition(self, tmp_path, monkeypatch):
        """Test that correct partition is read when multiple exist."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        dec_df = pd.DataFrame({'temp': [5.0]})
        jan_df = pd.DataFrame({'temp': [10.0]})
        
        write_staging("Toronto", 2025, 12, dec_df)
        write_staging("Toronto", 2026, 1, jan_df)
        
        loaded_dec = read_staging("Toronto", 2025, 12)
        loaded_jan = read_staging("Toronto", 2026, 1)
        
        pd.testing.assert_frame_equal(loaded_dec, dec_df)
        pd.testing.assert_frame_equal(loaded_jan, jan_df)


# =============================================================================
# RAW LAYER TESTS
# =============================================================================

class TestWriteRaw:
    """Tests for write_raw."""
    
    def test_creates_file(self, tmp_path, monkeypatch):
        """Test that write_raw creates a JSON file."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        data = {"temp": 5.2, "humidity": 80}
        ts = datetime.now()
        
        path = write_raw("Toronto", ts, data)
        
        assert path.exists()
        assert path.suffix == ".json"
    
    def test_data_preserved(self, tmp_path, monkeypatch):
        """Test that JSON content is correct."""
        import config
        import json
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        data = {"temp": 5.2, "humidity": 80, "nested": {"a": 1}}
        ts = datetime.now()
        
        path = write_raw("Toronto", ts, data)
        
        with path.open() as f:
            loaded = json.load(f)
        
        assert loaded == data
    
    def test_filename_is_unix_timestamp(self, tmp_path, monkeypatch):
        """Test that filename is the Unix timestamp."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 12, 20, 10, 30, 0)
        expected_unix = int(ts.timestamp())
        
        path = write_raw("Toronto", ts, {"temp": 5.0})
        
        assert path.stem == str(expected_unix)


class TestListRawFilesAfter:
    """Tests for list_raw_files_after."""
    
    def test_returns_empty_for_nonexistent_city(self, tmp_path, monkeypatch):
        """Test that non-existent city returns empty list."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        result = list_raw_files_after("NonExistentCity")
        
        assert result == []
    
    def test_returns_all_files_when_no_filter(self, tmp_path, monkeypatch):
        """Test that all files are returned when after_timestamp is None."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts1 = datetime(2025, 12, 20, 10, 0, 0)
        ts2 = datetime(2025, 12, 20, 11, 0, 0)
        ts3 = datetime(2025, 12, 20, 12, 0, 0)
        
        write_raw("Toronto", ts1, {"temp": 1})
        write_raw("Toronto", ts2, {"temp": 2})
        write_raw("Toronto", ts3, {"temp": 3})
        
        result = list_raw_files_after("Toronto")
        
        assert len(result) == 3
    
    def test_filters_by_timestamp(self, tmp_path, monkeypatch):
        """Test that files are filtered correctly by timestamp."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts1 = datetime(2025, 12, 20, 10, 0, 0)
        ts2 = datetime(2025, 12, 20, 11, 0, 0)
        ts3 = datetime(2025, 12, 20, 12, 0, 0)
        
        write_raw("Toronto", ts1, {"temp": 1})
        write_raw("Toronto", ts2, {"temp": 2})
        write_raw("Toronto", ts3, {"temp": 3})
        
        # Filter: only files after ts1
        result = list_raw_files_after("Toronto", after_timestamp=ts1)
        
        # Should return ts2 and ts3, not ts1
        assert len(result) == 2
    
    def test_sorted_by_timestamp(self, tmp_path, monkeypatch):
        """Test that results are sorted by timestamp ascending."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        # Write in random order
        ts2 = datetime(2025, 12, 20, 11, 0, 0)
        ts1 = datetime(2025, 12, 20, 10, 0, 0)
        ts3 = datetime(2025, 12, 20, 12, 0, 0)
        
        write_raw("Toronto", ts2, {"temp": 2})
        write_raw("Toronto", ts1, {"temp": 1})
        write_raw("Toronto", ts3, {"temp": 3})
        
        result = list_raw_files_after("Toronto")
        
        # Should be sorted: ts1, ts2, ts3
        timestamps = [int(p.stem) for p in result]
        assert timestamps == sorted(timestamps)


# =============================================================================
# HIGH-WATER MARK TESTS
# =============================================================================

class TestGetHighWaterMark:
    """Tests for get_high_water_mark."""
    
    def test_returns_none_when_no_marker(self, tmp_path, monkeypatch):
        """Test that None is returned when no marker file exists."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        result = get_high_water_mark("Toronto", 2025, 12)
        
        assert result is None
    
    def test_reads_existing_marker(self, tmp_path, monkeypatch):
        """Test that existing marker is read correctly."""
        import config
        import json
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        # Create marker file manually
        marker_path = get_staging_path("Toronto", 2025, 12).parent / ".last_processed.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        
        test_timestamp = datetime(2025, 12, 20, 10, 0, 0)
        with marker_path.open("w") as f:
            json.dump({"timestamp": test_timestamp.timestamp()}, f)
        
        result = get_high_water_mark("Toronto", 2025, 12)
        
        assert result is not None
        assert isinstance(result, datetime)
        # Allow small floating point differences
        assert abs(result.timestamp() - test_timestamp.timestamp()) < 1
    
    def test_returns_none_for_invalid_json(self, tmp_path, monkeypatch):
        """Test that invalid JSON returns None."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        marker_path = get_staging_path("Toronto", 2025, 12).parent / ".last_processed.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        
        with marker_path.open("w") as f:
            f.write("not valid json")
        
        result = get_high_water_mark("Toronto", 2025, 12)
        
        assert result is None
    
    def test_returns_none_for_missing_timestamp_key(self, tmp_path, monkeypatch):
        """Test that missing 'timestamp' key returns None."""
        import config
        import json
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        marker_path = get_staging_path("Toronto", 2025, 12).parent / ".last_processed.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        
        with marker_path.open("w") as f:
            json.dump({"wrong_key": 123456}, f)
        
        result = get_high_water_mark("Toronto", 2025, 12)
        
        assert result is None


class TestSetHighWaterMark:
    """Tests for set_high_water_mark."""
    
    def test_creates_marker_file(self, tmp_path, monkeypatch):
        """Test that marker file is created."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 12, 20, 10, 0, 0)
        set_high_water_mark("Toronto", 2025, 12, ts)
        
        marker_path = get_staging_path("Toronto", 2025, 12).parent / ".last_processed.json"
        assert marker_path.exists()
    
    def test_roundtrip(self, tmp_path, monkeypatch):
        """Test that set → get returns the same timestamp."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        original_ts = datetime(2025, 12, 20, 10, 30, 45)
        set_high_water_mark("Toronto", 2025, 12, original_ts)
        
        loaded_ts = get_high_water_mark("Toronto", 2025, 12)
        
        # Allow small floating point differences
        assert abs(loaded_ts.timestamp() - original_ts.timestamp()) < 1
    
    def test_overwrites_existing(self, tmp_path, monkeypatch):
        """Test that new marker overwrites old one."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        old_ts = datetime(2025, 12, 20, 10, 0, 0)
        new_ts = datetime(2025, 12, 20, 12, 0, 0)
        
        set_high_water_mark("Toronto", 2025, 12, old_ts)
        set_high_water_mark("Toronto", 2025, 12, new_ts)
        
        loaded_ts = get_high_water_mark("Toronto", 2025, 12)
        
        assert abs(loaded_ts.timestamp() - new_ts.timestamp()) < 1
    
    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        """Test that parent directories are created."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 12, 20, 10, 0, 0)
        
        # Should not raise even though directories don't exist
        set_high_water_mark("NewCity", 2025, 12, ts)
        
        marker_path = get_staging_path("NewCity", 2025, 12).parent / ".last_processed.json"
        assert marker_path.exists()
    
    def test_city_name_normalized(self, tmp_path, monkeypatch):
        """Test that city names are normalized in path."""
        import config
        monkeypatch.setattr(config, 'DATA_LAKE_ROOT', tmp_path)
        
        ts = datetime(2025, 12, 20, 10, 0, 0)
        set_high_water_mark("NEW YORK", 2025, 12, ts)
        
        # Should be able to read with any case
        loaded_ts = get_high_water_mark("new york", 2025, 12)
        
        assert loaded_ts is not None

