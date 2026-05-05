import pandas as pd
from datetime import datetime, timezone

from validators import engine as ve


def test_run_validations_pass_current():
    ts = datetime(2026, 2, 6, 10, 0, 0, tzinfo=timezone.utc)
    df = pd.DataFrame([
        {
            "city": "Toronto",
            "timestamp": ts,
            "temp_c": 5.0,
            "feels_like_c": 3.0,
            "humidity_pct": 50,
            "pressure_hpa": 1010,
            "wind_speed_ms": 3.2,
            "wind_deg": 180,
            "weather_main": "Clear",
            "weather_description": "clear sky",
            "clouds_pct": 0,
            "feels_like_delta": -2.0,
        }
    ])

    report = ve.run_validations(df, data_type="current")
    assert isinstance(report, ve.ValidationReport)
    assert report.failures == []


def test_run_validations_missing_required_column():
    # Missing 'timestamp' should trigger an error
    df = pd.DataFrame([
        {"city": "Toronto", "temp_c": 5.0}
    ])

    report = ve.run_validations(df, data_type="current")
    assert any(f.check == "schema" for f in report.failures)
