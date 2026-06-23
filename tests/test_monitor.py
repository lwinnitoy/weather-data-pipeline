"""
Unit tests for monitor.py.

All DB calls are mocked so no live Postgres connection is required.
Run with: pytest tests/test_monitor.py -v
"""
import os
import smtplib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import psycopg2
import pytest

from monitor import (
    FRESHNESS_THRESHOLD_MINUTES,
    anomaly_detection,
    check_data_freshness,
    monitor_pipeline,
    send_alert_email,
)


# =============================================================================
# SHARED FIXTURES
# =============================================================================

@pytest.fixture
def mock_db_connection():
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)

    return mock_conn, mock_cursor


@pytest.fixture
def city_mapping():
    return {"Toronto": 1, "Montreal": 2, "Vancouver": 3}


# =============================================================================
# check_data_freshness
# =============================================================================

class TestCheckDataFreshness:

    @patch("monitor.psycopg2.connect")
    def test_returns_minutes_since_last_update(self, mock_connect, mock_db_connection, city_mapping):
        """Fresh city returns a positive float close to the actual age."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        forty_mins_ago = datetime.now(timezone.utc) - timedelta(minutes=40)
        mock_cursor.fetchall.return_value = [(1, forty_mins_ago)]

        result = check_data_freshness(city_mapping, "current")

        assert "Toronto" in result
        assert 39 < result["Toronto"] < 41

    @patch("monitor.psycopg2.connect")
    def test_normalizes_naive_datetime(self, mock_connect, mock_db_connection, city_mapping):
        """psycopg2 may return naive datetimes; the function should not raise."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        # Naive datetime (no tzinfo) — strip tzinfo to simulate psycopg2 behaviour
        naive_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(tzinfo=None)
        assert naive_ts.tzinfo is None
        mock_cursor.fetchall.return_value = [(1, naive_ts)]

        result = check_data_freshness(city_mapping, "current")

        assert "Toronto" in result
        assert 29 < result["Toronto"] < 31

    @patch("monitor.psycopg2.connect")
    def test_omits_city_not_in_mapping(self, mock_connect, mock_db_connection, city_mapping):
        """City IDs absent from city_mapping are silently skipped."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        # city_id=99 does not exist in the fixture mapping
        mock_cursor.fetchall.return_value = [(99, datetime.now(timezone.utc))]

        result = check_data_freshness(city_mapping, "current")

        assert result == {}

    @patch("monitor.psycopg2.connect")
    def test_omits_city_with_null_timestamp(self, mock_connect, mock_db_connection, city_mapping):
        """Cities that have rows but a NULL max timestamp are skipped."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        mock_cursor.fetchall.return_value = [(1, None)]

        result = check_data_freshness(city_mapping, "current")

        assert result == {}

    @patch("monitor.psycopg2.connect")
    def test_returns_empty_dict_on_db_error(self, mock_connect, city_mapping):
        """DB failures return an empty dict rather than raising."""
        mock_connect.side_effect = psycopg2.Error("connection refused")

        result = check_data_freshness(city_mapping, "current")

        assert result == {}

    @patch("monitor.psycopg2.connect")
    def test_queries_weather_history_for_current(self, mock_connect, mock_db_connection, city_mapping):
        """Correct table is queried for data_type='current'."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_cursor.fetchall.return_value = []

        check_data_freshness(city_mapping, "current")

        sql = mock_cursor.execute.call_args[0][0]
        assert "weather_history" in sql

    @patch("monitor.psycopg2.connect")
    def test_queries_weather_forecast_for_forecast(self, mock_connect, mock_db_connection, city_mapping):
        """Correct table is queried for data_type='forecast'."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_cursor.fetchall.return_value = []

        check_data_freshness(city_mapping, "forecast")

        sql = mock_cursor.execute.call_args[0][0]
        assert "weather_forecast" in sql


# =============================================================================
# anomaly_detection
# =============================================================================

