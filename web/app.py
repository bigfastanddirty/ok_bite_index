
def get_lunar_telemetry(dt=None):
    return lunar(dt or datetime.now(timezone.utc))



def estimate_water_temperature(
    air_temp_f,
    month=None,
    recent_air_temp_f=None,
    when=None
):
    """
    Estimate Oklahoma reservoir surface-water temperature when actual
    reservoir temperature telemetry is unavailable.

    The model combines:
      - a smooth annual reservoir-temperature cycle
      - modest current-air-temperature forcing

    Reservoir temperature intentionally reacts much more slowly than air
    temperature. Measured reservoir telemetry always takes precedence.
    """

    try:
        air_temp = (
            float(air_temp_f)
            if air_temp_f is not None
            else 70.0
        )
    except (TypeError, ValueError):
        air_temp = 70.0

    # --------------------------------------------------------
    # Resolve calendar date.
    #
    # Existing callers that only provide month continue working,
    # but an actual timestamp can be supplied for a smoother
    # day-of-year seasonal calculation.
    # --------------------------------------------------------
    dt = None

    if when is not None:
        if isinstance(when, datetime):
            dt = when
        else:
            try:
                dt = datetime.fromisoformat(
                    str(when).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                dt = None

    if dt is None:
        try:
            month_num = (
                int(month)
                if month is not None
                else datetime.now(timezone.utc).month
            )
        except (TypeError, ValueError):
            month_num = datetime.now(timezone.utc).month

        month_num = max(1, min(12, month_num))

        # Mid-month approximation avoids abrupt month-boundary
        # jumps for legacy callers.
        dt = datetime(
            2024,
            month_num,
            15,
            tzinfo=timezone.utc
        )

    day_of_year = dt.timetuple().tm_yday

    # --------------------------------------------------------
    # Smooth Oklahoma reservoir seasonal baseline.
    #
    # Approximate annual pattern:
    #   winter low  : ~46 F
    #   spring      : gradual warming
    #   summer peak : ~88 F
    #   fall        : gradual cooling
    #
    # Peak is delayed relative to peak solar input to represent
    # reservoir thermal inertia.
    # --------------------------------------------------------
    seasonal_baseline = (
        67.0
        + 21.0
        * math.sin(
            (2.0 * math.pi * (day_of_year - 119))
            / 365.2425
        )
    )

    # --------------------------------------------------------
    # Atmospheric forcing.
    #
    # Prefer the trailing 96-hour air-temperature mean when it is
    # available. Current air receives only 25% of the atmospheric
    # weight so a single hot/cold afternoon cannot unrealistically
    # move the estimated reservoir temperature.
    # --------------------------------------------------------
    try:
        recent_air = (
            float(recent_air_temp_f)
            if recent_air_temp_f is not None
            else air_temp
        )
    except (TypeError, ValueError):
        recent_air = air_temp

    effective_air = (
        (recent_air * 0.75)
        + (air_temp * 0.25)
    )

    estimated = (
        seasonal_baseline
        + 0.30 * (effective_air - seasonal_baseline)
    )

    # Broad physical sanity limits only.
    # Unlike the old model, there is no 89 F seasonal plateau.
    estimated = max(
        35.0,
        min(94.0, estimated)
    )

    return round(estimated, 1)



import os
import math
import json
import urllib.request
import time
import threading
from quality import utc, is_fresh, pressure_delta, MODEL_VERSION, record_prediction
from astronomy import lunar, solar_factor, solunar_factor, solar_context
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from collections import deque

from logging_setup import setup_logging
logger = setup_logging("web")
app = FastAPI()

@app.middleware("http")
async def log_unexpected_api_errors(request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        route = request.scope.get('route')
        logger.error('Unhandled API error', extra={
            'event': 'api_failed', 'method': request.method,
            'path': route.path if route else '<unmatched>', 'status_code': 500},
            exc_info=(type(exc), exc, exc.__traceback__))
        return JSONResponse(status_code=500, content={'detail': 'Internal server error'})



FORECAST_CACHE_TTL_SECONDS = 15 * 60
FORECAST_STALE_MAX_SECONDS = 60 * 60

_forecast_cache = {}
_forecast_cache_lock = threading.Lock()


def fetch_open_meteo_forecast_cached(
    lake_code,
    lat,
    lon,
):
    """
    Fetch Open-Meteo hourly forecast with a 15-minute per-lake cache.

    Browser cache-busting query parameters do not affect this server-side
    cache. If Open-Meteo temporarily fails, an expired entry may be reused
    for up to one hour.
    """
    cache_key = (
        str(lake_code).upper(),
        round(float(lat), 5),
        round(float(lon), 5),
    )

    now_monotonic = time.monotonic()

    with _forecast_cache_lock:
        entry = _forecast_cache.get(cache_key)

    if entry is not None:
        age_seconds = (
            now_monotonic
            - entry["stored_at"]
        )

        if age_seconds < FORECAST_CACHE_TTL_SECONDS:
            return (
                entry["data"],
                "HIT",
                int(age_seconds),
            )
    else:
        age_seconds = None

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={float(lat)}"
        f"&longitude={float(lon)}"
        f"&hourly="
        f"temperature_2m,"
        f"relative_humidity_2m,"
        f"surface_pressure,"
        f"wind_speed_10m,"
        f"wind_gusts_10m,"
        f"wind_direction_10m,"
        f"cloud_cover,"
        f"precipitation_probability,"
        f"precipitation"
        f"&temperature_unit=fahrenheit"
        f"&wind_speed_unit=mph"
        f"&precipitation_unit=inch"
        f"&timezone=UTC&timeformat=unixtime&past_days=1"
        f"&forecast_days=3"
    )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "OKLakesTelemetry/2.0"
            },
        )

        with urllib.request.urlopen(
            req,
            timeout=8,
        ) as res:
            data = json.loads(
                res.read().decode()
            )

        with _forecast_cache_lock:
            _forecast_cache[cache_key] = {
                "stored_at": time.monotonic(),
                "data": data,
            }

        return data, "MISS", 0

    except Exception as exc:
        if (
            entry is not None
            and age_seconds is not None
            and age_seconds
                <= FORECAST_STALE_MAX_SECONDS
        ):
            logger.warning('Forecast provider failed; serving bounded stale cache', extra={
                'event': 'forecast_stale_cache', 'source': 'open-meteo', 'lake': lake_code,
                'cache_age_seconds': int(age_seconds)}, exc_info=True)

            return (
                entry["data"],
                "STALE",
                int(age_seconds),
            )

        logger.exception('Forecast provider failed; no acceptable cached forecast', extra={
            'event': 'forecast_unavailable', 'source': 'open-meteo', 'lake': lake_code})
        raise HTTPException(
            status_code=502,
            detail="Forecast provider unavailable",
        )


DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def calculate_time_of_day_factor(dt_val,lon=-97.5,lat=35.5):
    return solar_factor(dt_val,lon,lat)

def finite_number(value):
    """Keep missing/invalid readings distinct from a measured zero."""
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def calculate_spawn_phase(water_temp_f, month):
    """Broad lake season; surface temperature cannot verify species spawning."""
    wt = finite_number(water_temp_f)
    if wt is None:
        phase = "Seasonal conditions unavailable"
    elif wt < 50:
        phase = "Cold-water pattern"
    elif wt >= 80:
        phase = "Hot-water pattern"
    elif month in (2, 3, 4, 5):
        phase = "Spring transition"
    elif month in (9, 10, 11, 12):
        phase = "Fall cooling pattern"
    else:
        phase = "Warm-water transition"
    return {"phase": phase, "tactic": "Locate forage and remaining submerged cover; confirm fish depth locally."}


def calculate_solunar_factor(dt_val,lon,lat):
    return solunar_factor(dt_val,lon,lat)

def calculate_water_temp_factor(temp_f: Optional[float]):
    if temp_f is None:
        return 0.0
    try:
        tf = float(temp_f)
        if 62.0 <= tf <= 76.0:
            return 15.0
        elif 54.0 <= tf < 62.0:
            return 8.0
        elif 76.0 < tf <= 83.0:
            return 7.0
        elif 46.0 <= tf < 54.0:
            return -4.0
        elif 83.0 < tf <= 88.0:
            return -8.0
        else:
            return -15.0
    except Exception:
        return 0.0

