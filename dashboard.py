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

COLOR_CURRENT = "#62d0ff"
COLOR_FORECAST = "#8be1bb"
COLOR_BAND_FILL = "#62d0ff"
COLOR_BAND_LINE = "#ffbc73"

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
        _render_stat_card("Current rows",   f"{snapshot.current_summary.rows:,}",  f"{snapshot.current_summary.stale_cities} stale cities"),
        _render_stat_card("Forecast rows",  f"{snapshot.forecast_summary.rows:,}", f"{snapshot.forecast_summary.stale_cities} stale cities"),
        _render_stat_card("Cities tracked", len(snapshot.city_statuses),            f"Current: {snapshot.current_summary.tracked_cities} / Forecast: {snapshot.forecast_summary.tracked_cities}"),
        _render_stat_card("Latest current",  _format_timestamp(snapshot.current_summary.latest_ts),  "Warehouse snapshot"),
        _render_stat_card("Latest forecast", _format_timestamp(snapshot.forecast_summary.latest_ts), "Warehouse snapshot"),
        _render_stat_card("Generated at",    _format_timestamp(snapshot.generated_at), f"Rolling window: last {TREND_DAYS} days"),
    ])

    ribbon_series = _build_city_ribbon_series(snapshot.city_temp_series)
    ribbon_legend = _chart_legend(ribbon_series)
    ribbon_chart  = _svg_line_chart(ribbon_series, height=320, value_suffix="°", fill_area=False)

    current_volume_series: List[Series] = [
        ("Current", COLOR_CURRENT, [(t.day, float(t.current_rows)) for t in snapshot.daily_totals if t.current_rows > 0]),
    ]
    forecast_volume_series: List[Series] = [
        ("Forecast", COLOR_FORECAST, [(t.day, float(t.forecast_rows)) for t in snapshot.daily_totals if t.forecast_rows > 0]),
    ]
    current_volume_chart  = _svg_line_chart(current_volume_series,  start_at_zero=True)
    forecast_volume_chart = _svg_line_chart(forecast_volume_series, start_at_zero=True)

    band_chart     = _svg_band_chart(snapshot.temp_band)
    accuracy_bars  = _render_forecast_accuracy_bars(snapshot.forecast_accuracy)
    temp_ranking   = _render_temp_ranking(snapshot.city_current_temps)
    freshness_rows = _render_freshness_table(snapshot)

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
                    --panel: rgba(15, 28, 48, 0.92);
                    --panel-border: rgba(140, 168, 208, 0.18);
                    --text: #e8eef9;
                    --muted: #97aac4;
                    --accent: #62d0ff;
                    --accent-2: #8be1bb;
                    --accent-3: #ffbc73;
                    --shadow: 0 24px 60px rgba(2, 8, 20, 0.45);
                    --fresh: #8be1bb;
                    --aging: #ffbc73;
                    --stale: #ff6b6b;
                }}

                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                    color: var(--text);
                    background:
                        radial-gradient(circle at top left,  rgba(98, 208, 255, 0.16), transparent 30%),
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
                    width: 280px; height: 280px;
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

                .grid    {{ display: grid; gap: 16px; }}
                .stats   {{ grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); margin-top: 18px; }}
                .two-col   {{ margin-top: 20px; grid-template-columns: 1fr 1fr; }}
                .three-col {{ margin-top: 20px; grid-template-columns: 1fr 1fr 1fr; }}
                .one-col {{ margin-top: 20px; grid-template-columns: 1fr; }}

                .card {{
                    border: 1px solid var(--panel-border);
                    background: var(--panel);
                    border-radius: 22px;
                    box-shadow: var(--shadow);
                    min-width: 0;
                }}
                .stat-card {{ padding: 18px 18px 16px; min-height: 122px; }}
                .card-label  {{ color: var(--muted); font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.12em; }}
                .card-value  {{ font-size: clamp(1.35rem, 2.3vw, 2rem); font-weight: 700; margin-top: 10px; }}
                .card-detail {{ color: var(--muted); margin-top: 8px; font-size: 0.92rem; line-height: 1.5; }}

                .panel {{ padding: 20px; }}
                .panel h2 {{ margin: 0 0 6px; font-size: 1.1rem; }}
                .panel p  {{ margin: 0 0 14px; color: var(--muted); line-height: 1.6; font-size: 0.94rem; }}

                .chart-svg   {{ width: 100%; height: auto; display: block; margin-top: 10px; }}
                .grid-line   {{ stroke: rgba(151, 170, 196, 0.12); stroke-width: 1; }}
                .axis-label  {{ fill: var(--muted); font-size: 11px; }}
                .axis-label.y {{ text-anchor: end; }}
                .chart-empty {{ color: var(--muted); padding: 48px 0; text-align: center; font-size: 0.95rem; }}

                .legend      {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 4px 0 0; }}
                .legend-item {{ display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 0.86rem; }}
                .legend-swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; }}

                .band-legend      {{ display: flex; gap: 20px; flex-wrap: wrap; margin: 4px 0 0; font-size: 0.86rem; color: var(--muted); }}
                .band-legend-item {{ display: inline-flex; align-items: center; gap: 8px; }}
                .band-swatch      {{ width: 28px; height: 10px; border-radius: 3px; display: inline-block; }}

                .bars      {{ display: grid; gap: 12px; }}
                .bar-row   {{ display: grid; grid-template-columns: 1.3fr 2fr auto; gap: 12px; align-items: center; }}
                .bar-label {{ font-size: 0.92rem; color: var(--text); }}
                .bar-value {{ font-size: 0.92rem; color: var(--text); text-align: right; white-space: nowrap; }}
                .bar-count {{ color: var(--muted); font-size: 0.82rem; }}
                .bar-track {{ height: 12px; background: rgba(151, 170, 196, 0.12); border-radius: 999px; overflow: hidden; }}
                .bar-fill  {{ height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }}

                .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
                table {{ width: 100%; border-collapse: collapse; min-width: 480px; }}
                th, td {{ padding: 12px 10px; text-align: left; border-bottom: 1px solid rgba(151, 170, 196, 0.14); }}
                th {{ color: var(--muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; }}
                td {{ font-size: 0.95rem; }}
                tr:hover td {{ background: rgba(98, 208, 255, 0.04); }}

                .status-fresh {{ color: var(--fresh); font-weight: 600; }}
                .status-aging {{ color: var(--aging); font-weight: 600; }}
                .status-stale {{ color: var(--stale); font-weight: 600; }}

                .footer {{ color: var(--muted); margin-top: 20px; font-size: 0.9rem; }}

                @media (max-width: 1080px) {{
                    .stats {{ grid-template-columns: 1fr 1fr 1fr; }}
                    .three-col {{ grid-template-columns: 1fr 1fr; }}
                }}
                @media (max-width: 720px) {{
                    .page  {{ padding: 20px 14px 40px; }}
                    .hero  {{ padding: 22px; border-radius: 22px; }}
                    .stats, .two-col, .three-col {{ grid-template-columns: 1fr; }}
                    .bar-row {{ grid-template-columns: 1fr 1fr auto; }}
                }}
            </style>
        </head>
        <body>
            <main class="page">

                <!-- ── Hero ── -->
                <section class="hero">
                    <div class="eyebrow">Portfolio dashboard</div>
                    <h1>{escape(title)}</h1>
                    <p class="hero-copy">
                        A static snapshot of the weather pipeline warehouse —
                        {len(snapshot.city_statuses)} cities, current observations and 5-day forecasts.
                        Times shown in Pacific (Victoria, BC). Publishes on Cloudflare Pages with no
                        server-side runtime.
                    </p>
                    <div class="grid stats">
                        {summary_cards}
                    </div>
                </section>

                <!-- ── Multi-city temperature ribbon ── -->
                <section class="grid one-col">
                    <article class="card panel">
                        <h2>Temperature by city — last {TREND_DAYS} days</h2>
                        <p>Daily average temperature per city. One series per city; stable color assignment below.</p>
                        {ribbon_legend}
                        {ribbon_chart}
                    </article>
                </section>

                <!-- ── City temperature ranking + Ingestion volume (split by type) ── -->
                <section class="grid three-col">
                    <article class="card panel">
                        <h2>City temperature ranking</h2>
                        <p>Most recent observed temperature per city, sorted warmest to coldest.</p>
                        <div class="bars">
                            {temp_ranking}
                        </div>
                    </article>

                    <article class="card panel">
                        <h2>Current ingestion volume</h2>
                        <p>Daily current-weather rows landing in the warehouse, by Pacific day.</p>
                        {current_volume_chart}
                    </article>

                    <article class="card panel">
                        <h2>Forecast ingestion volume</h2>
                        <p>Daily forecast rows landing in the warehouse, by Pacific day.</p>
                        {forecast_volume_chart}
                    </article>
                </section>

                <!-- ── Temperature anomaly band ── -->
                <section class="grid one-col">
                    <article class="card panel">
                        <h2>Temperature spread &amp; anomaly band</h2>
                        <p>
                            Shaded region spans the min-to-max temperature across all cities each day.
                            The line shows the cross-city daily average. A reading well outside the
                            historical band is what triggers the pipeline monitor's anomaly alert.
                        </p>
                        <div class="band-legend">
                            <span class="band-legend-item">
                                <span class="band-swatch" style="background:{COLOR_BAND_FILL}; opacity:0.45;"></span>
                                Min–max range across all cities
                            </span>
                            <span class="band-legend-item">
                                <span class="band-swatch" style="background:{COLOR_BAND_LINE};"></span>
                                Cross-city daily average
                            </span>
                        </div>
                        {band_chart}
                    </article>
                </section>

                <!-- ── Forecast accuracy by horizon ── -->
                <section class="grid one-col">
                    <article class="card panel">
                        <h2>Forecast accuracy by horizon</h2>
                        <p>
                            Mean absolute error (°C) between forecast temperature and the matched actual
                            observation (±30 min window). Higher MAE at longer horizons demonstrates
                            skill degradation — a standard metric in numerical weather prediction.
                        </p>
                        <div class="bars" style="max-width: 600px;">
                            {accuracy_bars}
                        </div>
                    </article>
                </section>

                <!-- ── Pipeline health / freshness ── -->
                <section class="grid one-col">
                    <article class="card panel">
                        <h2>Pipeline health</h2>
                        <p>
                            Data freshness per city. Age is time since the most recent record landed in
                            the warehouse. <span class="status-fresh">Green</span> fresh ·
                            <span class="status-aging">Amber</span> aging ·
                            <span class="status-stale">Red</span> stale. Thresholds are generous
                            ({FRESHNESS_THRESHOLD_MINUTES // 60}h current, {FORECAST_FRESHNESS_THRESHOLD_MINUTES // 60}h forecast)
                            because GitHub Actions schedules runs on a best-effort basis.
                        </p>
                        <div class="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>City</th>
                                        <th>Current age</th>
                                        <th>Current rows</th>
                                        <th>Forecast age</th>
                                        <th>Forecast rows</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {freshness_rows}
                                </tbody>
                            </table>
                        </div>
                    </article>
                </section>

                <div class="footer">
                    Generated from PostgreSQL warehouse at {_format_timestamp(snapshot.generated_at)}.
                </div>

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