class TestAnomalyDetection:

    @patch("monitor.psycopg2.connect")
    def test_returns_current_and_baseline_counts(self, mock_connect, mock_db_connection, city_mapping):
        """Returns dict with correct current and baseline values."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        # First fetchall = current window, second = 7-day totals
        # baseline_windows for current = (7*24)//3 = 56
        mock_cursor.fetchall.side_effect = [
            [(1, 3)],    # current window: Toronto has 3 rows
            [(1, 112)],  # 7-day total: 112 rows / 56 windows = 2.0 baseline
        ]

        result = anomaly_detection(city_mapping, "current")

        assert result["Toronto"]["current"] == 3
        assert result["Toronto"]["baseline"] == pytest.approx(2.0)

    @patch("monitor.psycopg2.connect")
    def test_absent_city_has_zero_current(self, mock_connect, mock_db_connection, city_mapping):
        """City present in mapping but absent from DB gets current=0."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        mock_cursor.fetchall.side_effect = [
            [],  # nothing in current window
            [],  # nothing in 7-day baseline
        ]

        result = anomaly_detection(city_mapping, "current")

        assert result["Toronto"]["current"] == 0
        assert result["Toronto"]["baseline"] == pytest.approx(0.0)

    @patch("monitor.psycopg2.connect")
    def test_uses_correct_window_hours_for_forecast(self, mock_connect, mock_db_connection, city_mapping):
        """Forecast window is 9 hours; baseline_windows = (7*24)//9 = 18."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn

        # 18 total rows over 7 days → baseline = 18/18 = 1.0 per window
        mock_cursor.fetchall.side_effect = [
            [(1, 2)],   # current window
            [(1, 18)],  # 7-day total
        ]

        result = anomaly_detection(city_mapping, "forecast")

        assert result["Toronto"]["current"] == 2
        assert result["Toronto"]["baseline"] == pytest.approx(1.0)

    @patch("monitor.psycopg2.connect")
    def test_returns_empty_dict_on_db_error(self, mock_connect, city_mapping):
        """DB failures return an empty dict rather than raising."""
        mock_connect.side_effect = psycopg2.Error("timeout")

        result = anomaly_detection(city_mapping, "current")

        assert result == {}

    @patch("monitor.psycopg2.connect")
    def test_queries_correct_table_for_current(self, mock_connect, mock_db_connection, city_mapping):
        """weather_history is queried for data_type='current'."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_cursor.fetchall.side_effect = [[], []]

        anomaly_detection(city_mapping, "current")

        for c in mock_cursor.execute.call_args_list:
            assert "weather_history" in c[0][0]

    @patch("monitor.psycopg2.connect")
    def test_queries_correct_table_for_forecast(self, mock_connect, mock_db_connection, city_mapping):
        """weather_forecast is queried for data_type='forecast'."""
        mock_conn, mock_cursor = mock_db_connection
        mock_connect.return_value = mock_conn
        mock_cursor.fetchall.side_effect = [[], []]

        anomaly_detection(city_mapping, "forecast")

        for c in mock_cursor.execute.call_args_list:
            assert "weather_forecast" in c[0][0]


# =============================================================================
# monitor_pipeline
# =============================================================================

