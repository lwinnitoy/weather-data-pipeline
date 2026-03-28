import json
from io import BytesIO
from datetime import datetime, UTC

import pandas as pd
import pytest
from pathlib import Path
from botocore.exceptions import ClientError
from unittest.mock import Mock

import storage
import config
import clients


def _parquet_bytes_from_df(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def test_write_and_read_staging_r2(monkeypatch):
    # Arrange: mock s3 client on the storage module
    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    city = "Toronto"
    year = 2026
    month = 2
    data_type = "current"

    df = pd.DataFrame({"temp": [1.0, 2.0], "humidity": [50, 60]})
    parquet_bytes = _parquet_bytes_from_df(df)

    # Mock get_object to return a file-like Body (BytesIO) when reading
    mock_client.get_object.return_value = {"Body": BytesIO(parquet_bytes)}

    # Act: call the write helper (should call put_object) and then read helper
    storage._write_staging_r2(city, year, month, df, data_type)

    # Assert: put_object was called with expected bucket and key
    assert mock_client.put_object.called, "Expected put_object to be called"
    _, kwargs = mock_client.put_object.call_args
    assert kwargs["Bucket"] == config.R2_BUCKET_NAME
    expected_key = storage._get_staging_key(city, year, month, data_type)
    assert kwargs["Key"] == expected_key
    assert isinstance(kwargs["Body"], (bytes, bytearray))

    # Now read back and compare DataFrame contents
    read_df = storage._read_staging_r2(city, year, month, data_type)
    # Ensure we received a DataFrame (helpful when mocks return None)
    assert read_df is not None, "Expected DataFrame from _read_staging_r2, got None"
    # Align column order with the original DataFrame to avoid false negatives
    read_df = read_df[df.columns]
    pd.testing.assert_frame_equal(read_df.reset_index(drop=True), df.reset_index(drop=True))


def test_get_keys_and_invalid_datatype():
    city = "Toronto"
    ts = datetime(2026, 2, 3, 4, 5, 6)

    raw_key = storage._get_raw_key(city, ts, "current")
    assert raw_key.startswith(f"{config.RAW_LAYER_DIR}/current/toronto/")

    staging_key = storage._get_staging_key(city, 2026, 2, "current")
    assert staging_key == f"{config.STAGING_LAYER_DIR}/current/city=toronto/year=2026/month=02/data.parquet"

    hwm_key = storage._get_hwm_key(city, 2026, 2, "current")
    assert hwm_key.endswith("/.last_processed.json")

    with pytest.raises(storage.InvalidDataTypeError):
        storage._get_raw_key(city, ts, "bad")
    with pytest.raises(storage.InvalidDataTypeError):
        storage._get_staging_key(city, 2026, 2, "bad")
    with pytest.raises(storage.InvalidDataTypeError):
        storage._get_hwm_key(city, 2026, 2, "bad")


def test_list_raw_files_after_r2(monkeypatch):
    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    # Paginator returns pages with Contents
    p1 = {"Contents": [{"Key": "raw/current/toronto/2026/02/01/100.json"}, {"Key": "raw/current/toronto/2026/02/01/300.json"}]}
    p2 = {"Contents": [{"Key": "raw/current/toronto/2026/02/01/200.json"}, {"Key": "raw/current/toronto/2026/02/01/not_a_ts.txt"}]}
    paginator = Mock()
    paginator.paginate.return_value = [p1, p2]
    mock_client.get_paginator.return_value = paginator

    objects = storage._list_raw_files_after_r2("Toronto", None, "current")
    assert isinstance(objects, list)
    assert all(isinstance(p, Path) for p in objects)

    stems = [int(p.stem) for p in objects]
    assert stems == sorted(stems)

    # Filter after timestamp (only those > 150)
    after = datetime.fromtimestamp(150)
    filtered = storage._list_raw_files_after_r2("Toronto", after, "current")
    assert all(int(p.stem) > 150 for p in filtered)


def test_read_staging_r2_handles_no_such_key(monkeypatch):
    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    error_response = {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}
    mock_client.get_object.side_effect = ClientError(error_response, "GetObject")

    got = storage._read_staging_r2("Toronto", 2026, 2, "current")
    assert got is None


def test_get_high_water_mark_r2_handles_no_such_key(monkeypatch):
    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    error_response = {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}
    mock_client.get_object.side_effect = ClientError(error_response, "GetObject")

    got = storage._get_high_water_mark_r2("Toronto", 2026, 2, "current")
    assert got is None


def test_read_staging_r2_with_raw_bytes(monkeypatch):
    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    city = "Toronto"
    year = 2026
    month = 2
    data_type = "current"

    df = pd.DataFrame({"temp": [1.0, 2.0], "humidity": [50, 60]})
    parquet_bytes = _parquet_bytes_from_df(df)

    # Mock get_object to return raw bytes (not file-like)
    mock_client.get_object.return_value = {"Body": parquet_bytes}

    read_df = storage._read_staging_r2(city, year, month, data_type)
    assert read_df is not None
    pd.testing.assert_frame_equal(read_df.reset_index(drop=True)[df.columns], df.reset_index(drop=True))


def test_set_and_get_high_water_mark_r2(monkeypatch):
    from unittest.mock import Mock

    mock_client = Mock()
    monkeypatch.setattr(clients, "s3", mock_client, raising=False)

    city = "Toronto"
    ts = datetime.now(UTC)
    year = ts.year
    month = ts.month

    # Arrange: mock get_object to return a JSON payload containing timestamp
    payload = json.dumps({"timestamp": ts.timestamp()}).encode()
    mock_client.get_object.return_value = {"Body": BytesIO(payload)}

    # Act: set high-water mark (should call put_object)
    storage._set_high_water_mark_r2(city, year, month, ts, "current")

    # Assert: put_object called with expected args
    assert mock_client.put_object.called
    _, kwargs = mock_client.put_object.call_args
    assert kwargs["Bucket"] == config.R2_BUCKET_NAME
    expected_key = storage._get_hwm_key(city, year, month, "current")
    assert kwargs["Key"] == expected_key

    # Validate payload written contains numeric timestamp
    body = kwargs["Body"]
    if isinstance(body, (bytes, bytearray)):
        written = json.loads(body)
    else:
        written = json.loads(body)
    assert float(written["timestamp"]) == pytest.approx(ts.timestamp(), rel=1e-6)

    # Act & Assert: get_high_water_mark_r2 should return a datetime roughly equal
    got = storage._get_high_water_mark_r2(city, year, month, "current")
    assert isinstance(got, datetime)
    assert got.timestamp() == pytest.approx(ts.timestamp(), rel=1e-6)
