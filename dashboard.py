"""Static dashboard generation for portfolio hosting.

Renders a single self-contained HTML file (no JS, no external assets) so it
can publish cleanly on Cloudflare Pages / GitHub Pages. Time-series charts are
drawn as inline SVG. All timestamps are displayed in Pacific time
(America/Vancouver) for a Victoria, BC audience.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
import textwrap
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

import psycopg2

import config

FRESHNESS_THRESHOLD_MINUTES = 120
DEFAULT_OUTPUT_PATH = Path("docs/index.html")

# Victoria, BC shares the America/Vancouver zone (PST/PDT). tzdata (in
# requirements.txt) guarantees this resolves on all platforms incl. CI.
DISPLAY_TZ = ZoneInfo("America/Vancouver")

# Chart series colors (kept in sync with the CSS accent variables).
COLOR_CURRENT = "#62d0ff"
COLOR_FORECAST = "#8be1bb"
COLOR_TEMP = "#ffbc73"


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
class DailyTotal:
    """Total rows landed per Pacific day, across all cities."""
    day: date
    current_rows: int
    forecast_rows: int


@dataclass(frozen=True)
class TempPoint:
    """Mean observed temperature across all cities for a Pacific day."""
    day: date
    avg_temp_c: float


@dataclass(frozen=True)
class DashboardSnapshot:
    generated_at: datetime
    current_summary: SummaryMetrics
    forecast_summary: SummaryMetrics
    city_statuses: List[CityStatus]
    daily_current: List[DailyCount]
    daily_forecast: List[DailyCount]
    # Newer fields default to empty so older callers/tests still construct cleanly.
    daily_totals: List[DailyTotal] = field(default_factory=list)
    temp_trend: List[TempPoint] = field(default_factory=list)


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


def _to_pacific(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert any datetime to Pacific time (America/Vancouver)."""
    utc = _ensure_utc(dt)
    return utc.astimezone(DISPLAY_TZ) if utc is not None else None


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
    # Day buckets are computed in Pacific time so "rows by day" lines up with the
    # local calendar day for a Victoria, BC viewer.
    daily_current_sql = """
        SELECT c.city AS city_name, DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver') AS day, COUNT(*) AS rows
        FROM weather_history w
        JOIN cities c ON c.id = w.city_id
        WHERE w.timestamp_utc >= %s
        GROUP BY c.city, DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver')
        ORDER BY day DESC, city_name
    """
    daily_forecast_sql = """
        SELECT c.city AS city_name, DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver') AS day, COUNT(*) AS rows
        FROM weather_forecast w
        JOIN cities c ON c.id = w.city_id
        WHERE w.timestamp_utc >= %s
        GROUP BY c.city, DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver')
        ORDER BY day DESC, city_name
    """
    temp_trend_sql = """
        SELECT DATE(timestamp_utc AT TIME ZONE 'America/Vancouver') AS day, AVG(temp_c) AS avg_temp
        FROM weather_history
        WHERE timestamp_utc >= %s AND temp_c IS NOT NULL
        GROUP BY DATE(timestamp_utc AT TIME ZONE 'America/Vancouver')
        ORDER BY day
    """

    with _connect() as conn:
        with conn.cursor() as cursor:
            current_rows, current_cities, current_latest = _run_one(cursor, current_summary_sql)
            forecast_rows, forecast_cities, forecast_latest = _run_one(cursor, forecast_summary_sql)
            city_rows = _run_all(cursor, city_status_sql)
            daily_current_rows = _run_all(cursor, daily_current_sql, (cutoff,))
            daily_forecast_rows = _run_all(cursor, daily_forecast_sql, (cutoff,))
            temp_rows = _run_all(cursor, temp_trend_sql, (cutoff,))

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
    daily_totals = _aggregate_daily_totals(daily_current, daily_forecast)
    temp_trend = [TempPoint(day=row[0], avg_temp_c=float(row[1])) for row in temp_rows if row[1] is not None]

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
        daily_totals=daily_totals,
        temp_trend=temp_trend,
    )


def _aggregate_daily_totals(
    daily_current: Iterable[DailyCount], daily_forecast: Iterable[DailyCount]
) -> List[DailyTotal]:
    """Collapse per-city daily counts into one current+forecast total per day."""
    totals: dict[date, List[int]] = {}
    for dc in daily_current:
        totals.setdefault(dc.day, [0, 0])[0] += dc.rows
    for df in daily_forecast:
        totals.setdefault(df.day, [0, 0])[1] += df.rows
    return [
        DailyTotal(day=day, current_rows=cur, forecast_rows=fc)
        for day, (cur, fc) in sorted(totals.items())
    ]