def calculate_hydrology_factor(diff_from_normal_ft=None, elev_delta_24h=None):
    """
    Calculate the lake-level contribution to the bite score.

    Uses:
      - current pool elevation relative to normal pool
      - 24-hour lake-level movement

    The combined hydrology effect is capped at -12 to +6 so one
    hydrologic event cannot dominate the entire bite score.
    """

    pool_points = 0.0
    trend_points = 0.0
    details = []

    # --------------------------------------------------------
    # Pool elevation relative to normal
    # --------------------------------------------------------
    if diff_from_normal_ft is not None:
        try:
            diff = float(diff_from_normal_ft)

            if -1.0 <= diff <= 1.0:
                pool_points = 3.0
                details.append(f"{diff:+.2f} ft from normal pool")

            elif -2.0 <= diff < -1.0:
                pool_points = 1.0
                details.append(f"{diff:+.2f} ft below normal")

            elif -4.0 <= diff < -2.0:
                pool_points = -3.0
                details.append(f"{diff:+.2f} ft below normal")

            elif -7.0 <= diff < -4.0:
                pool_points = -7.0
                details.append(f"{diff:+.2f} ft significantly below normal")

            elif diff < -7.0:
                pool_points = -10.0
                details.append(f"{diff:+.2f} ft severely below normal")

            elif 1.0 < diff <= 2.0:
                pool_points = 1.0
                details.append(f"{diff:+.2f} ft above normal")

            elif 2.0 < diff <= 4.0:
                pool_points = 2.0
                details.append(f"{diff:+.2f} ft above normal")

            else:
                pool_points = -2.0
                details.append(f"{diff:+.2f} ft well above normal")

        except (TypeError, ValueError):
            pass

    # --------------------------------------------------------
    # 24-hour elevation trend
    # --------------------------------------------------------
    if elev_delta_24h is not None:
        try:
            delta = float(elev_delta_24h)

            if abs(delta) <= 0.10:
                # Stability is beneficial near normal pool, but should not
                # erase the penalty from a severe drawdown.
                if diff_from_normal_ft is not None:
                    try:
                        current_diff = float(diff_from_normal_ft)

                        if current_diff < -4.0:
                            trend_points = 1.0
                        elif current_diff < -2.0:
                            trend_points = 2.0
                        else:
                            trend_points = 3.0

                    except (TypeError, ValueError):
                        trend_points = 3.0
                else:
                    trend_points = 3.0

                details.append(f"stable ({delta:+.2f} ft/24h)")

            elif 0.10 < delta <= 0.75:
                trend_points = 2.0
                details.append(f"slowly rising ({delta:+.2f} ft/24h)")

            elif delta > 0.75:
                trend_points = -2.0
                details.append(f"rapidly rising ({delta:+.2f} ft/24h)")

            elif -0.50 <= delta < -0.10:
                trend_points = -1.0
                details.append(f"slowly falling ({delta:+.2f} ft/24h)")

            elif delta < -0.50:
                trend_points = -4.0
                details.append(f"rapidly falling ({delta:+.2f} ft/24h)")

        except (TypeError, ValueError):
            pass

    combined = pool_points + trend_points

    # Prevent hydrology from overwhelming the entire bite model.
    combined = max(-12.0, min(6.0, combined))

    if not details:
        detail = "Lake-level telemetry unavailable"
    else:
        detail = "; ".join(details)

    return combined, detail


def calculate_pool_score_cap(diff_from_normal_ft=None):
    """
    Apply a maximum Bite Index score during severe reservoir drawdown.

    The normal hydrology modifier still applies first. This ceiling prevents
    otherwise favorable short-term weather conditions from fully overcoming
    severely abnormal reservoir conditions.

    Returns:
        (maximum_score, detail)

        maximum_score is None when no cap applies.
    """
    if diff_from_normal_ft is None:
        return None, None

    try:
        diff = float(diff_from_normal_ft)
    except (TypeError, ValueError):
        return None, None

    # More than 10 ft below normal
    if diff < -10.0:
        return 34.0, (
            f"{diff:+.2f} ft below normal pool; "
            "severe drawdown limits Bite Score to 34"
        )

    # 7 through 10 ft below normal
    if diff <= -7.0:
        return 44.0, (
            f"{diff:+.2f} ft below normal pool; "
            "major drawdown limits Bite Score to 44"
        )

    # 4 through 7 ft below normal
    if diff <= -4.0:
        return 64.0, (
            f"{diff:+.2f} ft below normal pool; "
            "drawdown limits Bite Score to 64"
        )

    return None, None


def calculate_bite_score(
    delta_press,
    wind_speed,
    cloud_cover,
    dt_val,
    lon,
    lat,
    water_temp_f=None,
    release_cfs=None,
    diff_from_normal_ft=None,
    elev_delta_24h=None,
    precip_in=None,
    include_factors=False
):
    """
    Canonical Bite Index engine.

    Returns:
        score,
        rating,
        solunar_window,
        factors

    factors is populated only when include_factors=True.
    """
    # Unknown inputs must not be converted into favorable or unfavorable conditions.
    essential = [delta_press, wind_speed, cloud_cover]
    if any(v is None or not math.isfinite(float(v)) for v in essential):
        return None, 'UNAVAILABLE', 'NONE', []
    score = 35.0
    factors = []

    def add(label, points, detail):
        nonlocal score

        points = float(
            points or 0.0
        )

        score += points

        if include_factors:
            factors.append({
                "label": label,
                "points": int(round(points)),
                "detail": detail
            })

    try:
        dp = float(
            delta_press
            if delta_press is not None
            else 0.0
        )
    except (TypeError, ValueError):
        dp = 0.0

    try:
        wind = float(
            wind_speed
            if wind_speed is not None
            else 0.0
        )
    except (TypeError, ValueError):
        wind = 0.0

    try:
        clouds = float(
            cloud_cover
            if cloud_cover is not None
            else 0.0
        )
    except (TypeError, ValueError):
        clouds = 0.0


    # ========================================================
    # 1. BAROMETRIC PRESSURE
    # ========================================================

    if dp <= -1.2:
        add(
            "Pressure trend",
            25,
            "Strong falling-pressure feeding trigger"
        )

    elif dp <= -0.4:
        add(
            "Pressure trend",
            15,
            "Falling pressure favors feeding"
        )

    elif dp < 0.6:
        add(
            "Pressure trend",
            4,
            "Stable pressure"
        )

    elif dp >= 1.8:
        add(
            "Pressure trend",
            -22,
            "Rapidly rising pressure"
        )

    else:
        add(
            "Pressure trend",
            -10,
            "Rising pressure"
        )


    # ========================================================
    # 2. WIND
    # ========================================================

    if wind < 3.0:
        add(
            "Wind",
            -6,
            "Very light wind / slick conditions"
        )

    elif wind < 6.0:
        add(
            "Wind",
            2,
            "Light chop"
        )

    elif wind < 12.0:
        add(
            "Wind",
            10,
            "Productive surface chop"
        )

    elif wind < 18.0:
        add(
            "Wind",
            12,
            "Strong Oklahoma reservoir feeding chop"
        )

    elif wind < 23.0:
        add(
            "Wind",
            6,
            "Strong but still productive wind"
        )

    elif wind < 28.0:
        add(
            "Wind",
            -4,
            "Difficult boat control and wave exposure"
        )

    else:
        add(
            "Wind",
            -12,
            "Excessive wind / poor fishability"
        )


    # ========================================================
    # 3. CLOUD COVER
    # ========================================================

    if clouds >= 65.0:
        add(
            "Cloud cover",
            8,
            "Low-light conditions"
        )

    elif clouds <= 15.0:
        add(
            "Cloud cover",
            -4,
            "Bright clear conditions"
        )

    else:
        add(
            "Cloud cover",
            0,
            "Moderate cloud cover"
        )


    # ========================================================
    # 4. TIME OF DAY
    # ========================================================

    time_score = (
        calculate_time_of_day_factor(dt_val,lon,lat)
    )

    add(
        "Time of day",
        time_score,
        "Diurnal feeding-window adjustment"
    )


    # ========================================================
    # 5. SOLUNAR
    # ========================================================

    sol_score, window_type = (
        calculate_solunar_factor(
            dt_val,
            lon,
            lat
        )
    )

    add(
        "Solunar",
        sol_score,
        f"{window_type.title()} lunar window"
    )


    # ========================================================
    # 6. WATER TEMPERATURE
    # ========================================================

    wt_score = (
        calculate_water_temp_factor(
            water_temp_f
        )
    )

    add(
        "Water temperature",
        wt_score,
        "Seasonal metabolism adjustment"
    )


    # ========================================================
    # 7. DAM FLOW
    # ========================================================

    flow_score = 0.0

    if release_cfs is not None:
        try:
            if float(release_cfs) > 100.0:
                flow_score = 5.0

        except (TypeError, ValueError):
            pass

    add(
        "Dam flow",
        flow_score,
        "Current / tailrace effect"
    )


    # ========================================================
    # 8. RESERVOIR HYDROLOGY
    # ========================================================

    hydrology_score, hydrology_detail = (
        calculate_hydrology_factor(
            diff_from_normal_ft=(
                diff_from_normal_ft
            ),
            elev_delta_24h=(
                elev_delta_24h
            )
        )
    )

    add(
        "Lake level",
        hydrology_score,
        hydrology_detail
    )


    # ========================================================
    # 9. PRECIPITATION
    # ========================================================

    precip_score = 0.0

    if precip_in is not None:
        try:
            precip = float(
                precip_in
            )

            if 0.05 <= precip <= 0.40:
                precip_score = 7.0

            elif 0.40 < precip <= 0.80:
                precip_score = 3.0

            elif precip > 1.20:
                precip_score = -12.0

        except (TypeError, ValueError):
            pass

    add(
        "Precipitation",
        precip_score,
        "Runoff / disturbance adjustment"
    )


    # ========================================================
    # SEVERE DRAWDOWN SCORE CAP
    # ========================================================

    pool_score_cap, pool_cap_detail = (
        calculate_pool_score_cap(
            diff_from_normal_ft
        )
    )

    if (
        pool_score_cap is not None
        and score > pool_score_cap
    ):
        cap_adjustment = (
            float(pool_score_cap)
            - score
        )

        add(
            "Lake level cap",
            cap_adjustment,
            pool_cap_detail
        )


    # ========================================================
    # FINAL SCORE
    # ========================================================

    final_score = max(
        5,
        min(
            100,
            int(round(score))
        )
    )

    if final_score >= 80:
        rating = "EPIC"

    elif final_score >= 65:
        rating = "GOOD"

    elif final_score >= 45:
        rating = "FAIR"

    else:
        rating = "TOUGH"

    return (
        final_score,
        rating,
        window_type,
        factors
    )


