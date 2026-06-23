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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

import psycopg2

import config

# GitHub-hosted scheduled runs are best-effort — delayed and frequently dropped
# under load — so freshness thresholds are loose to avoid flagging stale on the
# status display when the pipeline is actually healthy. Forecast runs 3x less
# often (every 3h vs hourly) so it tolerates proportionally more lag.
FRESHNESS_THRESHOLD_MINUTES = 360           # current weather (~6h)
FORECAST_FRESHNESS_THRESHOLD_MINUTES = 720  # forecast (~12h)
TREND_DAYS = 14  # rolling window for temperature charts
DEFAULT_OUTPUT_PATH = Path("docs/index.html")

DISPLAY_TZ = ZoneInfo("America/Vancouver")

# Simplified palette: one accent (blue) used everywhere, plus a single warm
# contrast for the anomaly-band average line. Status colours are muted.
COLOR_CURRENT = "#4ea1ff"
COLOR_FORECAST = "#4ea1ff"
COLOR_BAND_FILL = "#4ea1ff"
COLOR_BAND_LINE = "#e0a13c"

# Stable per-city color palette — assigned alphabetically so colors don't shift
# as cities are added or removed.
CITY_PALETTE = [
    "#62d0ff", "#8be1bb", "#ffbc73", "#ff7eb3", "#b3a0ff",
    "#7cffd4", "#ffd662", "#ff8c69", "#a8e6cf", "#c9b1ff",
]


# =============================================================================
# DATA MODEL
# =============================================================================

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
    """Mean observed temperature across all cities for a Pacific day. Kept for backwards compatibility."""
    day: date
    avg_temp_c: float


@dataclass(frozen=True)
class CityTempPoint:
    """Per-city daily average temperature for the multi-city ribbon."""
    city_name: str
    day: date
    avg_temp_c: float


@dataclass(frozen=True)
class TempBandPoint:
    """Daily cross-city temperature statistics for the anomaly band chart."""
    day: date
    avg_temp_c: float
    min_temp_c: float
    max_temp_c: float


@dataclass(frozen=True)
class ForecastAccuracyPoint:
    """Mean absolute error between forecast and actual temperature, bucketed by horizon."""
    horizon_bucket: str
    mae: float
    sample_count: int


@dataclass(frozen=True)
class CityCurrentTemp:
    """Most recent temperature reading per city, for the ranking bar chart."""
    city_name: str
    temp_c: float


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
    temp_trend: List[TempPoint] = field(default_factory=list)  # derived from temp_band; kept for compatibility
    city_temp_series: List[CityTempPoint] = field(default_factory=list)
    temp_band: List[TempBandPoint] = field(default_factory=list)
    forecast_accuracy: List[ForecastAccuracyPoint] = field(default_factory=list)
    city_current_temps: List[CityCurrentTemp] = field(default_factory=list)


# =============================================================================
# DATABASE HELPERS
# =============================================================================

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


# =============================================================================
# DATA BUILDING
# =============================================================================

