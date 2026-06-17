"""Monitoring utilities for the weather data pipeline.

Provides freshness checks (how stale is each city's data?) and anomaly
detection (is the row count in the current window abnormal?). Sends a
Gmail summary when issues are found.

Usage:
    python monitor.py [--data-types current forecast]

Required env vars for email alerts:
    GMAIL_USER          sender address (e.g. you@gmail.com)
    GMAIL_APP_PASSWORD  Gmail App Password (not your login password)
    ALERT_EMAIL_TO      recipient address (defaults to GMAIL_USER)
"""
import argparse
import datetime
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2

import config
import utils

logger = logging.getLogger(__name__)

FRESHNESS_THRESHOLD_MINUTES = 120  # alert if last record is older than 2 hours

# How many hours to treat as the "current" window for anomaly detection.
# Sized to 3x pipeline frequency so minor delays don't false-alarm.
_WINDOW_HOURS = {
    "current": 3,   # pipeline runs hourly; 3 h = ~3 expected rows per city
    "forecast": 9,  # pipeline runs every 3 h; 9 h = ~3 expected batches
}

BASELINE_DAYS = 7          # rolling window used to compute expected row counts
ANOMALY_MAX_RATIO = 10.0   # alert if current window > 10x rolling baseline


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def monitor_pipeline(data_types) -> list:
    """Run freshness and anomaly checks for each data type.

    Logs results and returns a flat list of warning strings for any issues
    found. An empty list means everything looks healthy.
    """
    warnings = []
    city_mapping = utils._get_city_mapping()

    for data_type in data_types:
        logger.info("=== Monitoring %s ===", data_type)

        # --- Freshness ---
        freshness = check_data_freshness(city_mapping, data_type)
        if not freshness:
            msg = f"No {data_type} data found in DB — cannot assess freshness"
            logger.warning(msg)
            warnings.append(msg)
        for city_name, minutes_old in freshness.items():
            if minutes_old > FRESHNESS_THRESHOLD_MINUTES:
                overdue = minutes_old - FRESHNESS_THRESHOLD_MINUTES
                msg = (
                    f"{data_type} data for {city_name} is stale: "
                    f"{minutes_old:.1f} min since last update ({overdue:.1f} min overdue)"
                )
                logger.warning(msg)
                warnings.append(msg)
            else:
                logger.info(
                    "%s freshness OK for %s: %.1f min old", data_type, city_name, minutes_old
                )

        # --- Anomalies ---
        anomalies = anomaly_detection(city_mapping, data_type)
        for city_name, counts in anomalies.items():
            current = counts["current"]
            baseline = counts["baseline"]
            if current == 0:
                msg = (
                    f"{data_type} anomaly for {city_name}: "
                    f"0 rows in current window (baseline={baseline:.1f})"
                )
                logger.warning(msg)
                warnings.append(msg)
            elif baseline > 0 and current > ANOMALY_MAX_RATIO * baseline:
                msg = (
                    f"{data_type} anomaly for {city_name}: "
                    f"{current} rows in current window "
                    f"({current / baseline:.1f}x baseline of {baseline:.1f})"
                )
                logger.warning(msg)
                warnings.append(msg)
            else:
                logger.info(
                    "%s row count OK for %s: %d rows (baseline=%.1f)",
                    data_type, city_name, current, baseline,
                )

    return warnings


def check_data_freshness(city_mapping: dict, data_type: str) -> dict:
    """Return minutes since last record per city.

    Args:
        city_mapping: {city_name: city_id} from utils._get_city_mapping()
        data_type: "current" or "forecast"

    Returns:
        {city_name: minutes_since_last_update} for cities that have data.
        Cities with no rows are omitted (not stale, just absent).
    """
    table = "weather_history" if data_type == "current" else "weather_forecast"
    id_to_name = {v: k for k, v in city_mapping.items()}

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT city_id, MAX(timestamp_utc) FROM {table} GROUP BY city_id"
                )
                rows = cursor.fetchall()
    except psycopg2.Error as e:
        logger.error("DB error checking %s freshness: %s", data_type, e)
        return {}

    now = datetime.datetime.now(datetime.timezone.utc)
    result = {}
    for city_id, max_ts in rows:
        city_name = id_to_name.get(city_id)
        if city_name is None or max_ts is None:
            continue
        # psycopg2 may return a naive datetime from Postgres — normalize to UTC
        if max_ts.tzinfo is None:
            max_ts = max_ts.replace(tzinfo=datetime.timezone.utc)
        result[city_name] = (now - max_ts).total_seconds() / 60

    return result


