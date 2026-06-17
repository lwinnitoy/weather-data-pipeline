from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import dashboard


def _mock_connection(mock_connect):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_connect.return_value.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
    return mock_conn, mock_cursor


def test_build_dashboard_snapshot_queries_warehouse_data():
    with patch("dashboard.psycopg2.connect") as mock_connect:
        _, mock_cursor = _mock_connection(mock_connect)
        mock_cursor.fetchone.side_effect = [
            (120, 3, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc)),
            (240, 3, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)),
        ]
        mock_cursor.fetchall.side_effect = [
            [
                (1, "Toronto", 50, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), 80, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)),
                (2, "Montreal", 70, datetime(2026, 6, 10, 10, 30, tzinfo=timezone.utc), 160, datetime(2026, 6, 10, 9, 30, tzinfo=timezone.utc)),
            ],
            [("Toronto", date(2026, 6, 9), 30)],
            [("Toronto", date(2026, 6, 9), 60)],
        ]

        snapshot = dashboard.build_dashboard_snapshot(days=7, threshold_minutes=120)

    assert snapshot.current_summary.rows == 120
    assert snapshot.forecast_summary.rows == 240
    assert len(snapshot.city_statuses) == 2
    assert snapshot.daily_current[0].rows == 30
    assert snapshot.daily_forecast[0].city_name == "Toronto"


def test_render_dashboard_html_contains_key_metrics():
    snapshot = dashboard.DashboardSnapshot(
        generated_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        current_summary=dashboard.SummaryMetrics("current", 120, 2, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), 1),
        forecast_summary=dashboard.SummaryMetrics("forecast", 240, 2, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc), 0),
        city_statuses=[
            dashboard.CityStatus(1, "Toronto", 50, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), 80, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc)),
            dashboard.CityStatus(2, "Montreal", 70, datetime(2026, 6, 10, 10, 30, tzinfo=timezone.utc), 160, datetime(2026, 6, 10, 9, 30, tzinfo=timezone.utc)),
        ],
        daily_current=[dashboard.DailyCount("current", "Toronto", date(2026, 6, 9), 30)],
        daily_forecast=[dashboard.DailyCount("forecast", "Montreal", date(2026, 6, 9), 60)],
    )

    html = dashboard.render_dashboard_html(snapshot)

    assert "Weather Data Pipeline Dashboard" in html
    assert "Current rows" in html
    assert "Toronto" in html
    assert "Current rows by day" in html


def test_write_dashboard_html_creates_parent_directories(tmp_path: Path):
    snapshot = dashboard.DashboardSnapshot(
        generated_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        current_summary=dashboard.SummaryMetrics("current", 120, 2, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), 1),
        forecast_summary=dashboard.SummaryMetrics("forecast", 240, 2, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc), 0),
        city_statuses=[dashboard.CityStatus(1, "Toronto", 50, datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc), 80, datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc))],
        daily_current=[],
        daily_forecast=[],
    )

    with patch("dashboard.build_dashboard_snapshot", return_value=snapshot):
        output = dashboard.write_dashboard_html(tmp_path / "site" / "index.html")

    assert output.exists()
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")