def _format_timestamp(value: Optional[datetime]) -> str:
    if value is None:
        return "No data"
    pac = _to_pacific(value)
    offset_hours = int(pac.utcoffset().total_seconds() / 3600)
    return pac.strftime(f"%Y-%m-%d %H:%M %Z (UTC{offset_hours:+d})")


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


def _format_short_day(d: date) -> str:
    return d.strftime("%b %d")


# =============================================================================
# INLINE SVG CHARTS
# Dependency-free time-series rendering so the page stays self-contained.
# =============================================================================

Series = Tuple[str, str, List[Tuple[date, float]]]  # (name, color, points)


def _svg_line_chart(
    series: Sequence[Series],
    *,
    width: int = 880,
    height: int = 300,
    start_at_zero: bool = False,
    value_suffix: str = "",
) -> str:
    """Render a multi-series line chart as inline SVG.

    Returns an empty-state message div when there are no points to plot.
    """
    non_empty = [s for s in series if s[2]]
    if not non_empty:
        return '<div class="chart-empty">No data yet — charts populate as the pipeline runs.</div>'

    all_days = sorted({d for _, _, pts in non_empty for d, _ in pts})
    day_index = {d: i for i, d in enumerate(all_days)}
    n = len(all_days)

    values = [v for _, _, pts in non_empty for _, v in pts]
    vmin, vmax = min(values), max(values)
    if start_at_zero:
        vmin = min(vmin, 0)
    if vmax == vmin:
        vmax = vmin + 1  # avoid divide-by-zero on flat data

    pad_l, pad_r, pad_t, pad_b = 52, 20, 18, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_of(d: date) -> float:
        if n == 1:
            return pad_l + plot_w / 2
        return pad_l + (day_index[d] / (n - 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + (1 - (v - vmin) / (vmax - vmin)) * plot_h

    baseline_y = y_of(vmin)

    # Horizontal gridlines + y-axis tick labels
    grid, ylabels = [], []
    ticks = 4
    for t in range(ticks + 1):
        v = vmin + (vmax - vmin) * t / ticks
        y = y_of(v)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid-line" />')
        ylabels.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis-label y">{v:.0f}{value_suffix}</text>')

    # X-axis labels: first, middle, last day
    xlabels = []
    for i in sorted({0, n // 2, n - 1}):
        d = all_days[i]
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        xlabels.append(
            f'<text x="{x_of(d):.1f}" y="{height - 10}" text-anchor="{anchor}" class="axis-label x">{escape(_format_short_day(d))}</text>'
        )

    # Series: faint area fill + line + point dots
    series_svg = []
    for _name, color, pts in non_empty:
        coords = [(x_of(d), y_of(v)) for d, v in sorted(pts, key=lambda p: p[0])]
        line_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        area = (
            f"M {coords[0][0]:.1f},{baseline_y:.1f} "
            + " ".join(f"L {x:.1f},{y:.1f}" for x, y in coords)
            + f" L {coords[-1][0]:.1f},{baseline_y:.1f} Z"
        )
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" />' for x, y in coords)
        series_svg.append(
            f'<path d="{area}" fill="{color}" opacity="0.12" />'
            f'<polyline points="{line_pts}" fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round" />{dots}'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" class="chart-svg" role="img">'
        + "".join(grid)
        + "".join(series_svg)
        + "".join(ylabels)
        + "".join(xlabels)
        + "</svg>"
    )


def _chart_legend(series: Sequence[Series]) -> str:
    chips = [
        f'<span class="legend-item"><span class="legend-swatch" style="background:{color}"></span>{escape(name)}</span>'
        for name, color, _ in series
    ]
    return f'<div class="legend">{"".join(chips)}</div>'


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
        rows.append(
            f"""
            <tr>
                <td>{escape(status.city_name)}</td>
                <td>{status.current_rows}</td>
                <td>{escape(_format_timestamp(status.current_latest))}</td>
                <td>{escape(_format_age(status.current_latest, now))}</td>
                <td>{status.forecast_rows}</td>
                <td>{escape(_format_timestamp(status.forecast_latest))}</td>
                <td>{escape(_format_age(status.forecast_latest, now))}</td>
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
            _render_stat_card("Generated at", _format_timestamp(snapshot.generated_at), "Rolling window: last 7 days"),
        ]
    )

    volume_series: List[Series] = [
        ("Current", COLOR_CURRENT, [(t.day, float(t.current_rows)) for t in snapshot.daily_totals if t.current_rows > 0]),
        ("Forecast", COLOR_FORECAST, [(t.day, float(t.forecast_rows)) for t in snapshot.daily_totals if t.forecast_rows > 0]),
    ]
    temp_series: List[Series] = [
        ("Avg temperature", COLOR_TEMP, [(p.day, p.avg_temp_c) for p in snapshot.temp_trend]),
    ]

    volume_legend = _chart_legend(volume_series)
    volume_chart = _svg_line_chart(volume_series, start_at_zero=True)
    temp_legend = _chart_legend(temp_series)
    temp_chart = _svg_line_chart(temp_series, value_suffix="°")

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
                    grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
                    margin-top: 18px;
                }}
                .charts {{ margin-top: 20px; grid-template-columns: 1fr 1fr; }}
                .content {{ margin-top: 20px; grid-template-columns: 1.8fr 1fr; align-items: start; }}
                .content-secondary {{ margin-top: 20px; grid-template-columns: 1fr; }}
                .card {{
                    border: 1px solid var(--panel-border);
                    background: var(--panel);
                    border-radius: 22px;
                    box-shadow: var(--shadow);
                    min-width: 0;
                }}
                .stat-card {{ padding: 18px 18px 16px; min-height: 122px; }}
                .card-label {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.12em; }}
                .card-value {{ font-size: clamp(1.35rem, 2.3vw, 2rem); font-weight: 700; margin-top: 10px; }}
                .card-detail {{ color: var(--muted); margin-top: 8px; font-size: 0.92rem; line-height: 1.5; }}

                .panel {{ padding: 20px; }}
                .panel h2 {{ margin: 0 0 6px; font-size: 1.1rem; }}
                .panel p {{ margin: 0 0 12px; color: var(--muted); line-height: 1.6; }}

                .chart-svg {{ width: 100%; height: auto; display: block; margin-top: 10px; }}
                .grid-line {{ stroke: rgba(151, 170, 196, 0.12); stroke-width: 1; }}
                .axis-label {{ fill: var(--muted); font-size: 11px; }}
                .axis-label.y {{ text-anchor: end; }}
                .chart-empty {{ color: var(--muted); padding: 48px 0; text-align: center; font-size: 0.95rem; }}
                .legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 2px 0 0; }}
                .legend-item {{ display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 0.86rem; }}
                .legend-swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}

                .bars {{ display: grid; gap: 12px; }}
                .bar-row {{ display: grid; grid-template-columns: 1.1fr 2fr auto; gap: 12px; align-items: center; }}
                .bar-label, .bar-value {{ font-size: 0.92rem; color: var(--text); }}
                .bar-track {{ height: 12px; background: rgba(151, 170, 196, 0.12); border-radius: 999px; overflow: hidden; }}
                .bar-fill {{ height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}

                .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
                table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
                .content-secondary table {{ min-width: 320px; }}
                th, td {{ padding: 12px 10px; text-align: left; border-bottom: 1px solid rgba(151, 170, 196, 0.14); }}
                th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
                td {{ font-size: 0.95rem; }}
                tr:hover td {{ background: rgba(98, 208, 255, 0.04); }}

                .section {{ margin-top: 18px; }}
                .section-title {{ margin: 0 0 12px; font-size: 1.05rem; }}
                .section-subtitle {{ margin: 0 0 14px; color: var(--muted); font-size: 0.94rem; line-height: 1.6; }}

                .footer {{ color: var(--muted); margin-top: 20px; font-size: 0.9rem; }}

                @media (max-width: 1080px) {{
                    .stats, .charts, .content {{ grid-template-columns: 1fr 1fr; }}
                }}
                @media (max-width: 720px) {{
                    .page {{ padding: 20px 14px 40px; }}
                    .hero {{ padding: 22px; border-radius: 22px; }}
                    .stats, .charts, .content, .content-secondary {{ grid-template-columns: 1fr; }}
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
                        A static snapshot of the weather pipeline warehouse. Times are shown in Pacific
                        (Victoria, BC). This page publishes cleanly on Cloudflare Pages or GitHub Pages
                        with no server-side runtime.
                    </p>
                    <div class="grid stats">
                        {summary_cards}
                    </div>
                </section>

                <section class="grid charts">
                    <article class="card panel chart-card">
                        <h2>Ingestion volume over time</h2>
                        <p>Daily rows landing in the warehouse, bucketed by Pacific day.</p>
                        {volume_legend}
                        {volume_chart}
                    </article>

                    <article class="card panel chart-card">
                        <h2>Average temperature trend</h2>
                        <p>Mean observed temperature across all tracked cities, by Pacific day.</p>
                        {temp_legend}
                        {temp_chart}
                    </article>
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