def build_dashboard_snapshot(days: int = 7, threshold_minutes: int = FRESHNESS_THRESHOLD_MINUTES) -> DashboardSnapshot:
    """Query the warehouse and assemble the dashboard data model."""

    generated_at = datetime.now(timezone.utc)
    cutoff = generated_at - timedelta(days=days)
    trend_cutoff = generated_at - timedelta(days=TREND_DAYS)

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
    # Per-city daily average temperature for the multi-city ribbon (TREND_DAYS window).
    city_temp_sql = """
        SELECT
            c.city AS city_name,
            DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver') AS day,
            AVG(w.temp_c) AS avg_temp
        FROM weather_history w
        JOIN cities c ON c.id = w.city_id
        WHERE w.timestamp_utc >= %s AND w.temp_c IS NOT NULL
        GROUP BY c.city, DATE(w.timestamp_utc AT TIME ZONE 'America/Vancouver')
        ORDER BY c.city, day
    """
    # Daily cross-city min/avg/max for the anomaly band chart.
    temp_band_sql = """
        SELECT
            DATE(timestamp_utc AT TIME ZONE 'America/Vancouver') AS day,
            AVG(temp_c)  AS avg_temp,
            MIN(temp_c)  AS min_temp,
            MAX(temp_c)  AS max_temp
        FROM weather_history
        WHERE timestamp_utc >= %s AND temp_c IS NOT NULL
        GROUP BY DATE(timestamp_utc AT TIME ZONE 'America/Vancouver')
        ORDER BY day
    """
    # Forecast vs actual MAE bucketed by horizon. Match within a 30-minute window
    # using a range predicate (sargable — allows index scan on wh.timestamp_utc)
    # rather than ABS(EXTRACT(EPOCH ...)) which forces a full join scan.
    # Scoped to the last 90 days to keep the join small.
    forecast_accuracy_sql = """
        SELECT
            CASE
                WHEN wf.forecast_horizon <= 6  THEN '0–6h'
                WHEN wf.forecast_horizon <= 12 THEN '6–12h'
                WHEN wf.forecast_horizon <= 24 THEN '12–24h'
                WHEN wf.forecast_horizon <= 48 THEN '24–48h'
                ELSE '48h+'
            END AS horizon_bucket,
            ROUND(AVG(ABS(wf.temp_c - wh.temp_c))::numeric, 2) AS mae,
            COUNT(*) AS sample_count
        FROM weather_forecast wf
        JOIN weather_history wh
            ON  wh.city_id = wf.city_id
            AND wh.timestamp_utc >= wf.forecast_timestamp - INTERVAL '30 minutes'
            AND wh.timestamp_utc <  wf.forecast_timestamp + INTERVAL '30 minutes'
        WHERE wf.temp_c IS NOT NULL AND wh.temp_c IS NOT NULL
          AND wf.timestamp_utc >= NOW() - INTERVAL '90 days'
        GROUP BY 1
        ORDER BY MIN(wf.forecast_horizon)
    """
    # Most recent temperature per city for the ranking bar.
    city_current_temps_sql = """
        SELECT DISTINCT ON (w.city_id) c.city AS city_name, w.temp_c
        FROM weather_history w
        JOIN cities c ON c.id = w.city_id
        WHERE w.temp_c IS NOT NULL
        ORDER BY w.city_id, w.timestamp_utc DESC
    """

    with _connect() as conn:
        with conn.cursor() as cursor:
            current_rows, current_cities, current_latest = _run_one(cursor, current_summary_sql)
            forecast_rows, forecast_cities, forecast_latest = _run_one(cursor, forecast_summary_sql)
            city_rows          = _run_all(cursor, city_status_sql)
            daily_current_rows = _run_all(cursor, daily_current_sql, (cutoff,))
            daily_forecast_rows = _run_all(cursor, daily_forecast_sql, (cutoff,))
            city_temp_rows     = _run_all(cursor, city_temp_sql, (trend_cutoff,))
            temp_band_rows     = _run_all(cursor, temp_band_sql, (trend_cutoff,))
            forecast_acc_rows  = _run_all(cursor, forecast_accuracy_sql)
            city_curr_temp_rows = _run_all(cursor, city_current_temps_sql)

    city_statuses = [
        CityStatus(
            city_id=row[0], city_name=row[1],
            current_rows=row[2], current_latest=row[3],
            forecast_rows=row[4], forecast_latest=row[5],
        )
        for row in city_rows
    ]
    # threshold_minutes (CLI --threshold-minutes) governs current; forecast uses
    # its own looser threshold since it runs 3x less often.
    current_stale  = sum(1 for s in city_statuses if _count_stale(s.current_latest,  generated_at, threshold_minutes))
    forecast_stale = sum(1 for s in city_statuses if _count_stale(s.forecast_latest, generated_at, FORECAST_FRESHNESS_THRESHOLD_MINUTES))

    daily_current  = [DailyCount("current",  row[0], row[1], row[2]) for row in daily_current_rows]
    daily_forecast = [DailyCount("forecast", row[0], row[1], row[2]) for row in daily_forecast_rows]
    daily_totals   = _aggregate_daily_totals(daily_current, daily_forecast)

    city_temp_series = [
        CityTempPoint(row[0], row[1], float(row[2]))
        for row in city_temp_rows if row[2] is not None
    ]
    temp_band = [
        TempBandPoint(row[0], float(row[1]), float(row[2]), float(row[3]))
        for row in temp_band_rows if all(v is not None for v in row[1:])
    ]
    # Derive temp_trend from temp_band for backwards compatibility.
    temp_trend = [TempPoint(day=p.day, avg_temp_c=p.avg_temp_c) for p in temp_band]

    forecast_accuracy = [
        ForecastAccuracyPoint(row[0], float(row[1]), row[2])
        for row in forecast_acc_rows if row[1] is not None
    ]
    city_current_temps = [
        CityCurrentTemp(row[0], float(row[1]))
        for row in city_curr_temp_rows if row[1] is not None
    ]

    return DashboardSnapshot(
        generated_at=generated_at,
        current_summary=SummaryMetrics("current",  current_rows,  current_cities,  current_latest,  current_stale),
        forecast_summary=SummaryMetrics("forecast", forecast_rows, forecast_cities, forecast_latest, forecast_stale),
        city_statuses=city_statuses,
        daily_current=daily_current,
        daily_forecast=daily_forecast,
        daily_totals=daily_totals,
        temp_trend=temp_trend,
        city_temp_series=city_temp_series,
        temp_band=temp_band,
        forecast_accuracy=forecast_accuracy,
        city_current_temps=city_current_temps,
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


# =============================================================================
# FORMAT HELPERS
# =============================================================================

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


def _format_age_kpi(value: Optional[datetime], now: datetime) -> str:
    """Age for a KPI tile: '12 min ago', or 'No data' when absent."""
    if value is None:
        return "No data"
    return f"{_format_age(value, now)} ago"


def _format_short_day(d: date) -> str:
    return d.strftime("%b %d")


def _freshness_css_class(age_minutes: Optional[float], threshold: int = FRESHNESS_THRESHOLD_MINUTES) -> str:
    if age_minutes is None:
        return "status-stale"
    if age_minutes < threshold / 2:
        return "status-fresh"
    if age_minutes <= threshold:
        return "status-aging"
    return "status-stale"


# =============================================================================
# SVG CHARTS
# =============================================================================

Series = Tuple[str, str, List[Tuple[date, float]]]  # (name, color, points)


def _svg_line_chart(
    series: Sequence[Series],
    *,
    width: int = 880,
    height: int = 300,
    start_at_zero: bool = False,
    value_suffix: str = "",
    fill_area: bool = True,
) -> str:
    """Render a multi-series line chart as inline SVG."""
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
        vmax = vmin + 1

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

    grid, ylabels = [], []
    for t in range(5):
        v = vmin + (vmax - vmin) * t / 4
        y = y_of(v)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid-line" />')
        ylabels.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis-label y">{v:.0f}{value_suffix}</text>')

    xlabels = []
    for i in sorted({0, n // 2, n - 1}):
        d = all_days[i]
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        xlabels.append(
            f'<text x="{x_of(d):.1f}" y="{height - 10}" text-anchor="{anchor}" class="axis-label x">'
            f'{escape(_format_short_day(d))}</text>'
        )

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
        area_svg = f'<path d="{area}" fill="{color}" opacity="0.12" />' if fill_area else ""
        series_svg.append(
            area_svg
            + f'<polyline points="{line_pts}" fill="none" stroke="{color}" stroke-width="2" '
            + f'stroke-linejoin="round" stroke-linecap="round" />{dots}'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" class="chart-svg" role="img">'
        + "".join(grid)
        + "".join(series_svg)
        + "".join(ylabels)
        + "".join(xlabels)
        + "</svg>"
    )


def _svg_band_chart(
    band: Sequence[TempBandPoint],
    *,
    width: int = 880,
    height: int = 280,
    band_color: str = COLOR_BAND_FILL,
    line_color: str = COLOR_BAND_LINE,
    value_suffix: str = "°",
) -> str:
    """Render a shaded min/max band with a cross-city average line overlay."""
    if not band:
        return '<div class="chart-empty">No data yet — charts populate as the pipeline runs.</div>'

    pts = sorted(band, key=lambda p: p.day)
    all_days = [p.day for p in pts]
    n = len(all_days)
    day_index = {d: i for i, d in enumerate(all_days)}

    vmin = min(p.min_temp_c for p in pts)
    vmax = max(p.max_temp_c for p in pts)
    if vmax == vmin:
        vmax = vmin + 1

    pad_l, pad_r, pad_t, pad_b = 52, 20, 18, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    def x_of(d: date) -> float:
        if n == 1:
            return pad_l + plot_w / 2
        return pad_l + (day_index[d] / (n - 1)) * plot_w

    def y_of(v: float) -> float:
        return pad_t + (1 - (v - vmin) / (vmax - vmin)) * plot_h

    grid, ylabels = [], []
    for t in range(5):
        v = vmin + (vmax - vmin) * t / 4
        y = y_of(v)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" class="grid-line" />')
        ylabels.append(f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="axis-label y">{v:.0f}{value_suffix}</text>')

    xlabels = []
    for i in sorted({0, n // 2, n - 1}):
        d = all_days[i]
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        xlabels.append(
            f'<text x="{x_of(d):.1f}" y="{height - 10}" text-anchor="{anchor}" class="axis-label x">'
            f'{escape(_format_short_day(d))}</text>'
        )

    # Filled band: forward along lower (min) edge, backward along upper (max) edge.
    lower = [(x_of(p.day), y_of(p.min_temp_c)) for p in pts]
    upper = [(x_of(p.day), y_of(p.max_temp_c)) for p in pts]
    band_path = (
        f"M {lower[0][0]:.1f},{lower[0][1]:.1f} "
        + " ".join(f"L {x:.1f},{y:.1f}" for x, y in lower[1:])
        + " " + " ".join(f"L {x:.1f},{y:.1f}" for x, y in reversed(upper))
        + " Z"
    )

    avg_coords = [(x_of(p.day), y_of(p.avg_temp_c)) for p in pts]
    avg_pts_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in avg_coords)
    avg_dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{line_color}" />'
        for x, y in avg_coords
    )

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" class="chart-svg" role="img">'
        + "".join(grid)
        + f'<path d="{band_path}" fill="{band_color}" opacity="0.2" />'
        + f'<polyline points="{avg_pts_str}" fill="none" stroke="{line_color}" stroke-width="2.5" '
        + 'stroke-linejoin="round" stroke-linecap="round" />'
        + avg_dots
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


# =============================================================================
# HTML RENDERING HELPERS
# =============================================================================

def _render_stat_card(label: str, value: object, detail: str) -> str:
    return f"""
    <article class="card stat-card">
        <div class="card-label">{escape(label)}</div>
        <div class="card-value">{escape(str(value))}</div>
        <div class="card-detail">{escape(detail)}</div>
    </article>
    """


def _build_city_ribbon_series(city_temp_series: Sequence[CityTempPoint]) -> List[Series]:
    """Group per-city temp points into Series with stable color assignment (alphabetical)."""
    by_city: Dict[str, List[Tuple[date, float]]] = {}
    for pt in city_temp_series:
        by_city.setdefault(pt.city_name, []).append((pt.day, pt.avg_temp_c))
    cities = sorted(by_city.keys())
    return [
        (city, CITY_PALETTE[i % len(CITY_PALETTE)], sorted(by_city[city]))
        for i, city in enumerate(cities)
    ]


def _render_temp_ranking(city_temps: Sequence[CityCurrentTemp]) -> str:
    if not city_temps:
        return '<p class="chart-empty">No temperature data yet.</p>'
    sorted_temps = sorted(city_temps, key=lambda c: c.temp_c, reverse=True)
    min_t = min(c.temp_c for c in sorted_temps)
    max_t = max(c.temp_c for c in sorted_temps)
    t_range = max(max_t - min_t, 1.0)
    items = []
    for ct in sorted_temps:
        bar_pct = max(8, int(((ct.temp_c - min_t) / t_range) * 100))
        items.append(f"""
            <div class="bar-row">
                <div class="bar-label">{escape(ct.city_name)}</div>
                <div class="bar-track"><div class="bar-fill" style="width: {bar_pct}%"></div></div>
                <div class="bar-value">{ct.temp_c:.1f}°C</div>
            </div>
        """)
    return "\n".join(items)


def _render_forecast_accuracy_bars(accuracy: Sequence[ForecastAccuracyPoint]) -> str:
    if not accuracy:
        return '<p class="chart-empty">No forecast accuracy data yet — needs matched forecast and actual records.</p>'
    max_mae = max(p.mae for p in accuracy)
    if max_mae == 0:
        max_mae = 1.0
    items = []
    for pt in accuracy:
        bar_pct = max(4, int((pt.mae / max_mae) * 100))
        items.append(f"""
            <div class="bar-row">
                <div class="bar-label">{escape(pt.horizon_bucket)} <span class="bar-count">(n={pt.sample_count:,})</span></div>
                <div class="bar-track"><div class="bar-fill" style="width: {bar_pct}%"></div></div>
                <div class="bar-value">{pt.mae:.2f}°C</div>
            </div>
        """)
    return "\n".join(items)


def _render_freshness_table(snapshot: DashboardSnapshot) -> str:
    now = snapshot.generated_at
    ordered = sorted(snapshot.city_statuses, key=lambda s: s.city_name.lower())
    rows = []
    for status in ordered:
        c_utc = _ensure_utc(status.current_latest)
        f_utc = _ensure_utc(status.forecast_latest)
        c_age = None if c_utc is None else (now - c_utc).total_seconds() / 60
        f_age = None if f_utc is None else (now - f_utc).total_seconds() / 60
        rows.append(f"""
            <tr>
                <td>{escape(status.city_name)}</td>
                <td class="{_freshness_css_class(c_age, FRESHNESS_THRESHOLD_MINUTES)}">{escape(_format_age(status.current_latest, now))}</td>
                <td>{status.current_rows:,}</td>
                <td class="{_freshness_css_class(f_age, FORECAST_FRESHNESS_THRESHOLD_MINUTES)}">{escape(_format_age(status.forecast_latest, now))}</td>
                <td>{status.forecast_rows:,}</td>
            </tr>
        """)
    return "\n".join(rows)


# =============================================================================
# MAIN RENDER
# =============================================================================

def render_dashboard_html(snapshot: DashboardSnapshot, title: str = "Weather Data Pipeline Dashboard") -> str:
    """Render a standalone HTML dashboard page."""

    summary_cards = "".join([
        _render_stat_card("Current rows",   f"{snapshot.current_summary.rows:,}",  f"{snapshot.current_summary.stale_cities} stale"),
        _render_stat_card("Forecast rows",  f"{snapshot.forecast_summary.rows:,}", f"{snapshot.forecast_summary.stale_cities} stale"),
        _render_stat_card("Cities",         len(snapshot.city_statuses),           f"cur {snapshot.current_summary.tracked_cities} / fcst {snapshot.forecast_summary.tracked_cities}"),
        _render_stat_card("Latest current",  _format_age_kpi(snapshot.current_summary.latest_ts, snapshot.generated_at),  _format_timestamp(snapshot.current_summary.latest_ts)),
        _render_stat_card("Latest forecast", _format_age_kpi(snapshot.forecast_summary.latest_ts, snapshot.generated_at), _format_timestamp(snapshot.forecast_summary.latest_ts)),
    ])

    ribbon_series = _build_city_ribbon_series(snapshot.city_temp_series)
    ribbon_legend = _chart_legend(ribbon_series)
    ribbon_chart  = _svg_line_chart(ribbon_series, width=900, height=300, value_suffix="°", fill_area=False)

    current_volume_series: List[Series] = [
        ("Current", COLOR_CURRENT, [(t.day, float(t.current_rows)) for t in snapshot.daily_totals if t.current_rows > 0]),
    ]
    forecast_volume_series: List[Series] = [
        ("Forecast", COLOR_FORECAST, [(t.day, float(t.forecast_rows)) for t in snapshot.daily_totals if t.forecast_rows > 0]),
    ]
    current_volume_chart  = _svg_line_chart(current_volume_series,  width=460, height=280, start_at_zero=True)
    forecast_volume_chart = _svg_line_chart(forecast_volume_series, width=460, height=280, start_at_zero=True)

    band_chart     = _svg_band_chart(snapshot.temp_band, width=640, height=300)
    accuracy_bars  = _render_forecast_accuracy_bars(snapshot.forecast_accuracy)
    temp_ranking   = _render_temp_ranking(snapshot.city_current_temps)
    freshness_rows = _render_freshness_table(snapshot)

    generated = _format_timestamp(snapshot.generated_at)

    html = textwrap.dedent(f"""
    <!doctype html>
    <html lang="en">
        <head>
            <meta charset="utf-8" />
            <meta name="viewport" content="width=device-width, initial-scale=1" />
            <title>{escape(title)}</title>
            <style>
                :root {{
                    --bg: #0e0f12;
                    --panel: #16181d;
                    --track: #20242b;
                    --border: #272b33;
                    --text: #e6e8ec;
                    --muted: #8b919c;
                    --accent: #4ea1ff;
                    --ok: #5fb878;
                    --warn: #e0a13c;
                    --bad: #e05a5a;
                }}

                * {{ box-sizing: border-box; }}
                html, body {{ margin: 0; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                    color: var(--text);
                    background: var(--bg);
                    font-size: 14px;
                    line-height: 1.4;
                }}

                .page {{ max-width: 1560px; margin: 0 auto; padding: 18px 20px 28px; }}

                /* ── Top bar ── */
                .topbar {{
                    display: flex; align-items: baseline; justify-content: space-between;
                    gap: 16px; flex-wrap: wrap;
                    padding-bottom: 14px; margin-bottom: 16px;
                    border-bottom: 1px solid var(--border);
                }}
                .topbar h1 {{ margin: 0; font-size: 1.1rem; font-weight: 600; letter-spacing: 0.01em; }}
                .topbar .sub {{ color: var(--muted); font-size: 0.82rem; }}
                .topbar .gen {{ color: var(--muted); font-size: 0.8rem; }}
                .accent-dot {{ color: var(--accent); }}

                /* ── Grid system ── */
                .grid {{ display: grid; gap: 12px; }}
                .grid + .grid {{ margin-top: 12px; }}
                .kpis  {{ grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }}
                .row-2 {{ grid-template-columns: 3fr 2fr; }}
                .row-3 {{ grid-template-columns: 1fr 1fr 1fr; }}
                .row-2b {{ grid-template-columns: 1fr 1fr; }}

                /* ── Cards ── */
                .card {{
                    border: 1px solid var(--border);
                    background: var(--panel);
                    border-radius: 8px;
                    min-width: 0;
                }}
                .panel {{ padding: 14px 16px; display: flex; flex-direction: column; }}
                .panel h2 {{
                    margin: 0; font-size: 0.72rem; font-weight: 600;
                    text-transform: uppercase; letter-spacing: 0.07em; color: var(--muted);
                }}
                .cap {{ margin: 3px 0 10px; color: var(--muted); font-size: 0.78rem; }}

                /* ── KPI tiles ── */
                .stat-card {{ padding: 12px 14px; }}
                .card-label  {{ color: var(--muted); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.06em; }}
                .card-value  {{ font-size: 1.5rem; font-weight: 600; margin-top: 4px; font-variant-numeric: tabular-nums; }}
                .card-detail {{ color: var(--muted); margin-top: 3px; font-size: 0.76rem; }}

                /* ── Charts ── */
                .chart-svg   {{ width: 100%; height: auto; display: block; margin-top: auto; }}
                .grid-line   {{ stroke: #2b3038; stroke-width: 1; }}
                .axis-label  {{ fill: var(--muted); font-size: 11px; }}
                .axis-label.y {{ text-anchor: end; }}
                .chart-empty {{ color: var(--muted); padding: 32px 0; text-align: center; font-size: 0.85rem; }}

                .legend {{ display: flex; gap: 10px 14px; flex-wrap: wrap; margin: 6px 0 0; max-height: 52px; overflow: auto; }}
                .legend-item {{ display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 0.74rem; }}
                .legend-swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}

                .band-legend {{ display: flex; gap: 16px; flex-wrap: wrap; margin: 4px 0 0; font-size: 0.74rem; color: var(--muted); }}
                .band-legend-item {{ display: inline-flex; align-items: center; gap: 7px; }}
                .band-swatch {{ width: 24px; height: 9px; border-radius: 2px; display: inline-block; }}

                /* ── Bar lists ── */
                .scroll {{ overflow: auto; }}
                .scroll.tall {{ max-height: 340px; }}
                .bars      {{ display: grid; gap: 8px; }}
                .bar-row   {{ display: grid; grid-template-columns: 1.2fr 2fr auto; gap: 10px; align-items: center; }}
                .bar-label {{ font-size: 0.82rem; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
                .bar-value {{ font-size: 0.82rem; color: var(--text); text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }}
                .bar-count {{ color: var(--muted); font-size: 0.72rem; }}
                .bar-track {{ height: 8px; background: var(--track); border-radius: 4px; overflow: hidden; }}
                .bar-fill  {{ height: 100%; border-radius: inherit; background: var(--accent); }}

                /* ── Tables ── */
                table {{ width: 100%; border-collapse: collapse; }}
                thead th {{ position: sticky; top: 0; background: var(--panel); z-index: 1; }}
                th, td {{ padding: 7px 8px; text-align: left; border-bottom: 1px solid var(--border); }}
                th {{ color: var(--muted); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
                td {{ font-size: 0.82rem; font-variant-numeric: tabular-nums; }}
                tbody tr:hover td {{ background: #1b1e24; }}

                .status-fresh {{ color: var(--ok); }}
                .status-aging {{ color: var(--warn); }}
                .status-stale {{ color: var(--bad); }}

                .footer {{ color: var(--muted); margin-top: 16px; font-size: 0.76rem; }}

                @media (max-width: 1100px) {{
                    .row-2, .row-3, .row-2b {{ grid-template-columns: 1fr 1fr; }}
                }}
                @media (max-width: 680px) {{
                    .row-2, .row-3, .row-2b {{ grid-template-columns: 1fr; }}
                    .bar-row {{ grid-template-columns: 1fr 1.4fr auto; }}
                }}
            </style>
        </head>
        <body>
            <main class="page">

                <!-- ── Top bar ── -->
                <header class="topbar">
                    <div>
                        <h1><span class="accent-dot">●</span> {escape(title)}</h1>
                        <div class="sub">{len(snapshot.city_statuses)} cities · current + 5-day forecast · Pacific time (Victoria, BC)</div>
                    </div>
                    <div class="gen">Generated {generated}</div>
                </header>

                <!-- ── KPI strip ── -->
                <section class="grid kpis">
                    {summary_cards}
                </section>

                <!-- ── Main charts: ribbon + anomaly band ── -->
                <section class="grid row-2">
                    <article class="card panel">
                        <h2>Temperature by city · {TREND_DAYS}d</h2>
                        <div class="cap">Daily average temperature per city</div>
                        {ribbon_chart}
                        {ribbon_legend}
                    </article>
                    <article class="card panel">
                        <h2>Temperature spread &amp; anomaly band</h2>
                        <div class="cap">Cross-city min–max range, with daily average overlaid</div>
                        <div class="band-legend">
                            <span class="band-legend-item"><span class="band-swatch" style="background:{COLOR_BAND_FILL}; opacity:0.4;"></span>min–max</span>
                            <span class="band-legend-item"><span class="band-swatch" style="background:{COLOR_BAND_LINE};"></span>daily avg</span>
                        </div>
                        {band_chart}
                    </article>
                </section>

                <!-- ── Volume + forecast accuracy ── -->
                <section class="grid row-3">
                    <article class="card panel">
                        <h2>Current ingestion volume</h2>
                        <div class="cap">Rows/day landing in the warehouse</div>
                        {current_volume_chart}
                    </article>
                    <article class="card panel">
                        <h2>Forecast ingestion volume</h2>
                        <div class="cap">Rows/day landing in the warehouse</div>
                        {forecast_volume_chart}
                    </article>
                    <article class="card panel">
                        <h2>Forecast accuracy by horizon</h2>
                        <div class="cap">Mean absolute error (°C), forecast vs actual</div>
                        <div class="bars">
                            {accuracy_bars}
                        </div>
                    </article>
                </section>

                <!-- ── Ranking + pipeline health ── -->
                <section class="grid row-2b">
                    <article class="card panel">
                        <h2>City temperature ranking</h2>
                        <div class="cap">Latest observed temperature, warmest to coldest</div>
                        <div class="scroll tall">
                            <div class="bars">
                                {temp_ranking}
                            </div>
                        </div>
                    </article>
                    <article class="card panel">
                        <h2>Pipeline health</h2>
                        <div class="cap">
                            Freshness per city ·
                            <span class="status-fresh">fresh</span> /
                            <span class="status-aging">aging</span> /
                            <span class="status-stale">stale</span> ·
                            {FRESHNESS_THRESHOLD_MINUTES // 60}h current, {FORECAST_FRESHNESS_THRESHOLD_MINUTES // 60}h forecast SLA
                        </div>
                        <div class="scroll tall">
                            <table>
                                <thead>
                                    <tr>
                                        <th>City</th>
                                        <th>Cur age</th>
                                        <th>Cur rows</th>
                                        <th>Fcst age</th>
                                        <th>Fcst rows</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {freshness_rows}
                                </tbody>
                            </table>
                        </div>
                    </article>
                </section>

                <div class="footer">Static snapshot from the PostgreSQL warehouse · no server-side runtime · deployed on Cloudflare Pages</div>

            </main>
        </body>
    </html>
    """).lstrip()
    return html


# =============================================================================
# ENTRY POINTS
# =============================================================================

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
    parser.add_argument("--days", type=int, default=7, help="Rolling window for ingestion volume chart.")
    parser.add_argument(
        "--threshold-minutes",
        type=int,
        default=FRESHNESS_THRESHOLD_MINUTES,
        help="Freshness threshold used to colour-code stale cities.",
    )
    args = parser.parse_args(argv)
    output = write_dashboard_html(args.output, days=args.days, threshold_minutes=args.threshold_minutes)
    print(f"Wrote dashboard to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