def calculate_best_window(forecast_cards, window_hours=3, horizon_hours=24):
    """Return the strongest rolling fishing window in the next 24 forecast hours."""
    now=datetime.now(timezone.utc)
    cards=[c for c in (forecast_cards or []) if utc(c['time'])>=now][:horizon_hours]
    if not cards:
        return None

    width = max(1, min(int(window_hours), len(cards)))
    best = None

    for i in range(0, len(cards) - width + 1):
        group = cards[i:i + width]
        if any(c.get('bite_score') is None for c in group):
            continue
        if any((utc(b['time'])-utc(a['time'])).total_seconds()!=3600 for a,b in zip(group,group[1:])):
            continue
        scores = [float(x.get("bite_score") or 0) for x in group]
        avg_score = sum(scores) / len(scores)
        peak_score = max(scores)

        candidate = {
            "start": group[0].get("time"),
            "end": (utc(group[-1]["time"])+timedelta(hours=1)).isoformat(),
            "average_score": int(round(avg_score)),
            "peak_score": int(round(peak_score))
        }

        if best is None or (candidate["average_score"], candidate["peak_score"]) > (best["average_score"], best["peak_score"]):
            best = candidate

    if best:
        sc = best["average_score"]
        best["rating"] = "EPIC" if sc >= 80 else ("GOOD" if sc >= 65 else ("FAIR" if sc >= 45 else "TOUGH"))

    return best


