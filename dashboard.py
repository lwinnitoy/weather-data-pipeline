"""Static dashboard generation for portfolio hosting."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
import textwrap
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import psycopg2

import config

FRESHNESS_THRESHOLD_MINUTES = 120
DEFAULT_OUTPUT_PATH = Path("docs/index.html")


@dataclass(frozen=True)
class SummaryMetrics:
		data_type: str
		rows: int
		tracked_cities: int
		latest_ts: Optional[datetime]
		stale_cities: int


@dataclass(frozen=True)
class CityStatus:
		city_id: int
		city_name: str
		current_rows: int
		current_latest: Optional[datetime]
		forecast_rows: int
		forecast_latest: Optional[datetime]


@dataclass(frozen=True)
class DailyCount:
		data_type: str
		city_name: str
		day: date
		rows: int


@dataclass(frozen=True)
class DashboardSnapshot:
		generated_at: datetime
		current_summary: SummaryMetrics
		forecast_summary: SummaryMetrics
		city_statuses: List[CityStatus]
		daily_current: List[DailyCount]
		daily_forecast: List[DailyCount]


def _connect():
		return psycopg2.connect(**config.DATABASE)


def _run_one(cursor, sql: str, params: Sequence[object] | None = None):
		cursor.execute(sql, params or ())
		return cursor.fetchone()


def _run_all(cursor, sql: str, params: Sequence[object] | None = None):
		cursor.execute(sql, params or ())
		return cursor.fetchall()


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
		"""Normalize a datetime to UTC-aware, handling naive datetimes from psycopg2."""
		if dt is None:
				return None
		if dt.tzinfo is None:
				return dt.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc)


def _count_stale(latest_ts: Optional[datetime], now: datetime, threshold_minutes: int) -> bool:
		if latest_ts is None:
				return True
		age_minutes = (now - _ensure_utc(latest_ts)).total_seconds() / 60
		return age_minutes > threshold_minutes


def build_dashboard_snapshot(days: int = 7, threshold_minutes: int = FRESHNESS_THRESHOLD_MINUTES) -> DashboardSnapshot:
		"""Query the warehouse and assemble the dashboard data model."""

		generated_at = datetime.now(timezone.utc)
		cutoff = generated_at - timedelta(days=days)

		current_summary_sql = """
				SELECT COUNT(*) AS rows, COUNT(DISTINCT city_id) AS tracked_cities, MAX(timestamp_utc) AS latest_ts
				FROM weather_history
		"""
		forecast_summary_sql = """
				SELECT COUNT(*) AS rows, COUNT(DISTINCT city_id) AS tracked_cities, MAX(timestamp_utc) AS latest_ts
				FROM weather_forecast
		"""
		city_status_sql = """
				SELECT
						c.id AS city_id,
						c.city AS city_name,
						COALESCE(ch.rows, 0) AS current_rows,
						ch.latest_ts AS current_latest,
						COALESCE(fh.rows, 0) AS forecast_rows,
						fh.latest_ts AS forecast_latest
				FROM cities c
				LEFT JOIN (
						SELECT city_id, COUNT(*) AS rows, MAX(timestamp_utc) AS latest_ts
						FROM weather_history
						GROUP BY city_id
				) ch ON ch.city_id = c.id
				LEFT JOIN (
						SELECT city_id, COUNT(*) AS rows, MAX(timestamp_utc) AS latest_ts
						FROM weather_forecast
						GROUP BY city_id
				) fh ON fh.city_id = c.id
				ORDER BY c.city
		"""
		daily_current_sql = """
				SELECT c.city AS city_name, DATE(w.timestamp_utc AT TIME ZONE 'UTC') AS day, COUNT(*) AS rows
				FROM weather_history w
				JOIN cities c ON c.id = w.city_id
				WHERE w.timestamp_utc >= %s
				GROUP BY c.city, DATE(w.timestamp_utc AT TIME ZONE 'UTC')
				ORDER BY day DESC, city_name
		"""
		daily_forecast_sql = """
				SELECT c.city AS city_name, DATE(w.timestamp_utc AT TIME ZONE 'UTC') AS day, COUNT(*) AS rows
				FROM weather_forecast w
				JOIN cities c ON c.id = w.city_id
				WHERE w.timestamp_utc >= %s
				GROUP BY c.city, DATE(w.timestamp_utc AT TIME ZONE 'UTC')
				ORDER BY day DESC, city_name
		"""

		with _connect() as conn:
				with conn.cursor() as cursor:
						current_rows, current_cities, current_latest = _run_one(cursor, current_summary_sql)
						forecast_rows, forecast_cities, forecast_latest = _run_one(cursor, forecast_summary_sql)
						city_rows = _run_all(cursor, city_status_sql)
						daily_current_rows = _run_all(cursor, daily_current_sql, (cutoff,))
						daily_forecast_rows = _run_all(cursor, daily_forecast_sql, (cutoff,))

		city_statuses = [
				CityStatus(
						city_id=row[0],
						city_name=row[1],
						current_rows=row[2],
						current_latest=row[3],
						forecast_rows=row[4],
						forecast_latest=row[5],
				)
				for row in city_rows
		]

		current_stale = sum(1 for status in city_statuses if _count_stale(status.current_latest, generated_at, threshold_minutes))
		forecast_stale = sum(1 for status in city_statuses if _count_stale(status.forecast_latest, generated_at, threshold_minutes))

		daily_current = [DailyCount("current", row[0], row[1], row[2]) for row in daily_current_rows]
		daily_forecast = [DailyCount("forecast", row[0], row[1], row[2]) for row in daily_forecast_rows]

		return DashboardSnapshot(
				generated_at=generated_at,
				current_summary=SummaryMetrics(
						data_type="current",
						rows=current_rows,
						tracked_cities=current_cities,
						latest_ts=current_latest,
						stale_cities=current_stale,
				),
				forecast_summary=SummaryMetrics(
						data_type="forecast",
						rows=forecast_rows,
						tracked_cities=forecast_cities,
						latest_ts=forecast_latest,
						stale_cities=forecast_stale,
				),
				city_statuses=city_statuses,
				daily_current=daily_current,
				daily_forecast=daily_forecast,
		)


def _format_timestamp(value: Optional[datetime]) -> str:
		if value is None:
				return "No data"
		return _ensure_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _format_age(value: Optional[datetime], now: datetime) -> str:
		if value is None:
				return "No data"
		age_minutes = int((now - _ensure_utc(value)).total_seconds() // 60)
		if age_minutes < 1:
				return "just now"
		if age_minutes < 60:
				return f"{age_minutes} min"
		hours, minutes = divmod(age_minutes, 60)
		return f"{hours}h {minutes}m"


def _render_stat_card(label: str, value: object, detail: str) -> str:
		return f"""
		<article class="card stat-card">
			<div class="card-label">{escape(label)}</div>
			<div class="card-value">{escape(str(value))}</div>
			<div class="card-detail">{escape(detail)}</div>
		</article>
		"""


def _render_city_rows(snapshot: DashboardSnapshot) -> str:
		now = snapshot.generated_at
		ordered = sorted(
				snapshot.city_statuses,
				key=lambda item: (item.current_rows + item.forecast_rows, item.city_name.lower()),
				reverse=True,
		)
		rows = []
		for status in ordered:
				current_age = _format_age(status.current_latest, now)
				forecast_age = _format_age(status.forecast_latest, now)
				rows.append(
						f"""
						<tr>
							<td>{escape(status.city_name)}</td>
							<td>{status.current_rows}</td>
							<td>{escape(_format_timestamp(status.current_latest))}</td>
							<td>{escape(current_age)}</td>
							<td>{status.forecast_rows}</td>
							<td>{escape(_format_timestamp(status.forecast_latest))}</td>
							<td>{escape(forecast_age)}</td>
						</tr>
						"""
				)
		return "\n".join(rows)


def _render_daily_rows(rows: Iterable[DailyCount]) -> str:
		html_rows = []
		for row in rows:
				html_rows.append(
						f"""
						<tr>
							<td>{escape(row.city_name)}</td>
							<td>{escape(row.day.isoformat())}</td>
							<td>{row.rows}</td>
						</tr>
						"""
				)
		return "\n".join(html_rows)


def _render_bar_list(snapshot: DashboardSnapshot, data_type: str) -> str:
		statuses = snapshot.city_statuses
		if data_type == "current":
				values = [(status.city_name, status.current_rows) for status in statuses]
		else:
				values = [(status.city_name, status.forecast_rows) for status in statuses]

		max_value = max((value for _, value in values), default=0)
		items = []
		for label, value in sorted(values, key=lambda item: item[1], reverse=True)[:8]:
				width = 0 if max_value == 0 else max(8, int((value / max_value) * 100))
				items.append(
						f"""
						<div class="bar-row">
							<div class="bar-label">{escape(label)}</div>
							<div class="bar-track"><div class="bar-fill" style="width: {width}%"></div></div>
							<div class="bar-value">{value}</div>
						</div>
						"""
				)
		return "\n".join(items)


def render_dashboard_html(snapshot: DashboardSnapshot, title: str = "Weather Data Pipeline Dashboard") -> str:
		"""Render a standalone HTML dashboard page."""

		summary_cards = "".join(
				[
						_render_stat_card("Current rows", snapshot.current_summary.rows, f"{snapshot.current_summary.stale_cities} stale cities"),
						_render_stat_card("Forecast rows", snapshot.forecast_summary.rows, f"{snapshot.forecast_summary.stale_cities} stale cities"),
						_render_stat_card("Cities tracked", len(snapshot.city_statuses), f"Current: {snapshot.current_summary.tracked_cities} / Forecast: {snapshot.forecast_summary.tracked_cities}"),
						_render_stat_card("Latest current refresh", _format_timestamp(snapshot.current_summary.latest_ts), "Warehouse snapshot"),
						_render_stat_card("Latest forecast refresh", _format_timestamp(snapshot.forecast_summary.latest_ts), "Warehouse snapshot"),
						_render_stat_card("Generated at", _format_timestamp(snapshot.generated_at), f"Rolling window: last 7 days"),
				]
		)

		html = textwrap.dedent(f"""
		<!doctype html>
		<html lang="en">
			<head>
				<meta charset="utf-8" />
				<meta name="viewport" content="width=device-width, initial-scale=1" />
				<title>{escape(title)}</title>
				<style>
					:root {{
						--bg: #07111f;
						--bg-soft: #0d1a2f;
						--panel: rgba(15, 28, 48, 0.92);
						--panel-border: rgba(140, 168, 208, 0.18);
						--text: #e8eef9;
						--muted: #97aac4;
						--accent: #62d0ff;
						--accent-2: #8be1bb;
						--accent-3: #ffbc73;
						--shadow: 0 24px 60px rgba(2, 8, 20, 0.45);
					}}

					* {{ box-sizing: border-box; }}
					body {{
						margin: 0;
						font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
						color: var(--text);
						background:
							radial-gradient(circle at top left, rgba(98, 208, 255, 0.16), transparent 30%),
							radial-gradient(circle at top right, rgba(139, 225, 187, 0.12), transparent 26%),
							linear-gradient(180deg, #081120 0%, #07111f 55%, #050b14 100%);
					}}

					.page {{ max-width: 1320px; margin: 0 auto; padding: 40px 24px 56px; }}
					.hero {{
						padding: 32px;
						border: 1px solid var(--panel-border);
						border-radius: 28px;
						background: linear-gradient(160deg, rgba(15, 28, 48, 0.96), rgba(9, 19, 35, 0.92));
						box-shadow: var(--shadow);
						position: relative;
						overflow: hidden;
					}}
					.hero::after {{
						content: "";
						position: absolute;
						inset: auto -8% -40% auto;
						width: 280px;
						height: 280px;
						border-radius: 999px;
						background: radial-gradient(circle, rgba(98, 208, 255, 0.22), transparent 68%);
					}}
					.eyebrow {{
						text-transform: uppercase;
						letter-spacing: 0.18em;
						color: var(--accent);
						font-size: 0.78rem;
						margin-bottom: 10px;
					}}
					h1 {{ margin: 0; font-size: clamp(2rem, 4vw, 3.4rem); line-height: 1.02; max-width: 11ch; }}
					.hero-copy {{ max-width: 760px; color: var(--muted); margin: 16px 0 0; font-size: 1.02rem; line-height: 1.65; }}

					.grid {{ display: grid; gap: 16px; }}
					.stats {{
						grid-template-columns: repeat(6, minmax(0, 1fr));
						margin-top: 18px;
					}}
					.content {{ margin-top: 20px; grid-template-columns: 1.8fr 1fr; align-items: start; }}
					.content-secondary {{ margin-top: 20px; grid-template-columns: 1fr 1fr; }}
					.card {{
						border: 1px solid var(--panel-border);
						background: var(--panel);
						border-radius: 22px;
						box-shadow: var(--shadow);
					}}
					.stat-card {{ padding: 18px 18px 16px; min-height: 122px; }}
					.card-label {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.12em; }}
					.card-value {{ font-size: clamp(1.35rem, 2.3vw, 2rem); font-weight: 700; margin-top: 10px; }}
					.card-detail {{ color: var(--muted); margin-top: 8px; font-size: 0.92rem; line-height: 1.5; }}

					.panel {{ padding: 20px; }}
					.panel h2 {{ margin: 0 0 16px; font-size: 1.1rem; }}
					.panel p {{ margin: 0 0 16px; color: var(--muted); line-height: 1.6; }}

					.bars {{ display: grid; gap: 12px; }}
					.bar-row {{ display: grid; grid-template-columns: 1.1fr 2fr auto; gap: 12px; align-items: center; }}
					.bar-label, .bar-value {{ font-size: 0.92rem; color: var(--text); }}
					.bar-track {{ height: 12px; background: rgba(151, 170, 196, 0.12); border-radius: 999px; overflow: hidden; }}
					.bar-fill {{ height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}

					.table-wrap {{ overflow-x: auto; }}
					table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
					th, td {{ padding: 12px 10px; text-align: left; border-bottom: 1px solid rgba(151, 170, 196, 0.14); }}
					th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
					td {{ font-size: 0.95rem; }}
					tr:hover td {{ background: rgba(98, 208, 255, 0.04); }}

					.section {{ margin-top: 18px; }}
					.section-title {{ margin: 0 0 12px; font-size: 1.05rem; }}
					.section-subtitle {{ margin: 0 0 14px; color: var(--muted); font-size: 0.94rem; line-height: 1.6; }}

					.footer {{ color: var(--muted); margin-top: 20px; font-size: 0.9rem; }}

					@media (max-width: 1080px) {{
						.stats, .content, .content-secondary {{ grid-template-columns: 1fr 1fr; }}
					}}
					@media (max-width: 720px) {{
						.page {{ padding: 20px 14px 40px; }}
						.hero {{ padding: 22px; border-radius: 22px; }}
						.stats, .content, .content-secondary {{ grid-template-columns: 1fr; }}
						.bar-row {{ grid-template-columns: 1fr; gap: 8px; }}
					}}
				</style>
			</head>
			<body>
				<main class="page">
					<section class="hero">
						<div class="eyebrow">Portfolio dashboard</div>
						<h1>{escape(title)}</h1>
						<p class="hero-copy">
							A static snapshot of the weather pipeline warehouse. This page is designed to publish cleanly
							on GitHub Pages or Cloudflare Pages without any server-side runtime.
						</p>
						<div class="grid stats">
							{summary_cards}
						</div>
					</section>

					<section class="grid content">
						<article class="card panel">
							<h2>Freshness by city</h2>
							<p>Current and forecast rows grouped by city, with the latest refresh time and lag from generation time.</p>
							<div class="table-wrap">
								<table>
									<thead>
										<tr>
											<th>City</th>
											<th>Current rows</th>
											<th>Current latest</th>
											<th>Current age</th>
											<th>Forecast rows</th>
											<th>Forecast latest</th>
											<th>Forecast age</th>
										</tr>
									</thead>
									<tbody>
										{_render_city_rows(snapshot)}
									</tbody>
								</table>
							</div>
						</article>

						<article class="card panel">
							<h2>Top cities by volume</h2>
							<p>These bars highlight where the largest concentration of records is landing right now.</p>
							<div class="bars">
								{_render_bar_list(snapshot, "current")}
							</div>
						</article>
					</section>

					<section class="grid content-secondary section">
						<article class="card panel">
							<h2 class="section-title">Current rows by day</h2>
							<p class="section-subtitle">Useful for showing row growth and backfill behaviour in the portfolio version of the dashboard.</p>
							<div class="table-wrap">
								<table>
									<thead>
										<tr>
											<th>City</th>
											<th>Day</th>
											<th>Rows</th>
										</tr>
									</thead>
									<tbody>
										{_render_daily_rows(snapshot.daily_current)}
									</tbody>
								</table>
							</div>
						</article>

						<article class="card panel">
							<h2 class="section-title">Forecast rows by day</h2>
							<p class="section-subtitle">Forecast output has a different cadence, so this table makes the 3-hour rhythm visible.</p>
							<div class="table-wrap">
								<table>
									<thead>
										<tr>
											<th>City</th>
											<th>Day</th>
											<th>Rows</th>
										</tr>
									</thead>
									<tbody>
										{_render_daily_rows(snapshot.daily_forecast)}
									</tbody>
								</table>
							</div>
						</article>
					</section>

					<div class="footer">
						Generated from PostgreSQL warehouse data at {_format_timestamp(snapshot.generated_at)}.
					</div>
				</main>
			</body>
		</html>
		""").lstrip()
		return html


def write_dashboard_html(output_path: Path | str = DEFAULT_OUTPUT_PATH, days: int = 7, threshold_minutes: int = FRESHNESS_THRESHOLD_MINUTES) -> Path:
		"""Build a snapshot and write the dashboard HTML to disk."""

		snapshot = build_dashboard_snapshot(days=days, threshold_minutes=threshold_minutes)
		html = render_dashboard_html(snapshot)
		output = Path(output_path)
		output.parent.mkdir(parents=True, exist_ok=True)
		output.write_text(html, encoding="utf-8")
		return output


def main(argv: Optional[Sequence[str]] = None) -> int:
		parser = argparse.ArgumentParser(description="Generate a static weather pipeline dashboard.")
		parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output HTML file path.")
		parser.add_argument("--days", type=int, default=7, help="Rolling window for daily row counts.")
		parser.add_argument(
				"--threshold-minutes",
				type=int,
				default=FRESHNESS_THRESHOLD_MINUTES,
				help="Freshness threshold used to mark stale cities.",
		)
		args = parser.parse_args(argv)

		output = write_dashboard_html(args.output, days=args.days, threshold_minutes=args.threshold_minutes)
		print(f"Wrote dashboard to {output}")
		return 0


if __name__ == "__main__":
		raise SystemExit(main())