def anomaly_detection(city_mapping: dict, data_type: str) -> dict:
    """Compare per-city row counts in the current window vs. a 7-day rolling baseline.

    Baseline is the average rows-per-window over the past BASELINE_DAYS days,
    using the same window size as the current check.

    Args:
        city_mapping: {city_name: city_id}
        data_type: "current" or "forecast"

    Returns:
        {city_name: {"current": int, "baseline": float}} for all known cities.
        Cities absent from the DB have current=0.
    """
    table = "weather_history" if data_type == "current" else "weather_forecast"
    window_hours = _WINDOW_HOURS[data_type]
    # Number of non-overlapping windows in the baseline period
    baseline_windows = (BASELINE_DAYS * 24) // window_hours

    try:
        with psycopg2.connect(**config.DATABASE) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT city_id, COUNT(*)
                    FROM {table}
                    WHERE timestamp_utc > NOW() AT TIME ZONE 'UTC' - INTERVAL '{window_hours} HOURS'
                    GROUP BY city_id
                    """
                )
                current_counts = {row[0]: row[1] for row in cursor.fetchall()}

                cursor.execute(
                    f"""
                    SELECT city_id, COUNT(*)
                    FROM {table}
                    WHERE timestamp_utc > NOW() AT TIME ZONE 'UTC' - INTERVAL '{BASELINE_DAYS} DAYS'
                    GROUP BY city_id
                    """
                )
                baseline_totals = {
                    row[0]: row[1] / baseline_windows for row in cursor.fetchall()
                }

    except psycopg2.Error as e:
        logger.error("DB error in anomaly_detection for %s: %s", data_type, e)
        return {}

    id_to_name = {v: k for k, v in city_mapping.items()}
    return {
        city_name: {
            "current": current_counts.get(city_id, 0),
            "baseline": baseline_totals.get(city_id, 0.0),
        }
        for city_id, city_name in id_to_name.items()
    }


# ---------------------------------------------------------------------------
# Email alerting
# ---------------------------------------------------------------------------

def send_alert_email(warnings: list) -> None:
    """Send a Gmail summary of all monitoring warnings.

    Silently skips if GMAIL_USER or GMAIL_APP_PASSWORD are not set, so the
    monitor still runs cleanly in local dev without credentials configured.
    """
    gmail_user = os.getenv("GMAIL_USER")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")

    if not gmail_user or not gmail_password:
        logger.warning("Email alert skipped: GMAIL_USER or GMAIL_APP_PASSWORD not set")
        return

    recipient = os.getenv("ALERT_EMAIL_TO", gmail_user)
    run_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    subject = f"[Weather Pipeline] {len(warnings)} monitoring alert(s) — {run_time}"

    body_lines = [
        "The pipeline monitor detected the following issues:\n",
        *[f"  • {w}" for w in warnings],
        f"\nRun time: {run_time}",
        "Check the GitHub Actions logs for full details.",
    ]
    body = "\n".join(body_lines)

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_user, gmail_password)
            server.send_message(msg)
        logger.info("Alert email sent to %s", recipient)
    except smtplib.SMTPException as e:
        logger.error("Failed to send alert email: %s", e)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    parser = argparse.ArgumentParser(description="Monitor the weather data pipeline")
    parser.add_argument(
        "--data-types",
        nargs="+",
        default=["current", "forecast"],
        choices=["current", "forecast"],
        help="Data types to monitor (default: both)",
    )
    args = parser.parse_args()

    found_warnings = monitor_pipeline(args.data_types)
    if found_warnings:
        send_alert_email(found_warnings)
    else:
        logger.info("All checks passed — no alerts sent")