@app.api_route("/methodology", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/about", methods=["GET", "HEAD"], response_class=HTMLResponse)
def get_methodology():
    # Keep direct links and sidebar navigation on the same live dashboard shell.
    # The browser selects Methodology from /methodology or /about.
    return get_index()

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def get_index():
    with open("index.html", "r") as f:
        content = f.read()
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    from fastapi.responses import PlainTextResponse

    content = """User-agent: *
Allow: /
"""

    return PlainTextResponse(content)



@app.get("/api/lakes")
def get_lakes(response: Response):
    response.headers["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        cur.execute("""
            SELECT
                lake_code,
                name AS lake_name
            FROM lakes
            ORDER BY lake_code;
        """)

        return cur.fetchall()

    finally:
        conn.close()






def classify_current(inflow_cfs=None, release_cfs=None):
    """
    Categorize reported discharge for the existing species heuristic.
    This does not measure current velocity at the fishing location.

    This is intentionally categorical rather than proportional. Very large
    flows should identify strong current without allowing extreme CFS values
    to dominate tactical/species scoring.

    Returns:
        NONE      < 100 CFS
        LIGHT     100-199 CFS
        MODERATE  200-799 CFS
        STRONG    >= 800 CFS
    """
    values = []

    for value in (inflow_cfs, release_cfs):
        number = finite_number(value)
        if number is not None and number >= 0:
            values.append(number)

    if not values:
        return "UNKNOWN"

    current_cfs = max(values)

    if current_cfs >= 800.0:
        return "STRONG"

    if current_cfs >= 200.0:
        return "MODERATE"

    if current_cfs >= 100.0:
        return "LIGHT"

    return "NONE"


def rank_target_species(
    species_list,
    water_temp_f,
    month,
    diff_from_normal_ft=None,
    elevation_delta_24h=None,
    inflow_cfs=None,
    release_cfs=None,
    pressure_delta_hpa=None,
    wind_speed_mph=None,
    cloud_cover_pct=None,
    precipitation_in=None,
    top_n=3
):
    """
    Rank only species that ODWC lists for the selected lake.

    Returns:
      ranked_names: top species names
      details: explainable scoring records for all candidates

    This is a tactical ranking heuristic, not a biological population model.
    """
    if not species_list:
        return [], []

    wt = finite_number(water_temp_f)
    pool_diff = finite_number(diff_from_normal_ft)
    elev_delta = finite_number(elevation_delta_24h)
    inflow = finite_number(inflow_cfs)
    release = finite_number(release_cfs)
    dp = finite_number(pressure_delta_hpa)
    wind = finite_number(wind_speed_mph)
    clouds = finite_number(cloud_cover_pct)
    precip = finite_number(precipitation_in)

    current_class = classify_current(
        inflow_cfs=inflow,
        release_cfs=release
    )

    month = int(month or datetime.now(timezone.utc).month)

    def add(rec, points, reason):
        rec["score"] += points

        if reason:
            rec["reasons"].append(reason)

        rec["factors"].append({
            "points": points,
            "reason": reason or ""
        })

    ranked = []

    for original_name in species_list:
        name = str(original_name).strip()
        key = name.lower()

        rec = {
            "species": name,
            "score": 50.0,
            "baseline": 50.0,
            "reasons": [],
            "factors": []
        }

        # ---------- LARGEMOUTH BASS ----------
        if "bass, largemouth" in key or "largemouth bass" in key:
            if wt is not None and (60 <= wt <= 82):
                add(rec, 16, "Water temperature is in a strong largemouth feeding range.")
            elif wt is not None and (wt > 86):
                add(rec, -8, "Very warm water can suppress midday largemouth activity.")
            elif wt is not None and (wt < 48):
                add(rec, -10, "Cold water reduces largemouth metabolism.")

            if pool_diff is not None and (pool_diff >= 1.0):
                add(rec, 12, "Elevated pool expands flooded shoreline cover.")
            if elev_delta is not None and (elev_delta > 0.10):
                add(rec, 6, "Rising water encourages shallow movement.")
            if wind is not None and (5 <= wind <= 15):
                add(rec, 7, "Moderate wind improves ambush conditions.")
            if dp is not None and (dp <= -0.5):
                add(rec, 8, "Falling pressure is a secondary weather signal in this unvalidated ranking.")
            if clouds is not None and (clouds >= 60):
                add(rec, 6, "Cloud cover favors roaming shallow fish.")
            if 3 <= month <= 6:
                add(rec, 6, "Spring through early summer is a strong seasonal window.")

        # ---------- SMALLMOUTH BASS ----------
        elif "bass, smallmouth" in key or "smallmouth bass" in key:
            if wt is not None and (52 <= wt <= 74):
                add(rec, 18, "Moderate water temperature favors smallmouth activity.")
            elif wt is not None and (wt >= 82):
                add(rec, -10, "Hot surface temperatures often push smallmouth deeper.")
            if wind is not None and (6 <= wind <= 18):
                add(rec, 10, "Wind activates rocky points and offshore structure.")
            if dp is not None and (dp <= -0.4):
                add(rec, 7, "Falling pressure is a secondary weather signal in this unvalidated ranking.")
            if pool_diff is not None and (pool_diff <= 1.0):
                add(rec, 4, "Pool is not substantially elevated; check remaining rocky structure.")
            if month in (3, 4, 5, 9, 10, 11):
                add(rec, 6, "Spring and fall favor smallmouth movement.")

        # ---------- SPOTTED BASS ----------
        elif "bass, spotted" in key or "spotted bass" in key:
            if wt is not None and (55 <= wt <= 80):
                add(rec, 14, "Water temperature supports active spotted bass.")
            if wind is not None and (5 <= wind <= 16):
                add(rec, 8, "Moderate wind improves feeding on points and bluffs.")
            if current_class in ("MODERATE", "STRONG"):
                add(rec, 6, "Reported inflow or release is a possible localized-current signal, not a measurement of current at the fishing spot.")
            if dp is not None and (dp <= -0.4):
                add(rec, 5, "Falling pressure is a secondary weather signal in this unvalidated ranking.")

        # ---------- WHITE BASS ----------
        elif "bass, white" in key or "white bass" in key:
            if wt is not None and (58 <= wt <= 84):
                add(rec, 16, "Water temperature supports active white bass.")
            if wind is not None and (wind >= 6):
                add(rec, 10, "Wind helps concentrate shad and schooling fish.")
            if current_class in ("MODERATE", "STRONG"):
                add(rec, 10, "Reported inflow or release is a possible localized-current signal, not a measurement of current at the fishing spot.")
            if dp is not None and (dp <= -0.5):
                add(rec, 8, "Falling pressure is a secondary weather signal in this unvalidated ranking.")
            if month in (3, 4, 5, 9, 10, 11):
                add(rec, 8, "Spring and fall are strong white bass movement periods.")

        # ---------- STRIPED / HYBRID BASS ----------
        elif "striped" in key or "hybrid" in key:
            if wt is not None and (55 <= wt <= 78):
                add(rec, 18, "Water temperature is favorable for striped/hybrid bass.")
            elif wt is not None and (wt > 84):
                add(rec, -8, "Very warm water can restrict striped bass to deeper refuge.")
            if wind is not None and (wind >= 6):
                add(rec, 9, "Wind can push forage into predictable feeding zones.")
            if current_class in ("MODERATE", "STRONG"):
                add(rec, 12, "Reported inflow or release is a possible localized-current signal, not a measurement of current at the fishing spot.")
            if dp is not None and (dp <= -0.5):
                add(rec, 7, "Falling pressure is a secondary weather signal in this unvalidated ranking.")
            if clouds is not None and (clouds >= 50):
                add(rec, 5, "Lower light favors longer feeding windows.")

        # ---------- CRAPPIE ----------
        elif "crappie" in key:
            if wt is not None and (52 <= wt <= 72):
                add(rec, 18, "Water temperature is favorable for crappie.")
            elif wt is not None and (72 < wt <= 82):
                add(rec, 8, "Warm water still supports crappie around deeper structure.")
            elif wt is not None and (wt > 86):
                add(rec, -7, "Extreme heat often pushes crappie deeper and reduces daytime activity.")
            if elev_delta is not None and (abs(elev_delta) <= 0.20):
                add(rec, 8, "Stable water level favors predictable brush and structure patterns.")
            if wind is not None and (wind <= 12):
                add(rec, 5, "Light-to-moderate wind supports controlled vertical presentations.")
            if month in (2, 3, 4, 5, 10, 11):
                add(rec, 8, "Seasonal timing favors crappie movement and feeding.")
            if clouds is not None and (clouds >= 50):
                add(rec, 4, "Cloud cover can extend shallow feeding periods.")

        # ---------- BLUE CATFISH ----------
        elif "catfish, blue" in key or "blue catfish" in key:
            if wt is not None and (55 <= wt <= 85):
                add(rec, 14, "Water temperature supports active blue catfish.")
            if current_class in ("MODERATE", "STRONG"):
                add(rec, 14, "Reported inflow or release is a possible localized-current signal, not a measurement of current at the fishing spot.")
            if pool_diff is not None and (pool_diff >= 1.0):
                add(rec, 9, "Elevated water expands feeding access to flooded habitat.")
            if elev_delta is not None and (elev_delta > 0.10):
                add(rec, 6, "Rising water can increase shallow feeding activity.")
            if precip is not None and (precip >= 0.05):
                add(rec, 7, "Recent precipitation can improve runoff-driven feeding.")
            if clouds is not None and (clouds >= 50):
                add(rec, 4, "Low light can extend active feeding windows.")

        # ---------- CHANNEL CATFISH ----------
        elif "catfish, channel" in key or "channel catfish" in key:
            if wt is not None and (62 <= wt <= 86):
                add(rec, 16, "Warm water favors channel catfish feeding.")
            if precip is not None and (precip >= 0.05):
                add(rec, 10, "Recent rain can increase shoreline and inflow feeding.")
            if current_class in ("LIGHT", "MODERATE", "STRONG"):
                add(rec, 8, "Reported inflow or release suggests checking localized flow; current at the fishing spot is unverified.")
            if pool_diff is not None and (pool_diff >= 0.5):
                add(rec, 6, "Elevated water increases access to shallow feeding areas.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 6, "Warm-season timing favors channel catfish activity.")

        # ---------- FLATHEAD CATFISH ----------
        elif "catfish, flathead" in key or "flathead catfish" in key:
            if wt is not None and (68 <= wt <= 84):
                add(rec, 18, "Warm water supports active flathead metabolism.")
            elif wt is not None and (wt < 55):
                add(rec, -12, "Cold water strongly reduces flathead activity.")
            if current_class in ("MODERATE", "STRONG"):
                add(rec, 8, "Reported inflow or release is a possible localized-current signal, not a measurement of current at the fishing spot.")
            if clouds is not None and (clouds >= 50):
                add(rec, 5, "Low light favors flathead movement.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 8, "Warm-season timing favors flathead activity.")

        # ---------- WALLEYE / SAUGEYE / SAUGER ----------
        elif "walleye" in key or "saugeye" in key or "sauger" in key:
            if wt is not None and (45 <= wt <= 68):
                add(rec, 20, "Cool-to-moderate water strongly favors walleye-family activity.")
            elif wt is not None and (68 < wt <= 76):
                add(rec, 6, "Water remains workable but may shift fish deeper.")
            elif wt is not None and (wt >= 80):
                add(rec, -12, "Warm water generally reduces shallow walleye-family activity.")
            if clouds is not None and (clouds >= 50):
                add(rec, 9, "Low light favors walleye-family feeding.")
            if wind is not None and (wind >= 5):
                add(rec, 7, "Wind creates low-light, broken-surface feeding conditions.")
            if month in (2, 3, 4, 10, 11, 12):
                add(rec, 8, "Seasonal timing favors cool-water movement.")

        # ---------- PADDLEFISH ----------
        elif "paddlefish" in key:
            if current_class == "STRONG":
                add(rec, 20, "Strong current is favorable for paddlefish movement.")
            elif current_class != "UNKNOWN":
                add(rec, -8, "Limited current reduces paddlefish movement potential.")
            if month in (2, 3, 4, 5):
                add(rec, 12, "Spring timing favors paddlefish movement.")

        # ---------- SUNFISH ----------
        elif "sunfish" in key:
            if wt is not None and (68 <= wt <= 84):
                add(rec, 14, "Warm water favors sunfish activity.")
            if pool_diff is not None and (pool_diff >= 0):
                add(rec, 4, "Stable-to-elevated pool supports shallow cover.")
            if wind is not None and (wind <= 12):
                add(rec, 4, "Lower wind improves shallow presentation control.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 6, "Warm-season timing favors sunfish feeding.")

        # ---------- ALLIGATOR GAR ----------
        elif "gar, alligator" in key or "alligator gar" in key:
            if wt is not None and (wt >= 70):
                add(rec, 14, "Warm water favors alligator gar activity.")
            if (inflow is not None and inflow >= 200) or (pool_diff is not None and pool_diff >= 1.0):
                add(rec, 8, "Current or elevated water can increase feeding opportunities.")

        # Unknown/other ODWC species remain valid candidates with neutral score.
        # This ensures we never invent a species that ODWC did not list.

        raw_score = rec["score"]
        final_score = max(0.0, min(100.0, raw_score))

        rec["raw_score"] = round(raw_score, 1)
        rec["score"] = round(final_score, 1)

        if final_score != raw_score:
            rec["score_cap_adjustment"] = round(
                final_score - raw_score,
                1
            )
        else:
            rec["score_cap_adjustment"] = 0.0

        ranked.append(rec)

    ranked.sort(key=lambda x: (-x["score"], x["species"].lower()))

    top = ranked[:max(1, min(int(top_n), len(ranked)))]
    return [item["species"] for item in top], ranked




def format_species_name(name):
    """
    Convert ODWC's canonical "Family, Type" labels into natural prose while
    leaving the stored database value unchanged.
    """
    value = str(name or "").strip()

    if "," in value:
        left, right = [part.strip() for part in value.split(",", 1)]
        if left and right:
            return f"{right} {left}"

    return value


def _species_family(species_name):
    s = str(species_name or "").lower()

    if "bass, largemouth" in s or "largemouth bass" in s:
        return "largemouth"
    if "bass, smallmouth" in s or "smallmouth bass" in s:
        return "smallmouth"
    if "bass, spotted" in s or "spotted bass" in s:
        return "spotted"
    if "bass, white" in s or "white bass" in s:
        return "white_bass"
    if "striped" in s or "hybrid" in s:
        return "striped_hybrid"
    if "crappie" in s:
        return "crappie"
    if "catfish, blue" in s or "blue catfish" in s:
        return "blue_catfish"
    if "catfish, channel" in s or "channel catfish" in s:
        return "channel_catfish"
    if "catfish, flathead" in s or "flathead catfish" in s:
        return "flathead_catfish"
    if "walleye" in s or "saugeye" in s or "sauger" in s:
        return "walleye_family"
    if "paddlefish" in s:
        return "paddlefish"
    if "sunfish" in s:
        return "sunfish"
    if "gar, alligator" in s or "alligator gar" in s:
        return "alligator_gar"

    return "other"



def pool_pattern(diff_from_normal_ft, elevation_delta_24h):
    pool = finite_number(diff_from_normal_ft)
    trend = finite_number(elevation_delta_24h)
    if pool is None:
        level = "Pool position relative to normal is unavailable"
    elif pool >= 1:
        level = f"Pool is {pool:.1f} ft above normal"
    elif pool <= -1:
        level = f"Pool is {abs(pool):.1f} ft below normal"
    else:
        level = "Pool level is near normal"
    if trend is None:
        return level + "; the 24-hour trend is unavailable, so check remaining submerged cover and adjacent breaks."
    if trend <= -.20:
        return level + f" and falling ({trend:+.2f} ft in 24 hours); prioritize remaining submerged cover and its first adjacent break."
    if trend > .10:
        habitat = "check newly inundated cover and its adjacent break" if pool is not None and pool >= 1 else "check cover that is actually submerged and nearby depth transitions"
        return level + f" and rising ({trend:+.2f} ft in 24 hours); {habitat}."
    habitat = "check inundated cover and adjacent drops" if pool is not None and pool >= 1 else "check remaining submerged cover, forage and depth transitions"
    return level + f" with little measured change ({trend:+.2f} ft in 24 hours); {habitat}."


def flow_context(inflow_cfs, release_cfs):
    inflow, release = finite_number(inflow_cfs), finite_number(release_cfs)
    parts = []
    if inflow is not None and inflow > 0:
        parts.append(f"reported inflow is {inflow:,.0f} CFS; check the inflow corridor only where local current and forage are evident")
    if release is not None and release > 0:
        parts.append(f"reported dam release is {release:,.0f} CFS; assess an accessible tailwater separately from reservoir banks")
    if parts:
        text = "; ".join(parts)
        return text[0].upper() + text[1:] + "."
    if inflow is None or release is None:
        return "Flow telemetry is incomplete; local current is unverified."
    return "Reported inflow and dam release are zero; wind-driven or localized current may still occur."


def temperature_context(water_temp_f, estimated, light):
    wt = finite_number(water_temp_f)
    if wt is None:
        return "Water temperature is unavailable; confirm it locally before choosing a temperature-dependent pattern."
    label = "Estimated water temperature" if estimated else "Measured surface-water temperature"
    base = f"{label} is near {wt:.0f}°F"
    if light == 'night':
        return base + "; at night, check forage near cover and depth transitions and match presentation to the target species."
    if wt >= 80:
        return base + "; check shade and forage at usable depths, staying above oxygen-poor layers if the lake is stratified."
    if wt < 50:
        return base + "; start with controlled presentations near remaining cover and concentrated forage."
    return base + "; locate forage and remaining submerged cover before choosing shallow or deeper presentations."


def species_tactic(species, water_temp_f, pool_ft, trend_ft, light, month, estimated):
    family = _species_family(species)
    wt, pool, trend = map(finite_number, (water_temp_f, pool_ft, trend_ft))
    profiles = {
        'largemouth': 'work remaining submerged cover and secondary points with jigs, Texas rigs or baitfish imitations',
        'smallmouth': 'check rocky points, bluff transitions and humps with tubes, Ned rigs or jerkbaits',
        'spotted': 'check main-lake points and bluff transitions with small swimbaits or finesse jigs',
        'white_bass': 'locate bait schools near points and humps, then match their depth with small swimbaits or spoons',
        'striped_hybrid': 'locate open-water bait schools and channel edges, then match their depth with shad, swimbaits or spoons',
        'crappie': 'locate fish around submerged brush, timber or other available cover; present small jigs or minnows at or just above their depth',
        'blue_catfish': 'check channel-adjacent flats and ledges with fresh cut shad',
        'channel_catfish': 'check riprap and creek-channel edges with cut or prepared bait',
        'flathead_catfish': 'check submerged timber and channel bends with legal live bait, especially during low-light or overnight periods',
        'walleye_family': 'check riprap, points and breaklines with jig-and-minnow rigs or crankbaits, especially in low light',
        'paddlefish': 'check current ODWC location and method rules before considering a paddlefish outing',
        'sunfish': 'check remaining shallow submerged cover with small jigs or worms',
        'alligator_gar': 'check current species rules and available channel or backwater habitat before selecting legal tackle',
        'other': 'locate forage and submerged structure, then adjust depth and presentation to observed fish'
    }
    text = profiles.get(family, profiles['other'])
    if family == 'largemouth' and pool is not None and pool >= 1 and trend is not None and trend > .10:
        text = 'check newly inundated shoreline cover and its first adjacent break with jigs, Texas rigs or baitfish imitations'
    if wt is not None and wt < 50 and family in ('largemouth','smallmouth','spotted','crappie','walleye_family'):
        text += '; begin with controlled retrieves or pauses and adjust to the fish response'
    if wt is not None and wt >= 80 and family in ('largemouth','smallmouth','spotted','crappie','striped_hybrid','walleye_family'):
        text += '; locate forage at usable depths rather than assuming the deepest water has adequate oxygen'
    if family == 'crappie' and month in (3,4,5) and wt is not None and 55 <= wt <= 65:
        basis = 'the estimated temperature suggests' if estimated else 'the measured surface temperature suggests'
        text += f'; {basis} possible spawning conditions, so also check suitable covered spawning areas without assuming fish are nesting'
    if light == 'night' and family in ('largemouth','walleye_family','blue_catfish','channel_catfish'):
        text += '; check feeding areas next to cover or breaks during this overnight period'
    return text


def build_recommended_tactic(
    ranked_species, water_temp_f, diff_from_normal_ft, elevation_delta_24h,
    inflow_cfs, release_cfs, pressure_delta_hpa, wind_speed_mph, cloud_cover_pct,
    precipitation_in, dt_val, water_temp_is_estimated=False, lon=-97.5, lat=35.5
):
    """Current-condition guidance; shared hydrology interpretation with strategy."""
    light = solar_context(dt_val, lon, lat) if dt_val is not None else 'unknown'
    month = utc(dt_val).astimezone(ZoneInfo('America/Chicago')).month if dt_val is not None else None
    parts = [f"For {format_species_name(species)}, {species_tactic(species, water_temp_f, diff_from_normal_ft, elevation_delta_24h, light, month, water_temp_is_estimated)}."
             for species in (ranked_species or [])[:3]]
    parts.append(pool_pattern(diff_from_normal_ft, elevation_delta_24h))
    return " ".join(parts)


def build_tactical_strategy(
    ranked_species, water_temp_f, diff_from_normal_ft, elevation_delta_24h,
    inflow_cfs, release_cfs, pressure_delta_hpa, wind_speed_mph, cloud_cover_pct,
    precipitation_in, seasonal_phase, solunar_window,
    water_temp_is_estimated=False, dt_val=None, lon=-97.5, lat=35.5
):
    """A short action plan; telemetry and species details live in their own cards."""
    light = solar_context(dt_val, lon, lat) if dt_val is not None else 'unknown'
    pool = finite_number(diff_from_normal_ft)
    trend = finite_number(elevation_delta_24h)
    wt = finite_number(water_temp_f)
    inflow = finite_number(inflow_cfs)
    release = finite_number(release_cfs)
    wind = finite_number(wind_speed_mph)
    if trend is not None and trend <= -.20:
        location = 'Start at the first break beside remaining submerged cover as water falls.'
    elif pool is not None and pool >= 1 and trend is not None and trend > .10:
        location = 'Check newly inundated cover, then its first adjacent break.'
    elif pool is None or trend is None:
        location = 'Locate bait and confirm which cover is submerged before choosing a bank or depth.'
    elif pool <= -1:
        location = 'Start on channel edges and remaining submerged cover near bait.'
    else:
        location = 'Locate bait around submerged cover, points and nearby breaks.'
    if light == 'night':
        presentation = 'At night, check feeding areas beside cover and match your presentation to the depth of the fish.'
    elif wt is not None and wt >= 80:
        presentation = ('Confirm warm-water conditions, then check shade and forage above any oxygen-poor layer.'
                        if water_temp_is_estimated else
                        'Check shade and forage at usable depths above any oxygen-poor layer.')
    elif wt is not None and wt < 50:
        presentation = ('Confirm cold-water conditions, then start with controlled retrieves and pauses.'
                        if water_temp_is_estimated else
                        'Start with controlled retrieves and pauses, adjusting to the fish response.')
    else:
        presentation = 'Match your presentation to the depth of the fish and adjust speed to their response.'
    if inflow is not None and inflow > 0 and release is not None and release > 0:
        next_step = 'Check inflow seams or an accessible tailwater only where current and forage are evident.'
    elif inflow is not None and inflow > 0:
        next_step = 'Check the inflow corridor for visible current seams and concentrated bait.'
    elif release is not None and release > 0:
        next_step = 'Where dam release creates current, assess an accessible tailwater separately from reservoir banks.'
    elif light != 'night' and wind is not None and wind >= 7:
        next_step = 'Try wind-affected points where bait is present and you can control the presentation.'
    else:
        next_step = 'If the area is empty, move to the next cover or break holding bait.'
    parts = [location, presentation, next_step]
    return ' '.join(parts)


def fresh_reading(row):
    row=dict(row)
    quality=row.get('quality') or {}
    stale=[]
    now=datetime.now(timezone.utc)
    for metric in ['air_temp_f','relative_humidity_pct','surface_pressure_hpa','wind_speed_mph',
                   'wind_gust_mph','wind_direction_deg','cloud_cover_pct','uv_index','pressure_delta_3h',
                   'precipitation_in','precipitation_1h_in','water_temp_f','elevation_ft',
                   'diff_from_normal_ft','inflow_cfs','release_cfs']:
        meta=quality.get('precipitation_1h_in' if metric=='precipitation_in' else metric,{})
        source=meta.get('source')
        age=1 if source=='weather' else (6 if source=='usgs' else 4)
        if not is_fresh(meta.get('observed_at'),now,age):
            if row.get(metric) is not None:
                stale.append(metric)
            row[metric]=None
    row['quality']=quality
    row['stale_or_unverified_metrics']=stale
    return row


@app.get("/api/lakes/{lake_code}/analysis")
def get_lake_analysis(lake_code: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT name, latitude, longitude, normal_pool_ft, special_regulations, target_species FROM lakes WHERE lake_code = %s", (lake_code,))
    lake_meta = cur.fetchone()

    if not lake_meta:
        cur.close()
        conn.close()
        raise HTTPException(
            status_code=404,
            detail="Lake not found"
        )

    trend_query = """
    WITH latest AS (
        SELECT
            timestamp,
            lake_code,
            elevation_ft,
            diff_from_normal_ft,
            release_cfs,
            inflow_cfs,
            water_temp_f,
            air_temp_f,
            wind_speed_mph,
            wind_gust_mph,
            wind_direction_deg,
            surface_pressure_hpa,
            dissolved_oxygen_mg_l,
            conductance_us_cm,
            ph,
            cloud_cover_pct, precipitation_1h_in AS precipitation_in, uv_index,
            quality, relative_humidity_pct, pressure_delta_3h
        FROM lake_readings
        WHERE lake_code = %s
        ORDER BY timestamp DESC
        LIMIT 1
    ),
    recent_stats AS (
        SELECT
            AVG(air_temp_f) FILTER (
                WHERE air_temp_f IS NOT NULL
            ) AS recent_air_temp_f,

            AVG(surface_pressure_hpa) FILTER (
                WHERE timestamp >= NOW() - INTERVAL '2 hours'
            ) AS press_avg_now,

            AVG(surface_pressure_hpa) FILTER (
                WHERE timestamp >= NOW() - INTERVAL '5 hours'
                  AND timestamp <= NOW() - INTERVAL '3 hours'
            ) AS press_avg_3h

        FROM lake_readings
        WHERE lake_code = %s
          AND timestamp >= NOW() - INTERVAL '96 hours'
    ),
    h24 AS (
        SELECT
            surface_pressure_hpa AS press_24h_ago,
            elevation_ft AS elev_24h_ago
        FROM lake_readings
        WHERE lake_code = %s
          AND timestamp <= (
              SELECT timestamp
              FROM latest
          ) - INTERVAL '24 hours'
          AND timestamp >= (SELECT timestamp FROM latest) - INTERVAL '25 hours'
          AND elevation_ft IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    )
    SELECT
        latest.*,

        ROUND(
            COALESCE(
                rs.press_avg_now,
                latest.surface_pressure_hpa
            )::numeric,
            1
        ) AS cur_press_smoothed,

        ROUND(
            COALESCE(
                rs.press_avg_3h,
                latest.surface_pressure_hpa
            )::numeric,
            1
        ) AS press_3h_ago,

        COALESCE(
            h24.press_24h_ago,
            latest.surface_pressure_hpa
        ) AS press_24h_ago,

        h24.elev_24h_ago AS elev_24h_ago,

        rs.recent_air_temp_f

    FROM latest
    LEFT JOIN recent_stats rs ON TRUE
    LEFT JOIN h24 ON TRUE;
    """
    cur.execute(
        trend_query,
        (
            lake_code,
            lake_code,
            lake_code
        )
    )
    row = cur.fetchone() or {}
    row = fresh_reading(row)

    if row and row.get("timestamp"):
        ts = row["timestamp"]
        delta_p = row.get('pressure_delta_3h')
        row['cur_press_smoothed'] = row.get('surface_pressure_hpa')
        row['press_3h_ago'] = (float(row['surface_pressure_hpa'])-float(delta_p)) if delta_p is not None and row.get('surface_pressure_hpa') is not None else None
        w_speed = row.get('wind_speed_mph')
        c_cover = row.get('cloud_cover_pct')
        raw_wt = row.get("water_temp_f")
        if raw_wt is not None and float(raw_wt or 0) > 0:
            w_temp = float(raw_wt)
            is_estimated_temp = False
        else:
            air_t = float(row.get("air_temp_f") or 78.0)
            m_val = (
                ts.month
                if hasattr(ts, "month")
                else datetime.now(timezone.utc).month
            )

            try:
                recent_air = (
                    float(row.get("recent_air_temp_f"))
                    if row.get("recent_air_temp_f") is not None
                    else air_t
                )
            except (TypeError, ValueError):
                recent_air = air_t

            w_temp = estimate_water_temperature(
                air_t,
                month=m_val,
                recent_air_temp_f=recent_air,
                when=ts
            )

            is_estimated_temp = True

        row["water_temp_f"] = w_temp
        row["water_temp_is_estimated"] = is_estimated_temp

        rel_cfs = float(row["release_cfs"]) if row.get("release_cfs") is not None else None
        inflow_cfs = float(row["inflow_cfs"]) if row.get("inflow_cfs") is not None else None
        precip = float(row["precipitation_in"]) if row.get("precipitation_in") is not None else None

        elev_delta = None
        if row.get("elevation_ft") is not None and row.get("elev_24h_ago") is not None:
            elev_delta = float(row["elevation_ft"]) - float(row["elev_24h_ago"])
            row["elevation_delta_24h"] = round(elev_delta, 2)
        else:
            row["elevation_delta_24h"] = None

        lat_val = float(lake_meta["latitude"]) if lake_meta.get("latitude") is not None else 35.5
        lon_val = float(lake_meta["longitude"]) if lake_meta.get("longitude") is not None else -97.5

        score, rating, sol_win, bite_factors = calculate_bite_score(
            delta_press=delta_p,
            wind_speed=w_speed,
            cloud_cover=c_cover,
            dt_val=datetime.now(timezone.utc),
            lon=lon_val,
            lat=lat_val,
            water_temp_f=w_temp,
            release_cfs=rel_cfs,
            diff_from_normal_ft=row.get("diff_from_normal_ft"),
            elev_delta_24h=elev_delta,
            precip_in=precip,
            include_factors=True
        )

        row["lake_name"] = lake_meta.get("name")
        row['model_version']=MODEL_VERSION
        row['score_type']='Unvalidated relative conditions index; not catch probability'
        row['data_status']='available' if score is not None else 'unavailable'
        row["bite_score"] = score
        row["bite_rating"] = rating
        row["solunar_window"] = sol_win
        row["bite_factors"] = bite_factors
        _month_for_phase = ts.month if hasattr(ts, "month") else datetime.now(timezone.utc).month
        _seasonal = calculate_spawn_phase(w_temp, _month_for_phase)
        row["seasonal_phase"] = _seasonal.get("phase")
        row["lunar"] = get_lunar_telemetry(datetime.now(timezone.utc))

        try:
            if score is None:
                raise ValueError('Fresh essential inputs unavailable')
            _wt = float(row.get("water_temp_f") or w_temp or 75.0)
            _diff = finite_number(row.get("diff_from_normal_ft"))
            _month = ts.month if hasattr(ts, "month") else datetime.now(timezone.utc).month

            lake_species = (lake_meta or {}).get("target_species") or []
            if isinstance(lake_species, str):
                lake_species = [s.strip() for s in lake_species.split(",") if s.strip()]

            ranked_species, species_ranking = rank_target_species(
                species_list=lake_species,
                water_temp_f=_wt,
                month=_month,
                diff_from_normal_ft=_diff,
                elevation_delta_24h=row.get("elevation_delta_24h"),
                inflow_cfs=inflow_cfs,
                release_cfs=rel_cfs,
                pressure_delta_hpa=delta_p,
                wind_speed_mph=w_speed,
                cloud_cover_pct=c_cover,
                precipitation_in=precip,
                top_n=3
            )

            row["target_species"] = ranked_species
            row["species_ranking"] = species_ranking

            recommended_tactic = build_recommended_tactic(
                ranked_species=ranked_species,
                water_temp_f=_wt,
                diff_from_normal_ft=_diff,
                elevation_delta_24h=row.get("elevation_delta_24h"),
                inflow_cfs=inflow_cfs,
                release_cfs=rel_cfs,
                pressure_delta_hpa=delta_p,
                wind_speed_mph=w_speed,
                cloud_cover_pct=c_cover,
                precipitation_in=precip,
                dt_val=datetime.now(timezone.utc),
                water_temp_is_estimated=is_estimated_temp, lon=lon_val, lat=lat_val
            )

            tactical_strategy = build_tactical_strategy(
                ranked_species=ranked_species,
                water_temp_f=_wt,
                diff_from_normal_ft=_diff,
                elevation_delta_24h=row.get("elevation_delta_24h"),
                inflow_cfs=inflow_cfs,
                release_cfs=rel_cfs,
                pressure_delta_hpa=delta_p,
                wind_speed_mph=w_speed,
                cloud_cover_pct=c_cover,
                precipitation_in=precip,
                seasonal_phase=row.get("seasonal_phase"),
                solunar_window=sol_win,
                water_temp_is_estimated=is_estimated_temp,
                dt_val=datetime.now(timezone.utc), lon=lon_val, lat=lat_val
            )

            row["recommended_tactic"] = recommended_tactic
            row["analysis_commentary"] = tactical_strategy

        except Exception as _e:
            if score is None:
                logger.warning('Fresh essential inputs unavailable; species ranking withheld', extra={
                    'event': 'analysis_unavailable', 'lake': lake_code})
            else:
                logger.exception('Tactical analysis failed; using fallback strategy', extra={
                    'event': 'analysis_failed', 'lake': lake_code})
            fallback_species = (
                (lake_meta or {}).get("target_species")
                or []
            )

            if isinstance(fallback_species, str):
                fallback_species = [
                    item.strip()
                    for item in fallback_species.split(",")
                    if item.strip()
                ]

            row["target_species"] = (
                fallback_species[:3]
                if isinstance(fallback_species, list)
                else []
            )

            row["species_ranking"] = []

            row["recommended_tactic"] = (
                "Use lake structure, current, forage, and depth "
                "transitions while species ranking is unavailable."
            )

            row["analysis_commentary"] = (
                "Species ranking is unavailable; use the lake-listed "
                "species and current telemetry as the primary guide."
            )

    if row:
        row['nominal_pool_ft']=lake_meta.get('normal_pool_ft')
        reference=(row.get('quality',{}).get('diff_from_normal_ft',{}).get('source_metadata',{}).get('reference_ft'))
        row["normal_pool_ft"] = (
            float(reference)
            if (
                reference is not None
            )
            else None
        )

        row["special_regulations"] = (
            (lake_meta or {}).get("special_regulations")
            or (
                "Regulations unavailable; verify current rules with ODWC."
            )
        )

        # ----------------------------------------------------
        # Source freshness -- same DB connection as analysis.
        # ----------------------------------------------------
        source_freshness = {}

        try:
            cur.execute(
                """
                SELECT source, last_success
                FROM lake_source_status
                WHERE lake_code = %s
                ORDER BY source;
                """,
                (lake_code,)
            )

            for item in cur.fetchall():
                source_freshness[item["source"]] = (
                    item["last_success"]
                )

        except psycopg2.errors.UndefinedTable:
            conn.rollback()

        except Exception as exc:
            conn.rollback()

            logger.exception('Source freshness lookup failed', extra={'event': 'source_freshness_failed', 'lake': lake_code})

        if row.get("timestamp"):
            source_freshness["telemetry"] = (
                row["timestamp"]
            )

        row['source_retrievals']=source_freshness
        observed_by_source={}
        for meta in (row.get('quality') or {}).values():
            if meta.get('source') and meta.get('observed_at'):
                observed_by_source.setdefault(meta['source'],[]).append(utc(meta['observed_at']))
        row['source_freshness']={source:min(times) for source,times in observed_by_source.items()}
        row['source_freshness']['telemetry']=row.get('timestamp')

        # ----------------------------------------------------
        # Active lake alerts -- same DB connection as analysis.
        # ----------------------------------------------------
        lake_alerts = []

        try:
            cur.execute(
                """
                SELECT
                    project_key,
                    project_name,
                    source_name,
                    source_url,
                    summary,
                    status,
                    source_modified,
                    first_seen,
                    last_seen,
                    changed_at,
                    is_active
                FROM lake_project_alerts
                WHERE lake_code = %s
                  AND is_active = TRUE
                ORDER BY changed_at DESC;
                """,
                (lake_code.upper(),)
            )

            lake_alerts = [
                dict(item)
                for item in cur.fetchall()
            ]

        except psycopg2.errors.UndefinedTable:
            conn.rollback()

        except Exception as exc:
            conn.rollback()

            logger.exception('Lake alert lookup failed', extra={'event': 'lake_alerts_failed', 'lake': lake_code})

        row["lake_alerts"] = lake_alerts

    # Internal calculation fields are not part of the public API.
    for internal_key in (
        "recent_air_temp_f",
        "elev_24h_ago",
        "press_24h_ago",
        "conductance_us_cm",
        "ph"
    ):
        row.pop(
            internal_key,
            None
        )

    record_prediction(conn,lake_code,'current',row)
    cur.close()
    conn.close()

    return row

@app.get("/api/lakes/{lake_code}/forecast")
def get_lake_forecast(lake_code: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        """
        SELECT latitude, longitude, name
        FROM lakes
        WHERE lake_code = %s
        """,
        (lake_code,)
    )

    lake = cur.fetchone()

    if (
        not lake
        or lake.get("latitude") is None
        or lake.get("longitude") is None
    ):
        cur.close()
        conn.close()

        raise HTTPException(
            status_code=404,
            detail="Lake coordinates not found"
        )

    cur.execute(
        """
        SELECT
            r.timestamp, r.quality,
            r.water_temp_f,
            r.air_temp_f,
            r.release_cfs,
            r.diff_from_normal_ft,
            r.elevation_ft,
            (
                SELECT AVG(avgsrc.air_temp_f)
                FROM lake_readings avgsrc
                WHERE avgsrc.lake_code = r.lake_code
                  AND avgsrc.timestamp >= NOW() - INTERVAL '96 hours'
                  AND avgsrc.air_temp_f IS NOT NULL
            ) AS recent_air_temp_f,
            (
                r.elevation_ft - (
                    SELECT old.elevation_ft
                    FROM lake_readings old
                    WHERE old.lake_code = r.lake_code
                      AND old.timestamp
                          <= r.timestamp - INTERVAL '24 hours'
                      AND old.timestamp >= r.timestamp - INTERVAL '25 hours'
                      AND old.elevation_ft IS NOT NULL
                    ORDER BY old.timestamp DESC
                    LIMIT 1
                )
            ) AS elevation_delta_24h
        FROM lake_readings r
        WHERE r.lake_code = %s
        ORDER BY r.timestamp DESC
        LIMIT 1
        """,
        (lake_code,)
    )

    latest_data = fresh_reading(cur.fetchone() or {})

    cur.close()
    conn.close()

    lat = float(lake["latitude"])
    lon = float(lake["longitude"])

    raw_water_temp = latest_data.get("water_temp_f")

    if (
        raw_water_temp is not None
        and float(raw_water_temp or 0) > 0
    ):
        water_temp_baseline = float(raw_water_temp)

    else:
        air_temp = latest_data.get("air_temp_f")

        try:
            recent_air_temp = (
                float(latest_data.get("recent_air_temp_f"))
                if latest_data.get("recent_air_temp_f") is not None
                else None
            )
        except (TypeError, ValueError):
            recent_air_temp = None

        forecast_now = datetime.now(
            ZoneInfo("America/Chicago")
        )

        forecast_month = forecast_now.month

        water_temp_baseline = estimate_water_temperature(
            air_temp,
            month=forecast_month,
            recent_air_temp_f=recent_air_temp,
            when=forecast_now
        )

    release_baseline = latest_data.get("release_cfs")
    pool_diff_baseline = latest_data.get(
        "diff_from_normal_ft"
    )
    elevation_delta_baseline = latest_data.get(
        "elevation_delta_24h"
    )

    data, forecast_cache_status, forecast_cache_age_seconds = (
        fetch_open_meteo_forecast_cached(
            lake_code=lake_code,
            lat=lat,
            lon=lon,
        )
    )

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    pressures = hourly.get("surface_pressure", [])
    winds = hourly.get("wind_speed_10m", [])
    wind_gusts = hourly.get("wind_gusts_10m", [0.0] * len(times))
    wind_dirs = hourly.get("wind_direction_10m", [])
    temps = hourly.get("temperature_2m", [])
    humidities = hourly.get("relative_humidity_2m", [None] * len(times))
    clouds = hourly.get("cloud_cover", [])
    precip_probs = hourly.get("precipitation_probability", [])
    precips = hourly.get("precipitation", [None] * len(times))

    forecast_cards = []
    for i in range(len(times)):
        p_now = pressures[i] if i<len(pressures) else None
        delta_p = pressure_delta(times,pressures,times[i])
        w_speed = winds[i] if i<len(winds) else None
        c_cover = clouds[i] if i<len(clouds) else None
        pr_val = float(precips[i]) if i < len(precips) and precips[i] is not None else None
        
        score, rating, sol_win, _ = calculate_bite_score(
            delta_press=delta_p,
            wind_speed=w_speed,
            cloud_cover=c_cover,
            dt_val=times[i],
            lon=lon,
            lat=lat,
            water_temp_f=water_temp_baseline,
            release_cfs=release_baseline,
            diff_from_normal_ft=pool_diff_baseline,
            elev_delta_24h=elevation_delta_baseline,
            precip_in=pr_val,
            include_factors=False
        )

        g_val = (
            float(wind_gusts[i])
            if i < len(wind_gusts)
            and wind_gusts[i] is not None
            else w_speed
        )
        forecast_cards.append({
            "time": utc(times[i]).isoformat(),
            "air_temp_f": temps[i] if i<len(temps) else None,

            "relative_humidity_pct": int(humidities[i]) if i < len(humidities) and humidities[i] is not None else None,
            "surface_pressure_hpa": p_now,
            "delta_pressure_3h": delta_p,
            "wind_speed_mph": w_speed,
            "wind_gust_mph": round(g_val, 1),
            "wind_direction_deg": wind_dirs[i] if i<len(wind_dirs) else None,
            "cloud_cover_pct": c_cover,
            "precip_prob_pct": precip_probs[i] if i<len(precip_probs) else None,
            "bite_score": score,
            "rating": rating,
            "solunar_window": sol_win
        })

    now=datetime.now(timezone.utc)
    visible_forecast = [c for c in forecast_cards if utc(c['time'])>=now][:48]
    result = {
        "lake_code": lake_code,
        "lake_name": lake["name"],
        "forecast": visible_forecast,
        "best_window": calculate_best_window(visible_forecast),
        "hydrology_assumption": "Current reservoir hydrology is held constant across the forecast window.",
        "forecast_cache_status": forecast_cache_status,
        "forecast_cache_age_seconds": forecast_cache_age_seconds,
        "model_version":MODEL_VERSION,
        "score_type":"Unvalidated relative conditions index; not catch probability",
        "inputs":latest_data,
    }
    archive=get_db()
    try:
        record_prediction(archive,lake_code,'forecast',dict(forecast=visible_forecast,inputs=latest_data,weather=data))
    finally:
        archive.close()
    return result


@app.get("/api/lakes/{lake_code}/history")
def get_history(
    lake_code: str,
    response: Response,
    days: int = 7
):
    response.headers["Cache-Control"] = (
        "no-cache, no-store, must-revalidate"
    )

    # Keep pathological or accidental requests bounded while
    # preserving the frontend's normal 7/30/90-day behavior.
    days = max(1, min(int(days), 365))

    bucket_interval = "1 hour"

    conn = get_db()

    try:
        cur = conn.cursor(
            cursor_factory=RealDictCursor
        )

        query = f"""
            SELECT
                time_bucket(
                    '{bucket_interval}',
                    timestamp
                ) AS bucket,

                ROUND(
                    AVG(elevation_ft)::numeric,
                    2
                ) AS elevation_ft,

                ROUND(
                    AVG(water_temp_f)::numeric,
                    1
                ) AS water_temp_f,

                ROUND(
                    AVG(air_temp_f)::numeric,
                    1
                ) AS air_temp_f,

                ROUND(
                    AVG(surface_pressure_hpa)::numeric,
                    1
                ) AS surface_pressure_hpa,

                ROUND(
                    AVG(release_cfs)::numeric,
                    1
                ) AS release_cfs,

                ROUND(
                    AVG(inflow_cfs)::numeric,
                    1
                ) AS inflow_cfs,

                ROUND(
                    AVG(dissolved_oxygen_mg_l)::numeric,
                    2
                ) AS dissolved_oxygen_mg_l,

                ROUND(
                    AVG(cloud_cover_pct)::numeric,
                    0
                ) AS cloud_cover_pct,
                ROUND(AVG(elevation_ft-diff_from_normal_ft)::numeric,2) AS normal_pool_ft

            FROM lake_readings

            WHERE lake_code = %s
              AND timestamp >= NOW() - INTERVAL '{days+4} days'

            GROUP BY 1
            ORDER BY 1 ASC;
        """

        cur.execute(
            query,
            (lake_code,)
        )

        rows = cur.fetchall()

    finally:
        conn.close()

    # ----------------------------------------------------
    # Fill missing historical reservoir-temperature
    # observations with the same model used by live
    # analysis.
    #
    # A rolling 96-hour air-temperature window keeps this
    # O(n). Real reservoir telemetry always wins.
    # ----------------------------------------------------

    air_window = deque()
    air_window_sum = 0.0

    for row in rows:
        bucket = row.get("bucket")
        air_temp = row.get("air_temp_f")

        try:
            air_temp = (
                float(air_temp)
                if air_temp is not None
                else None
            )

        except (TypeError, ValueError):
            air_temp = None

        if isinstance(bucket, datetime):
            bucket_dt = bucket

        elif bucket is not None:
            try:
                bucket_dt = datetime.fromisoformat(
                    str(bucket).replace(
                        "Z",
                        "+00:00"
                    )
                )

            except (TypeError, ValueError):
                bucket_dt = None

        else:
            bucket_dt = None

        # Maintain trailing 96-hour atmospheric window.
        if bucket_dt is not None:

            while air_window:
                age_hours = (
                    bucket_dt
                    - air_window[0][0]
                ).total_seconds() / 3600.0

                if age_hours <= 96.0:
                    break

                _, old_air = air_window.popleft()
                air_window_sum -= old_air

            if air_temp is not None:
                air_window.append(
                    (
                        bucket_dt,
                        air_temp
                    )
                )

                air_window_sum += air_temp

        recent_air = (
            air_window_sum
            / len(air_window)
            if air_window
            else air_temp
        )

        raw_water_temp = row.get(
            "water_temp_f"
        )

        try:
            has_measured_temp = (
                raw_water_temp is not None
                and float(raw_water_temp) > 0
            )

        except (TypeError, ValueError):
            has_measured_temp = False

        # Real reservoir telemetry always wins.
        if has_measured_temp:
            row["water_temp_f"] = round(
                float(raw_water_temp),
                1
            )

            row[
                "water_temp_is_estimated"
            ] = False

            continue

        historical_month = (
            bucket_dt.month
            if bucket_dt is not None
            else datetime.now(
                timezone.utc
            ).month
        )

        row["water_temp_f"] = (
            estimate_water_temperature(
                air_temp,
                month=historical_month,
                recent_air_temp_f=recent_air,
                when=bucket_dt
            )
        )

        row[
            "water_temp_is_estimated"
        ] = True

    cutoff=datetime.now(timezone.utc)-timedelta(days=days)
    return [r for r in rows if utc(r['bucket'])>=cutoff]