class TestMonitorPipeline:

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_returns_empty_list_when_all_healthy(self, mock_mapping, mock_freshness, mock_anomaly):
        """No warnings returned when data is fresh and counts are normal."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {"Toronto": 30.0}   # 30 min old — well within threshold
        mock_anomaly.return_value = {"Toronto": {"current": 3, "baseline": 3.0}}

        result = monitor_pipeline(["current"])

        assert result == []

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_warns_when_city_is_stale(self, mock_mapping, mock_freshness, mock_anomaly):
        """Returns a warning when a city's data is older than the threshold."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {"Toronto": FRESHNESS_THRESHOLD_MINUTES + 60}
        mock_anomaly.return_value = {"Toronto": {"current": 3, "baseline": 3.0}}

        warnings = monitor_pipeline(["current"])

        assert len(warnings) == 1
        assert "Toronto" in warnings[0]
        assert "stale" in warnings[0]

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_warns_when_city_has_zero_rows(self, mock_mapping, mock_freshness, mock_anomaly):
        """Returns a warning when the current window has zero rows."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {"Toronto": 30.0}
        mock_anomaly.return_value = {"Toronto": {"current": 0, "baseline": 3.0}}

        warnings = monitor_pipeline(["current"])

        assert len(warnings) == 1
        assert "0 rows" in warnings[0]

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_warns_when_rows_exceed_10x_baseline(self, mock_mapping, mock_freshness, mock_anomaly):
        """Returns a warning when current window rows are >10x the rolling baseline."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {"Toronto": 30.0}
        mock_anomaly.return_value = {"Toronto": {"current": 100, "baseline": 3.0}}

        warnings = monitor_pipeline(["current"])

        assert len(warnings) == 1
        assert "anomaly" in warnings[0]

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_warns_when_no_freshness_data(self, mock_mapping, mock_freshness, mock_anomaly):
        """Returns a warning when freshness check returns an empty dict."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {}
        mock_anomaly.return_value = {}

        warnings = monitor_pipeline(["current"])

        assert len(warnings) == 1
        assert "No current data" in warnings[0]

    @patch("monitor.anomaly_detection")
    @patch("monitor.check_data_freshness")
    @patch("monitor.utils._get_city_mapping")
    def test_runs_checks_for_each_data_type(self, mock_mapping, mock_freshness, mock_anomaly):
        """check_data_freshness and anomaly_detection are called once per data_type."""
        mock_mapping.return_value = {"Toronto": 1}
        mock_freshness.return_value = {"Toronto": 30.0}
        mock_anomaly.return_value = {"Toronto": {"current": 3, "baseline": 3.0}}

        monitor_pipeline(["current", "forecast"])

        assert mock_freshness.call_count == 2
        assert mock_anomaly.call_count == 2
        called_types = [c[0][1] for c in mock_freshness.call_args_list]
        assert "current" in called_types
        assert "forecast" in called_types


# =============================================================================
# send_alert_email
# =============================================================================

class TestSendAlertEmail:

    def test_skips_when_gmail_user_not_set(self, caplog, monkeypatch):
        """Email is skipped with a warning when GMAIL_USER is absent."""
        monkeypatch.delenv("GMAIL_USER", raising=False)
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        import logging
        with caplog.at_level(logging.WARNING, logger="monitor"):
            send_alert_email(["some warning"])

        assert "skipped" in caplog.text.lower()

    def test_skips_when_app_password_not_set(self, caplog, monkeypatch):
        """Email is skipped with a warning when GMAIL_APP_PASSWORD is absent."""
        monkeypatch.setenv("GMAIL_USER", "test@gmail.com")
        monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

        import logging
        with caplog.at_level(logging.WARNING, logger="monitor"):
            send_alert_email(["some warning"])

        assert "skipped" in caplog.text.lower()

    @patch("monitor.smtplib.SMTP_SSL")
    def test_sends_email_with_correct_recipient(self, mock_smtp_cls, monkeypatch):
        """Email is sent to ALERT_EMAIL_TO when set."""
        monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
        monkeypatch.setenv("ALERT_EMAIL_TO", "recipient@example.com")

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_alert_email(["city X is stale"])

        mock_server.login.assert_called_once_with("sender@gmail.com", "app-password")
        mock_server.send_message.assert_called_once()

    @patch("monitor.smtplib.SMTP_SSL")
    def test_subject_contains_warning_count(self, mock_smtp_cls, monkeypatch):
        """Email subject includes the number of warnings."""
        monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_alert_email(["warn 1", "warn 2", "warn 3"])

        sent_msg = mock_server.send_message.call_args[0][0]
        assert "3" in sent_msg["Subject"]

    @patch("monitor.smtplib.SMTP_SSL")
    def test_defaults_recipient_to_sender(self, mock_smtp_cls, monkeypatch):
        """When ALERT_EMAIL_TO is unset, email is sent to GMAIL_USER."""
        monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_alert_email(["a warning"])

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["To"] == "sender@gmail.com"

    @patch("monitor.smtplib.SMTP_SSL")
    def test_empty_recipient_env_falls_back_to_sender(self, mock_smtp_cls, monkeypatch):
        """An empty ALERT_EMAIL_TO (as injected by an undefined GHA secret) falls back to GMAIL_USER."""
        monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
        monkeypatch.setenv("ALERT_EMAIL_TO", "")  # set-but-empty, not absent

        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_alert_email(["a warning"])

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg["To"] == "sender@gmail.com"

    @patch("monitor.smtplib.SMTP_SSL")
    def test_logs_error_on_smtp_exception(self, mock_smtp_cls, caplog, monkeypatch):
        """SMTPException is caught and logged — does not propagate."""
        monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
        monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
        monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)

        mock_server = MagicMock()
        mock_server.send_message.side_effect = smtplib.SMTPException("auth failed")
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        import logging
        with caplog.at_level(logging.ERROR, logger="monitor"):
            send_alert_email(["a warning"])  # must not raise

        assert "auth failed" in caplog.text
