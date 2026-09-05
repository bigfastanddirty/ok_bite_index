
def estimate_lake_water_temp(air_temp_f, month=None, mean_depth_ft=25.0):
    if air_temp_f is None:
        return 78.0
    try:
        val = float(air_temp_f)
    except (ValueError, TypeError):
        return 78.0

    if month is None:
        from datetime import datetime
        month = datetime.utcnow().month

    if 6 <= month <= 8:
        base_temp = (val * 0.75) + 20.0
    elif 9 <= month <= 11:
        base_temp = (val * 0.65) + 26.0
    elif 3 <= month <= 5:
        base_temp = (val * 0.60) + 22.0
    else:
        base_temp = (val * 0.50) + 25.0

    damping = max(0.85, 1.0 - (float(mean_depth_ft) / 200.0))
    est = round(base_temp * damping, 1)
    return min(max(est, 42.0), 89.0)

def get_lunar_telemetry(dt=None):
    if dt is None:
        dt = datetime.now(timezone.utc)
    
    # Astronomical Moon Phase calculation using synodic epoch
    epoch = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    diff = (dt - epoch).total_seconds()
    synodic_period = 29.53058867 * 86400
    phase_ratio = (diff % synodic_period) / synodic_period
    
    moon_age_days = round(phase_ratio * 29.53058867, 1)
    illumination_pct = round(0.5 * (1 - math.cos(2 * math.pi * phase_ratio)) * 100, 1)
    
    if phase_ratio < 0.03 or phase_ratio >= 0.97:
        phase_name = "New Moon"
        phase_icon = "🌑"
    elif phase_ratio < 0.22:
        phase_name = "Waxing Crescent"
        phase_icon = "🌒"
    elif phase_ratio < 0.28:
        phase_name = "First Quarter"
        phase_icon = "🌓"
    elif phase_ratio < 0.47:
        phase_name = "Waxing Gibbous"
        phase_icon = "🌔"
    elif phase_ratio < 0.53:
        phase_name = "Full Moon"
        phase_icon = "🌕"
    elif phase_ratio < 0.72:
        phase_name = "Waning Gibbous"
        phase_icon = "🌖"
    elif phase_ratio < 0.78:
        phase_name = "Last Quarter"
        phase_icon = "🌗"
    else:
        phase_name = "Waning Crescent"
        phase_icon = "🌘"

    return {
        "phase_name": phase_name,
        "phase_icon": phase_icon,
        "illumination_pct": illumination_pct,
        "moon_age_days": moon_age_days
    }



def estimate_water_temperature(air_temp_f, month=None):
    """
    Empirical limnological equilibrium water temp model for shallow-to-mid depth Oklahoma reservoirs.
    Uses seasonal thermal inertia and heat exchange regression.
    """
    if month is None:
        month = datetime.now(timezone.utc).month
    
    base_air = air_temp_f if air_temp_f is not None else 70.0
    
    # Oklahoma seasonal equilibrium offsets
    if month in [12, 1, 2]:  # Winter
        est = (base_air * 0.45) + 26.0
        return round(max(38.0, min(54.0, est)), 1)
    elif month in [3, 4, 5]:  # Spring warming lag
        est = (base_air * 0.60) + 22.0
        return round(max(48.0, min(74.0, est)), 1)
    elif month in [6, 7, 8, 9]:  # Summer peak thermal stratification
        est = (base_air * 0.55) + 36.0
        return round(max(72.0, min(89.0, est)), 1)
    else:  # Fall cooling
        est = (base_air * 0.65) + 20.0
        return round(max(52.0, min(72.0, est)), 1)

import os
import math
import json
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI, Response, HTTPException
from fastapi.responses import HTMLResponse
from typing import Optional
from datetime import datetime, timezone, timedelta

app = FastAPI()

DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def calculate_time_of_day_factor(dt_val):
    """
    Evaluates diurnal solar periods (CDT timezone):
    - Dawn / Sunrise (5:30 AM - 8:30 AM): +15 pts
    - Dusk / Sunset (6:30 PM - 9:00 PM): +15 pts
    - Midday High Sun (11:00 AM - 3:30 PM): -10 pts
    - Night Window (10:00 PM - 4:30 AM): +5 pts
    """
    try:
        if isinstance(dt_val, str):
            dt = datetime.fromisoformat(dt_val.replace('Z', '+00:00'))
        elif isinstance(dt_val, datetime):
            dt = dt_val
        else:
            dt = datetime.now(timezone.utc)

        # Convert to Central Time (-5 hours CDT)
        local_dt = dt.astimezone(timezone(timedelta(hours=-5)))
        hour_float = local_dt.hour + (local_dt.minute / 60.0)

        if 5.5 <= hour_float <= 8.5:
            return 15.0  # Dawn peak
        elif 18.5 <= hour_float <= 21.0:
            return 15.0  # Dusk peak
        elif 11.0 <= hour_float <= 15.5:
            return -10.0 # Midday high light stress
        elif hour_float >= 22.0 or hour_float <= 4.5:
            return 5.0   # Night roaming window
        else:
            return 0.0   # Mid-morning / late afternoon baseline
    except Exception:
        return 0.0

def calculate_spawn_phase(water_temp_f, month, diff_ft=0.0):
    """
    Estimate the lake's broad seasonal fishing phase.

    Water temperature is the primary biological signal.
    Calendar month acts as a seasonal guardrail for spawn-related phases.
    """
    if water_temp_f is None:
        water_temp_f = 70.0

    wt = float(water_temp_f)

    # Winter / cold-water period
    if wt < 50:
        return {
            "phase": "Winter Torpor / Staging",
            "tactic": "Metabolism suppressed. Baitfish suspended over deep wintering river channels and main-lake basins."
        }

    # Early spring pre-spawn
    if 50 <= wt < 60 and month in (2, 3, 4):
        return {
            "phase": "Pre-Spawn Movement",
            "tactic": "Bass and crappie staging along secondary points and creek-channel contours leading into protected pockets."
        }

    # Primary spawning window
    if 60 <= wt <= 72 and month in (3, 4, 5):
        return {
            "phase": "Active Spawn / Bedding",
            "tactic": "Shallow nest guarding in protected coves, flat pockets, and hard-bottom sand/gravel banks."
        }

    # Post-spawn recovery
    if 72 < wt < 78 and month in (5, 6):
        return {
            "phase": "Post-Spawn Recovery",
            "tactic": "Fish sliding toward outside weedlines, brush piles, secondary drops, and shad-oriented structure."
        }

    # Hot-water summer pattern.
    # Temperature intentionally overrides the calendar so hot September
    # reservoirs are not prematurely classified as fall.
    if wt >= 80:
        return {
            "phase": "Summer Thermal Refuge",
            "tactic": "Predators favoring current breaks, deeper structure, shade, and thermocline-related depth during bright periods."
        }

    # Fall transition begins when the lake actually cools.
    if month in (9, 10, 11) and 60 <= wt < 80:
        return {
            "phase": "Fall Forage Push",
            "tactic": "Predators tracking schooling shad into creek arms, flats, points, and windblown pockets."
        }

    # Late-fall cooling
    if month in (10, 11, 12) and 50 <= wt < 60:
        return {
            "phase": "Late Fall Transition",
            "tactic": "Fish consolidating around channel edges, rock, deeper points, and remaining forage concentrations."
        }

    return {
        "phase": "Transitional",
        "tactic": "Scattered forage holding along primary breaklines, points, and secondary creek structure."
    }

def calculate_solunar_factor(dt_val, lon, lat):
    try:
        lon_f = float(lon)
        lat_f = float(lat)

        if isinstance(dt_val, str):
            dt = datetime.fromisoformat(dt_val.replace('Z', '+00:00'))
        elif isinstance(dt_val, datetime):
            dt = dt_val
        else:
            dt = datetime.now(timezone.utc)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        ts_sec = float(dt.timestamp())
        jd = 2440587.5 + (ts_sec / 86400.0)
        d = jd - 2451545.0
        
        L = (218.316 + 13.176396 * d) % 360.0
        M = (134.963 + 13.064993 * d) % 360.0
        moon_lon = (L + 6.289 * math.sin(math.radians(M))) % 360.0
        
        gmst = (280.46061837 + 360.98564736629 * d) % 360.0
        local_sidereal_time = (gmst + lon_f) % 360.0
        
        hour_angle = ((local_sidereal_time - moon_lon) % 360.0) / 15.0
        
        days_since_new = (jd - 2451549.5) % 29.53058867
        phase_ratio = days_since_new / 29.53058867
        
        dist_to_major_phase = min(abs(phase_ratio - 0.0), abs(phase_ratio - 0.5), abs(phase_ratio - 1.0))
        phase_bonus = 6.0 if dist_to_major_phase < 0.06 else (3.0 if dist_to_major_phase < 0.12 else 0.0)

        dist_to_upper = min(hour_angle, 24.0 - hour_angle)
        dist_to_lower = abs(hour_angle - 12.0)
        min_dist_major = min(dist_to_upper, dist_to_lower)
        min_dist_minor = min(abs(hour_angle - 6.0), abs(hour_angle - 18.0))

        if min_dist_major <= 1.0:
            return 16.0 * (1.0 - (min_dist_major / 1.0)) + phase_bonus, "MAJOR"
        elif min_dist_minor <= 0.75:
            return 10.0 * (1.0 - (min_dist_minor / 0.75)) + phase_bonus, "MINOR"
        else:
            return phase_bonus, "NONE"
    except Exception:
        return 0.0, "NONE"

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

def calculate_master_bite_score(
    pressure,
    delta_press,
    wind_speed,
    cloud_cover,
    dt_val,
    lon,
    lat,
    water_temp_f=None,
    release_cfs=None,
    elev_delta_24h=None,
    precip_in=None
):
    score = 35.0  # Base score adjusted to accommodate time-of-day bonuses

    dp_val = float(delta_press) if delta_press is not None else 0.0
    w_val = float(wind_speed) if wind_speed is not None else 0.0
    c_val = float(cloud_cover) if cloud_cover is not None else 0.0

    # 1. Barometric Pressure
    if dp_val <= -1.2:
        score += 25.0
    elif dp_val <= -0.4:
        score += 15.0
    elif -0.4 < dp_val < 0.6:
        score += 4.0
    elif dp_val >= 1.8:
        score -= 22.0
    elif dp_val >= 0.6:
        score -= 10.0

    # 2. Wind Chop
    if 5.0 <= w_val <= 13.0:
        score += 12.0
    elif 13.0 < w_val <= 18.0:
        score += 6.0
    elif w_val < 4.0:
        score -= 8.0
    else:
        score -= 14.0

    # 3. Cloud Cover
    if c_val >= 65.0:
        score += 8.0
    elif c_val <= 15.0:
        score -= 4.0

    # 4. Time of Day (Diurnal Solar Cycle)
    score += calculate_time_of_day_factor(dt_val)

    # 5. Solunar Major / Minor Window
    sol_score, window_type = calculate_solunar_factor(dt_val, lon, lat)
    score += sol_score

    # 6. Water Temp Metabolism
    score += calculate_water_temp_factor(water_temp_f)

    # 7. Dam Flow
    if release_cfs is not None:
        try:
            if float(release_cfs) > 100.0:
                score += 5.0
        except Exception:
            pass

    # 8. Pool Fluctuation
    if elev_delta_24h is not None:
        try:
            ed = float(elev_delta_24h)
            if ed < -0.3:
                score -= 6.0
            elif 0.1 <= ed <= 0.5:
                score += 4.0
        except Exception:
            pass

    # 9. Precipitation & Inflow Dynamics
    if precip_in is not None:
        try:
            pr = float(precip_in)
            if 0.05 <= pr <= 0.40:
                score += 7.0
            elif 0.40 < pr <= 0.80:
                score += 3.0
            elif pr > 1.20:
                score -= 12.0
        except Exception:
            pass

    final_score = max(5, min(100, int(round(score))))
    
    if final_score >= 80:
        rating = "EPIC"
    elif final_score >= 65:
        rating = "GOOD"
    elif final_score >= 45:
        rating = "FAIR"
    else:
        rating = "TOUGH"

    return final_score, rating, window_type


def calculate_bite_score_breakdown(
    delta_press,
    wind_speed,
    cloud_cover,
    dt_val,
    lon,
    lat,
    water_temp_f=None,
    release_cfs=None,
    elev_delta_24h=None,
    precip_in=None
):
    """Return the same major score contributions used by the bite model."""
    factors = []

    def add(label, points, detail):
        factors.append({
            "label": label,
            "points": int(round(points)),
            "detail": detail
        })

    dp = float(delta_press or 0.0)
    wind = float(wind_speed or 0.0)
    clouds = float(cloud_cover or 0.0)

    if dp <= -1.2:
        add("Pressure trend", 25, "Strong falling-pressure feeding trigger")
    elif dp <= -0.4:
        add("Pressure trend", 15, "Falling pressure favors feeding")
    elif dp < 0.6:
        add("Pressure trend", 4, "Stable pressure")
    elif dp >= 1.8:
        add("Pressure trend", -22, "Rapidly rising pressure")
    else:
        add("Pressure trend", -10, "Rising pressure")

    if 5.0 <= wind <= 13.0:
        add("Wind", 12, "Productive surface chop")
    elif 13.0 < wind <= 18.0:
        add("Wind", 6, "Strong but workable wind")
    elif wind < 4.0:
        add("Wind", -8, "Little surface disturbance")
    else:
        add("Wind", -14, "Excessive wind")

    if clouds >= 65:
        add("Cloud cover", 8, "Low-light conditions")
    elif clouds <= 15:
        add("Cloud cover", -4, "Bright clear conditions")
    else:
        add("Cloud cover", 0, "Moderate cloud cover")

    tod = calculate_time_of_day_factor(dt_val)
    add("Time of day", tod, "Diurnal feeding-window adjustment")

    sol_score, sol_window = calculate_solunar_factor(dt_val, lon, lat)
    add("Solunar", sol_score, f"{sol_window.title()} lunar window")

    wt_score = calculate_water_temp_factor(water_temp_f)
    add("Water temperature", wt_score, "Seasonal metabolism adjustment")

    flow_score = 0
    if release_cfs is not None:
        try:
            if float(release_cfs) > 100.0:
                flow_score = 5
        except Exception:
            pass
    add("Dam flow", flow_score, "Current / tailrace effect")

    pool_score = 0
    if elev_delta_24h is not None:
        try:
            ed = float(elev_delta_24h)
            if ed < -0.3:
                pool_score = -6
            elif 0.1 <= ed <= 0.5:
                pool_score = 4
        except Exception:
            pass
    add("Pool trend", pool_score, "24-hour water-level movement")

    precip_score = 0
    if precip_in is not None:
        try:
            pr = float(precip_in)
            if 0.05 <= pr <= 0.40:
                precip_score = 7
            elif 0.40 < pr <= 0.80:
                precip_score = 3
            elif pr > 1.20:
                precip_score = -12
        except Exception:
            pass
    add("Precipitation", precip_score, "Runoff / disturbance adjustment")

    return factors


def get_source_freshness(lake_code, fallback_timestamp=None):
    """
    Read per-source success timestamps written by the ingestor.
    Falls back to the lake reading timestamp if the status table has not
    been created yet.
    """
    result = {}
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT to_regclass('public.lake_source_status') AS table_name;")
        exists = cur.fetchone()
        if exists and exists.get("table_name"):
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
                result[item["source"]] = item["last_success"]
        cur.close()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()

    if not result and fallback_timestamp:
        result["telemetry"] = fallback_timestamp

    return result


def calculate_best_window(forecast_cards, window_hours=3, horizon_hours=24):
    """Return the strongest rolling fishing window in the next 24 forecast hours."""
    cards = list(forecast_cards or [])[:horizon_hours]
    if not cards:
        return None

    width = max(1, min(int(window_hours), len(cards)))
    best = None

    for i in range(0, len(cards) - width + 1):
        group = cards[i:i + width]
        scores = [float(x.get("bite_score") or 0) for x in group]
        avg_score = sum(scores) / len(scores)
        peak_score = max(scores)

        candidate = {
            "start": group[0].get("time"),
            "end": group[-1].get("time"),
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
    try:
        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(base_dir, "methodology.html")
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(
            content=content,
            headers={
                "Cache-Control": "public, max-age=3600",
            }
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail="Methodology page not found")

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

@app.get("/api/lakes")
def get_lakes(response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    query = """
    SELECT DISTINCT ON (l.lake_code)
        l.lake_code,
        l.name AS lake_name,
        l.latitude,
        l.longitude,
        l.normal_pool_ft AS normal_elevation_ft,
        r.timestamp,
        r.elevation_ft,
        r.diff_from_normal_ft,
        r.water_temp_f,
        r.air_temp_f,
        r.wind_speed_mph,
        r.wind_direction_deg,
        r.surface_pressure_hpa,
        r.dissolved_oxygen_mg_l,
        r.turbidity_fnu,
        r.conductance_us_cm,
        r.ph,
        r.release_cfs, r.inflow_cfs,
        COALESCE(r.cloud_cover_pct, 0.0) AS cloud_cover_pct,
        r.precipitation_in,
        COALESCE(r.uv_index, 0.0) AS uv_index
    FROM lakes l
    LEFT JOIN lake_readings r ON l.lake_code = r.lake_code
    ORDER BY l.lake_code, r.timestamp DESC;
    """
    cur.execute(query)
    raw_rows = cur.fetchall()
    cur.close()
    conn.close()

    rows = []
    for row in raw_rows:
        r_dict = dict(row)
        raw_temp = r_dict.get("water_temp_f")
        if raw_temp is None:
            air = r_dict.get("air_temp_f") or 82.0
            ts = r_dict.get("timestamp")
            m = ts.month if hasattr(ts, "month") else 8
            r_dict["water_temp_f"] = estimate_lake_water_temp(air, month=m)
            r_dict["water_temp_is_estimated"] = True
            r_dict["is_estimated"] = True
        else:
            r_dict["water_temp_f"] = round(float(raw_temp), 1)
            r_dict["water_temp_is_estimated"] = False
            r_dict["is_estimated"] = False
        rows.append(r_dict)

    return rows



def rank_target_species(
    species_list,
    water_temp_f,
    month,
    diff_from_normal_ft=0.0,
    elevation_delta_24h=None,
    inflow_cfs=None,
    release_cfs=None,
    pressure_delta_hpa=0.0,
    wind_speed_mph=0.0,
    cloud_cover_pct=0.0,
    precipitation_in=0.0,
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

    try:
        wt = float(water_temp_f) if water_temp_f is not None else 70.0
    except Exception:
        wt = 70.0

    try:
        pool_diff = float(diff_from_normal_ft or 0.0)
    except Exception:
        pool_diff = 0.0

    try:
        elev_delta = float(elevation_delta_24h or 0.0)
    except Exception:
        elev_delta = 0.0

    try:
        inflow = float(inflow_cfs or 0.0)
    except Exception:
        inflow = 0.0

    try:
        release = float(release_cfs or 0.0)
    except Exception:
        release = 0.0

    try:
        dp = float(pressure_delta_hpa or 0.0)
    except Exception:
        dp = 0.0

    try:
        wind = float(wind_speed_mph or 0.0)
    except Exception:
        wind = 0.0

    try:
        clouds = float(cloud_cover_pct or 0.0)
    except Exception:
        clouds = 0.0

    try:
        precip = float(precipitation_in or 0.0)
    except Exception:
        precip = 0.0

    month = int(month or datetime.now(timezone.utc).month)

    def add(rec, points, reason):
        rec["score"] += points
        if reason:
            rec["reasons"].append(reason)

    ranked = []

    for original_name in species_list:
        name = str(original_name).strip()
        key = name.lower()

        rec = {
            "species": name,
            "score": 50.0,
            "reasons": []
        }

        # ---------- LARGEMOUTH BASS ----------
        if "bass, largemouth" in key or "largemouth bass" in key:
            if 60 <= wt <= 82:
                add(rec, 16, "Water temperature is in a strong largemouth feeding range.")
            elif wt > 86:
                add(rec, -8, "Very warm water can suppress midday largemouth activity.")
            elif wt < 48:
                add(rec, -10, "Cold water reduces largemouth metabolism.")

            if pool_diff >= 1.0:
                add(rec, 12, "Elevated pool expands flooded shoreline cover.")
            if elev_delta > 0.10:
                add(rec, 6, "Rising water encourages shallow movement.")
            if 5 <= wind <= 15:
                add(rec, 7, "Moderate wind improves ambush conditions.")
            if dp <= -0.5:
                add(rec, 8, "Falling pressure can trigger aggressive feeding.")
            if clouds >= 60:
                add(rec, 6, "Cloud cover favors roaming shallow fish.")
            if 3 <= month <= 6:
                add(rec, 6, "Spring through early summer is a strong seasonal window.")

        # ---------- SMALLMOUTH BASS ----------
        elif "bass, smallmouth" in key or "smallmouth bass" in key:
            if 52 <= wt <= 74:
                add(rec, 18, "Moderate water temperature favors smallmouth activity.")
            elif wt >= 82:
                add(rec, -10, "Hot surface temperatures often push smallmouth deeper.")
            if 6 <= wind <= 18:
                add(rec, 10, "Wind activates rocky points and offshore structure.")
            if dp <= -0.4:
                add(rec, 7, "Falling pressure supports active feeding.")
            if pool_diff <= 1.0:
                add(rec, 4, "Stable pool conditions favor structure-oriented smallmouth.")
            if month in (3, 4, 5, 9, 10, 11):
                add(rec, 6, "Spring and fall favor smallmouth movement.")

        # ---------- SPOTTED BASS ----------
        elif "bass, spotted" in key or "spotted bass" in key:
            if 55 <= wt <= 80:
                add(rec, 14, "Water temperature supports active spotted bass.")
            if 5 <= wind <= 16:
                add(rec, 8, "Moderate wind improves feeding on points and bluffs.")
            if release > 100 or inflow > 200:
                add(rec, 6, "Current can concentrate forage.")
            if dp <= -0.4:
                add(rec, 5, "Falling pressure can improve feeding activity.")

        # ---------- WHITE BASS ----------
        elif "bass, white" in key or "white bass" in key:
            if 58 <= wt <= 84:
                add(rec, 16, "Water temperature supports active white bass.")
            if wind >= 6:
                add(rec, 10, "Wind helps concentrate shad and schooling fish.")
            if inflow >= 200 or release >= 200:
                add(rec, 10, "Current concentrates forage and schooling white bass.")
            if dp <= -0.5:
                add(rec, 8, "Falling pressure can strengthen schooling activity.")
            if month in (3, 4, 5, 9, 10, 11):
                add(rec, 8, "Spring and fall are strong white bass movement periods.")

        # ---------- STRIPED / HYBRID BASS ----------
        elif "striped" in key or "hybrid" in key:
            if 55 <= wt <= 78:
                add(rec, 18, "Water temperature is favorable for striped/hybrid bass.")
            elif wt > 84:
                add(rec, -8, "Very warm water can restrict striped bass to deeper refuge.")
            if wind >= 6:
                add(rec, 9, "Wind can push forage into predictable feeding zones.")
            if inflow >= 200 or release >= 200:
                add(rec, 12, "Current can strongly concentrate baitfish.")
            if dp <= -0.5:
                add(rec, 7, "Falling pressure can improve open-water feeding.")
            if clouds >= 50:
                add(rec, 5, "Lower light favors longer feeding windows.")

        # ---------- CRAPPIE ----------
        elif "crappie" in key:
            if 52 <= wt <= 72:
                add(rec, 18, "Water temperature is favorable for crappie.")
            elif 72 < wt <= 82:
                add(rec, 8, "Warm water still supports crappie around deeper structure.")
            elif wt > 86:
                add(rec, -7, "Extreme heat often pushes crappie deeper and reduces daytime activity.")
            if abs(elev_delta) <= 0.20:
                add(rec, 8, "Stable water level favors predictable brush and structure patterns.")
            if wind <= 12:
                add(rec, 5, "Light-to-moderate wind supports controlled vertical presentations.")
            if month in (2, 3, 4, 5, 10, 11):
                add(rec, 8, "Seasonal timing favors crappie movement and feeding.")
            if clouds >= 50:
                add(rec, 4, "Cloud cover can extend shallow feeding periods.")

        # ---------- BLUE CATFISH ----------
        elif "catfish, blue" in key or "blue catfish" in key:
            if 55 <= wt <= 85:
                add(rec, 14, "Water temperature supports active blue catfish.")
            if inflow >= 200 or release >= 200:
                add(rec, 14, "Current concentrates forage and scent corridors.")
            if pool_diff >= 1.0:
                add(rec, 9, "Elevated water expands feeding access to flooded habitat.")
            if elev_delta > 0.10:
                add(rec, 6, "Rising water can increase shallow feeding activity.")
            if precip >= 0.05:
                add(rec, 7, "Recent precipitation can improve runoff-driven feeding.")
            if clouds >= 50:
                add(rec, 4, "Low light can extend active feeding windows.")

        # ---------- CHANNEL CATFISH ----------
        elif "catfish, channel" in key or "channel catfish" in key:
            if 62 <= wt <= 86:
                add(rec, 16, "Warm water favors channel catfish feeding.")
            if precip >= 0.05:
                add(rec, 10, "Recent rain can increase shoreline and inflow feeding.")
            if inflow >= 100:
                add(rec, 8, "Inflow delivers forage and scent.")
            if pool_diff >= 0.5:
                add(rec, 6, "Elevated water increases access to shallow feeding areas.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 6, "Warm-season timing favors channel catfish activity.")

        # ---------- FLATHEAD CATFISH ----------
        elif "catfish, flathead" in key or "flathead catfish" in key:
            if 68 <= wt <= 84:
                add(rec, 18, "Warm water supports active flathead metabolism.")
            elif wt < 55:
                add(rec, -12, "Cold water strongly reduces flathead activity.")
            if inflow >= 150 or release >= 150:
                add(rec, 8, "Current edges create ambush opportunities.")
            if clouds >= 50:
                add(rec, 5, "Low light favors flathead movement.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 8, "Warm-season timing favors flathead activity.")

        # ---------- WALLEYE / SAUGEYE / SAUGER ----------
        elif "walleye" in key or "saugeye" in key or "sauger" in key:
            if 45 <= wt <= 68:
                add(rec, 20, "Cool-to-moderate water strongly favors walleye-family activity.")
            elif 68 < wt <= 76:
                add(rec, 6, "Water remains workable but may shift fish deeper.")
            elif wt >= 80:
                add(rec, -12, "Warm water generally reduces shallow walleye-family activity.")
            if clouds >= 50:
                add(rec, 9, "Low light favors walleye-family feeding.")
            if wind >= 5:
                add(rec, 7, "Wind creates low-light, broken-surface feeding conditions.")
            if month in (2, 3, 4, 10, 11, 12):
                add(rec, 8, "Seasonal timing favors cool-water movement.")

        # ---------- PADDLEFISH ----------
        elif "paddlefish" in key:
            if inflow >= 500 or release >= 500:
                add(rec, 20, "Strong current is favorable for paddlefish movement.")
            else:
                add(rec, -8, "Limited current reduces paddlefish movement potential.")
            if month in (2, 3, 4, 5):
                add(rec, 12, "Spring timing favors paddlefish movement.")

        # ---------- SUNFISH ----------
        elif "sunfish" in key:
            if 68 <= wt <= 84:
                add(rec, 14, "Warm water favors sunfish activity.")
            if pool_diff >= 0:
                add(rec, 4, "Stable-to-elevated pool supports shallow cover.")
            if wind <= 12:
                add(rec, 4, "Lower wind improves shallow presentation control.")
            if month in (5, 6, 7, 8, 9):
                add(rec, 6, "Warm-season timing favors sunfish feeding.")

        # ---------- ALLIGATOR GAR ----------
        elif "gar, alligator" in key or "alligator gar" in key:
            if wt >= 70:
                add(rec, 14, "Warm water favors alligator gar activity.")
            if inflow >= 200 or pool_diff >= 1.0:
                add(rec, 8, "Current or elevated water can increase feeding opportunities.")

        # Unknown/other ODWC species remain valid candidates with neutral score.
        # This ensures we never invent a species that ODWC did not list.

        rec["score"] = round(max(0.0, min(100.0, rec["score"])), 1)
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



def build_recommended_tactic(
    ranked_species,
    water_temp_f,
    diff_from_normal_ft,
    elevation_delta_24h,
    inflow_cfs,
    release_cfs,
    pressure_delta_hpa,
    wind_speed_mph,
    cloud_cover_pct,
    precipitation_in,
    dt_val
):
    """
    Build a concise, actionable tactic:
    - top 2 ranked species
    - one key environmental adjustment
    """
    if not ranked_species:
        return "Target the strongest combination of forage, structure, current, and depth transitions."

    try:
        wt = float(water_temp_f or 70.0)
    except Exception:
        wt = 70.0

    try:
        pool_diff = float(diff_from_normal_ft or 0.0)
    except Exception:
        pool_diff = 0.0

    try:
        elev_delta = float(elevation_delta_24h or 0.0)
    except Exception:
        elev_delta = 0.0

    try:
        inflow = float(inflow_cfs or 0.0)
    except Exception:
        inflow = 0.0

    try:
        release = float(release_cfs or 0.0)
    except Exception:
        release = 0.0

    try:
        dp = float(pressure_delta_hpa or 0.0)
    except Exception:
        dp = 0.0

    try:
        wind = float(wind_speed_mph or 0.0)
    except Exception:
        wind = 0.0

    try:
        clouds = float(cloud_cover_pct or 0.0)
    except Exception:
        clouds = 0.0

    try:
        precip = float(precipitation_in or 0.0)
    except Exception:
        precip = 0.0

    if isinstance(dt_val, datetime):
        local_dt = dt_val.astimezone(timezone(timedelta(hours=-5)))
        hour = local_dt.hour
    else:
        hour = 12

    profiles = {
        "largemouth": "work flooded shoreline cover and secondary points with moving baits, then jigs or Texas rigs",
        "smallmouth": "work rocky points, bluff ends, and humps with jerkbaits, tubes, or Ned rigs",
        "spotted": "target main-lake points and bluff transitions with small swimbaits or finesse jigs",
        "white_bass": "follow bait on windblown points and humps with small swimbaits, spoons, or inline spinners",
        "striped_hybrid": "target open-water bait schools, channel edges, and current seams with shad, swimbaits, or spoons",
        "crappie": "fish brush, timber, docks, and bridge structure vertically with small jigs or minnows",
        "blue_catfish": "work channel-adjacent flats and current seams with fresh cut bait",
        "channel_catfish": "fish runoff mouths, riprap, flats, and inflow areas with cut or prepared bait",
        "flathead_catfish": "target timber, channel bends, and heavy cover with legal live or natural bait",
        "walleye_family": "work windblown points, riprap, and breaklines with jig-and-minnow rigs or crankbaits",
        "paddlefish": "focus on legal paddlefish zones and current corridors using only current ODWC-approved methods",
        "sunfish": "fish shallow cover, docks, and vegetation edges with small jigs or worms",
        "alligator_gar": "focus on channel, backwater, and inflow corridors with species-appropriate legal tackle",
        "other": "match presentation speed and depth to forage, structure, and current"
    }

    top_parts = []
    for species in ranked_species[:2]:
        fam = _species_family(species)
        instruction = profiles.get(fam, profiles["other"])
        display_species = format_species_name(species)
        top_parts.append(f"For {display_species}, {instruction}.")

    adjustment = None

    # Priority order: strongest tactical modifier wins.
    if pool_diff >= 1.0 and elev_delta >= 0.10:
        adjustment = "Prioritize newly flooded shoreline cover and the first adjacent drop."
    elif pool_diff >= 1.0:
        adjustment = "Use the elevated pool to fish flooded cover, but check nearby depth transitions."
    elif elev_delta <= -0.20:
        adjustment = "With falling water, back off to the first break, channel edge, or remaining cover."
    elif inflow >= 800 or release >= 800:
        adjustment = "Strong current makes seams, eddies, bridge constrictions, and downstream forage concentrations high-priority."
    elif inflow >= 200 or release >= 200:
        adjustment = "Use current-facing points and seams where forage is being concentrated."
    elif precip >= 0.05 and inflow > 0:
        adjustment = "Check runoff color lines and inflow mouths for a localized feeding response."
    elif dp <= -0.5:
        adjustment = "Use faster moving presentations first while the falling-pressure window is active."
    elif wind >= 7:
        adjustment = "Favor windblown banks and points where chop is concentrating bait."
    elif clouds <= 25 and wt >= 78 and 10 <= hour <= 16:
        adjustment = "As the sun climbs, shift toward deeper edges, shade, and vertical structure."
    elif wind < 4:
        adjustment = "With little surface chop, slow down and emphasize shade, depth, and isolated cover."

    if adjustment:
        return " ".join(top_parts + [adjustment])

    return " ".join(top_parts)




def build_tactical_strategy(
    ranked_species,
    species_ranking,
    water_temp_f,
    diff_from_normal_ft,
    elevation_delta_24h,
    inflow_cfs,
    release_cfs,
    pressure_delta_hpa,
    wind_speed_mph,
    cloud_cover_pct,
    precipitation_in,
    seasonal_phase,
    solunar_window
):
    """
    Build a concise 3-sentence strategy:
    1) pool/hydrology
    2) temperature/light/wind
    3) strongest extra factor + target summary
    """
    try:
        wt = float(water_temp_f or 70.0)
    except Exception:
        wt = 70.0

    try:
        pool_diff = float(diff_from_normal_ft or 0.0)
    except Exception:
        pool_diff = 0.0

    try:
        elev_delta = float(elevation_delta_24h or 0.0)
    except Exception:
        elev_delta = 0.0

    try:
        inflow = float(inflow_cfs or 0.0)
    except Exception:
        inflow = 0.0

    try:
        release = float(release_cfs or 0.0)
    except Exception:
        release = 0.0

    try:
        dp = float(pressure_delta_hpa or 0.0)
    except Exception:
        dp = 0.0

    try:
        wind = float(wind_speed_mph or 0.0)
    except Exception:
        wind = 0.0

    try:
        clouds = float(cloud_cover_pct or 0.0)
    except Exception:
        clouds = 0.0

    try:
        precip = float(precipitation_in or 0.0)
    except Exception:
        precip = 0.0

    # Sentence 1: pool/hydrology
    if pool_diff >= 1.0:
        if elev_delta > 0.10:
            s1 = f"The lake is {pool_diff:.1f} ft above normal and rising, favoring newly flooded cover."
        elif elev_delta < -0.20:
            s1 = f"The lake is {pool_diff:.1f} ft above normal but falling, so fish should pull toward nearby breaks."
        else:
            s1 = f"The lake is {pool_diff:.1f} ft above normal but stable, keeping flooded cover and adjacent drops productive."
    elif pool_diff <= -1.0:
        s1 = f"The lake is {abs(pool_diff):.1f} ft below normal, concentrating fish around channels, points, and deeper cover."
    else:
        s1 = "Pool level is near normal, so structure, forage, wind, and light should drive positioning."

    # Sentence 2: temperature/light/wind
    if wt >= 80 and clouds <= 25:
        s2 = f"Warm {wt:.0f}°F water and clear skies favor an early shallow window followed by deeper structure and shade."
    elif wt >= 76 and wind >= 7:
        s2 = f"Warm {wt:.0f}°F water with {wind:.0f} mph wind should keep forage active on exposed points and banks."
    elif wt >= 76:
        s2 = f"Warm {wt:.0f}°F water supports active feeding, with structure and depth becoming more important as light increases."
    elif wt >= 60:
        s2 = f"Water near {wt:.0f}°F supports broad feeding activity across shallow-to-mid-depth structure."
    else:
        s2 = f"Cool water near {wt:.0f}°F favors slower presentations around rock, channels, and seasonal staging areas."

    # Sentence 3: strongest extra modifier
    extra = None
    if inflow >= 800 or release >= 800:
        extra = "Strong current is a major positioning factor, so prioritize seams, constrictions, and downstream forage."
    elif inflow >= 200 or release >= 200:
        extra = "Moderate current should concentrate forage around seams, points, and channel-related structure."
    elif dp <= -0.8:
        extra = "A meaningful pressure drop supports a more aggressive feeding window."
    elif dp >= 0.8:
        extra = "Rising pressure may tighten fish to cover or depth, favoring slower and more precise presentations."
    elif precip >= 0.05:
        extra = "Recent precipitation makes runoff pockets and inflow areas worth checking."
    elif solunar_window in ("MAJOR", "MINOR"):
        extra = f"A {solunar_window.lower()} solunar window may briefly improve activity in the highest-confidence areas."
    elif seasonal_phase:
        extra = f"Seasonal context is {seasonal_phase.lower()}."

    if ranked_species:
        display_targets = [format_species_name(s) for s in ranked_species[:3]]
        if len(display_targets) == 1:
            leaders = display_targets[0]
        elif len(display_targets) == 2:
            leaders = f"{display_targets[0]} and {display_targets[1]}"
        else:
            leaders = f"{display_targets[0]}, {display_targets[1]}, and {display_targets[2]}"
        if extra:
            s3 = f"{extra} Top targets are {leaders}."
        else:
            s3 = f"Current telemetry ranks {leaders} as the strongest targets."
    else:
        s3 = extra or "Use the strongest combination of forage, structure, and current."

    return " ".join([s1, s2, s3])



@app.get("/api/lakes/{lake_code}/analysis")
def get_lake_analysis(lake_code: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT name, latitude, longitude, normal_pool_ft, special_regulations, target_species FROM lakes WHERE lake_code = %s", (lake_code,))
    lake_meta = cur.fetchone() or {"latitude": 35.5, "longitude": -97.5}

    trend_query = """
    WITH latest AS (
        SELECT
            timestamp, lake_code, elevation_ft, diff_from_normal_ft,
            release_cfs, inflow_cfs, water_temp_f,
            COALESCE(air_temp_f, (SELECT air_temp_f FROM lake_readings WHERE lake_code = %s AND air_temp_f IS NOT NULL ORDER BY timestamp DESC LIMIT 1)) AS air_temp_f,
            COALESCE(wind_speed_mph, (SELECT wind_speed_mph FROM lake_readings WHERE lake_code = %s AND wind_speed_mph IS NOT NULL ORDER BY timestamp DESC LIMIT 1)) AS wind_speed_mph,
            COALESCE(wind_gust_mph, (SELECT wind_gust_mph FROM lake_readings WHERE lake_code = %s AND wind_gust_mph IS NOT NULL ORDER BY timestamp DESC LIMIT 1)) AS wind_gust_mph,
            COALESCE(wind_direction_deg, (SELECT wind_direction_deg FROM lake_readings WHERE lake_code = %s AND wind_direction_deg IS NOT NULL ORDER BY timestamp DESC LIMIT 1)) AS wind_direction_deg,
            COALESCE(surface_pressure_hpa, (SELECT surface_pressure_hpa FROM lake_readings WHERE lake_code = %s AND surface_pressure_hpa IS NOT NULL ORDER BY timestamp DESC LIMIT 1)) AS surface_pressure_hpa,
            dissolved_oxygen_mg_l, turbidity_fnu, conductance_us_cm, ph,
            COALESCE(cloud_cover_pct, (SELECT cloud_cover_pct FROM lake_readings WHERE lake_code = %s AND cloud_cover_pct IS NOT NULL ORDER BY timestamp DESC LIMIT 1), 0.0) AS cloud_cover_pct,
            COALESCE(precipitation_in, (SELECT precipitation_in FROM lake_readings WHERE lake_code = %s AND precipitation_in IS NOT NULL ORDER BY timestamp DESC LIMIT 1), 0.0) AS precipitation_in,
            COALESCE(uv_index, (SELECT uv_index FROM lake_readings WHERE lake_code = %s AND uv_index IS NOT NULL ORDER BY timestamp DESC LIMIT 1), 0.0) AS uv_index
        FROM lake_readings
        WHERE lake_code = %s
        ORDER BY timestamp DESC
        LIMIT 1
    ),
    smoothed_now AS (
        SELECT AVG(surface_pressure_hpa) AS press_avg_now
        FROM lake_readings
        WHERE lake_code = %s AND timestamp >= NOW() - INTERVAL '2 hours'
    ),
    smoothed_baseline AS (
        SELECT AVG(surface_pressure_hpa) AS press_avg_3h
        FROM lake_readings
        WHERE lake_code = %s
          AND timestamp >= NOW() - INTERVAL '5 hours'
          AND timestamp <= NOW() - INTERVAL '3 hours'
    ),
    h24 AS (
        SELECT surface_pressure_hpa AS press_24h_ago, elevation_ft AS elev_24h_ago
        FROM lake_readings
        WHERE lake_code = %s
          AND timestamp <= date_trunc('day', (SELECT timestamp AT TIME ZONE 'America/Chicago' FROM latest)) AT TIME ZONE 'America/Chicago'
          AND elevation_ft IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    )
    SELECT
        latest.*,
        ROUND(COALESCE(sn.press_avg_now, latest.surface_pressure_hpa)::numeric, 1) AS cur_press_smoothed,
        ROUND(COALESCE(sb.press_avg_3h, latest.surface_pressure_hpa)::numeric, 1) AS press_3h_ago,
        COALESCE(h24.press_24h_ago, latest.surface_pressure_hpa) AS press_24h_ago,
        COALESCE(h24.elev_24h_ago, latest.elevation_ft) AS elev_24h_ago
    FROM latest
    LEFT JOIN smoothed_now sn ON TRUE
    LEFT JOIN smoothed_baseline sb ON TRUE
    LEFT JOIN h24 ON TRUE;
    """
    cur.execute(trend_query, (lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code, lake_code))
    row = cur.fetchone() or {}
    cur.close()
    conn.close()

    if row and row.get("timestamp"):
        ts = row["timestamp"]
        p_now = float(row.get("cur_press_smoothed") or row.get("surface_pressure_hpa") or 1013.2)
        p_3h = float(row.get("press_3h_ago") or p_now)
        delta_p = round(p_now - p_3h, 1)
        w_speed = float(row.get("wind_speed_mph") or 0.0)
        c_cover = float(row.get("cloud_cover_pct") or 0.0)
        raw_wt = row.get("water_temp_f")
        if raw_wt is not None and float(raw_wt or 0) > 0:
            w_temp = float(raw_wt)
            is_estimated_temp = False
        else:
            air_t = float(row.get("air_temp_f") or 78.0)
            m_val = ts.month if hasattr(ts, 'month') else datetime.now(timezone.utc).month
            w_temp = estimate_water_temperature(air_t, m_val)
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

        score, rating, sol_win = calculate_master_bite_score(
            pressure=p_now,
            delta_press=delta_p,
            wind_speed=w_speed,
            cloud_cover=c_cover,
            dt_val=ts,
            lon=lon_val,
            lat=lat_val,
            water_temp_f=w_temp,
            release_cfs=rel_cfs,
            elev_delta_24h=elev_delta,
            precip_in=precip
        )
        row["lake_name"] = lake_meta.get("name")
        row["bite_score"] = score
        row["bite_rating"] = rating
        row["solunar_window"] = sol_win
        row["bite_factors"] = calculate_bite_score_breakdown(
            delta_press=delta_p,
            wind_speed=w_speed,
            cloud_cover=c_cover,
            dt_val=ts,
            lon=lon_val,
            lat=lat_val,
            water_temp_f=w_temp,
            release_cfs=rel_cfs,
            elev_delta_24h=elev_delta,
            precip_in=precip
        )
        _month_for_phase = ts.month if hasattr(ts, "month") else datetime.now(timezone.utc).month
        _seasonal = calculate_spawn_phase(w_temp, _month_for_phase)
        row["seasonal_phase"] = _seasonal.get("phase")
        row["spawn_phase"] = _seasonal.copy()
        row["lunar"] = get_lunar_telemetry(ts)

        try:
            _wt = float(row.get("water_temp_f") or w_temp or 75.0)
            _diff = float(row.get("diff_from_normal_ft") or 0.0)
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

            row["lake_species"] = lake_species
            row["target_species"] = ranked_species
            row["primary_species"] = ", ".join(ranked_species)
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
                dt_val=ts
            )

            tactical_strategy = build_tactical_strategy(
                ranked_species=ranked_species,
                species_ranking=species_ranking,
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
                solunar_window=sol_win
            )

            row["recommended_tactic"] = recommended_tactic
            row["tactical_strategy"] = tactical_strategy

            # Backward compatibility with the existing HTML:
            # Recommended Tactic currently reads spawn_phase.tactic.
            row["spawn_phase"]["tactic"] = recommended_tactic

            # Tactical Strategy currently reads analysis_commentary.
            row["analysis_commentary"] = tactical_strategy
            row["tactical_summary"] = tactical_strategy

        except Exception as _e:
            row["lake_species"] = (lake_meta or {}).get("target_species") or []
            row["target_species"] = row["lake_species"][:3] if isinstance(row["lake_species"], list) else []
            row["primary_species"] = ", ".join(row["target_species"])
            row["species_ranking"] = []
            row["recommended_tactic"] = "Use lake structure, current, forage, and depth transitions while species ranking is unavailable."
            row["tactical_strategy"] = "Species ranking is unavailable; use the lake-listed species and current telemetry as the primary guide."
            if isinstance(row.get("spawn_phase"), dict):
                row["spawn_phase"]["tactic"] = row["recommended_tactic"]
            row["analysis_commentary"] = row["tactical_strategy"]
            row["tactical_summary"] = row["tactical_strategy"]

    if row:
        row["normal_pool_ft"] = float(lake_meta["normal_pool_ft"]) if lake_meta and lake_meta.get("normal_pool_ft") is not None else None
        row["special_regulations"] = (lake_meta or {}).get("special_regulations") or "Statewide general limits apply (no special area restrictions listed)."
        if "lake_species" not in row:
            row["lake_species"] = (lake_meta or {}).get("target_species") or []
        row["source_freshness"] = get_source_freshness(lake_code, row.get("timestamp"))
    return row

@app.get("/api/lakes/{lake_code}/forecast")
def get_lake_forecast(lake_code: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT latitude, longitude, name FROM lakes WHERE lake_code = %s", (lake_code,))
    lake = cur.fetchone()
    
    cur.execute("SELECT water_temp_f FROM lake_readings WHERE lake_code = %s AND water_temp_f IS NOT NULL ORDER BY timestamp DESC LIMIT 1", (lake_code,))
    latest_wt = cur.fetchone()
    cur.close()
    conn.close()

    if not lake or lake.get("latitude") is None or lake.get("longitude") is None:
        raise HTTPException(status_code=404, detail="Lake coordinates not found")

    lat = float(lake["latitude"])
    lon = float(lake["longitude"])
    water_temp_baseline = float(latest_wt["water_temp_f"]) if latest_wt and latest_wt.get("water_temp_f") is not None else None

    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_gusts_10m,wind_direction_10m,cloud_cover,precipitation_probability,precipitation"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
        f"&timezone=America%2FChicago&forecast_days=3"
    )
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OKLakesTelemetry/2.0"})
        with urllib.request.urlopen(req, timeout=8) as res:
            data = json.loads(res.read().decode())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Forecast provider error: {str(e)}")

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    pressures = hourly.get("surface_pressure", [])
    winds = hourly.get("wind_speed_10m", [])
    wind_gusts = hourly.get("wind_gusts_10m", [0.0] * len(times))
    wind_dirs = hourly.get("wind_direction_10m", [])
    temps = hourly.get("temperature_2m", [])
    humidities = hourly.get("relative_humidity_2m", [50.0] * len(times))
    clouds = hourly.get("cloud_cover", [])
    precip_probs = hourly.get("precipitation_probability", [])
    precips = hourly.get("precipitation", [0.0] * len(times))

    forecast_cards = []
    for i in range(len(times)):
        p_now = float(pressures[i])
        p_prev = float(pressures[i - 2]) if i >= 2 else (float(pressures[0]) if i > 0 else p_now)
        delta_p = round(p_now - p_prev, 1)

        w_speed = float(winds[i])
        c_cover = float(clouds[i])
        pr_val = float(precips[i]) if i < len(precips) and precips[i] is not None else 0.0
        
        score, rating, sol_win = calculate_master_bite_score(
            pressure=p_now,
            delta_press=delta_p,
            wind_speed=w_speed,
            cloud_cover=c_cover,
            dt_val=times[i],
            lon=lon,
            lat=lat,
            water_temp_f=water_temp_baseline,
            precip_in=pr_val
        )

        g_val = float(wind_gusts[i]) if 'wind_gusts' in locals() and i < len(wind_gusts) and wind_gusts[i] is not None else w_speed
        forecast_cards.append({
            "time": times[i],
            "air_temp_f": temps[i],

            "relative_humidity_pct": int(humidities[i]) if i < len(humidities) and humidities[i] is not None else 50,
            "surface_pressure_hpa": p_now,
            "delta_pressure_2h": delta_p,
            "wind_speed_mph": w_speed,
            "wind_gust_mph": round(g_val, 1),
            "wind_gust_mph": float(wind_gusts[i]) if i < len(wind_gusts) and wind_gusts[i] is not None else w_speed,
            "wind_direction_deg": wind_dirs[i],
            "cloud_cover_pct": c_cover,
            "precip_prob_pct": precip_probs[i],
            "bite_score": score,
            "rating": rating,
            "solunar_window": sol_win
        })

    # Ensure water temp fallback is calculated
    raw_wt = latest_data.get('water_temp_f') if 'latest_data' in locals() and latest_data else None
    if raw_wt is not None and float(raw_wt or 0) > 0:
        final_water_temp = float(raw_wt)
        is_estimated = False
    else:
        air_val = latest_data.get('air_temp_f') if 'latest_data' in locals() and latest_data else 78.0
        now_m = now.month if 'now' in locals() else 8
        final_water_temp = estimate_water_temperature(air_val, now_m)
        is_estimated = True
    visible_forecast = forecast_cards[:48]
    return {
        "lake_code": lake_code,
        "lake_name": lake["name"],
        "forecast": visible_forecast,
        "best_window": calculate_best_window(visible_forecast)
    }

@app.get("/api/lakes/{lake_code}/history")
def get_history(lake_code: str, response: Response, days: int = 7, target_date: Optional[str] = None):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    if target_date:
        query = """
        WITH time_slots AS (
            SELECT generate_series(
                (%s || ' 00:00:00 America/Chicago')::timestamptz,
                (%s || ' 23:45:00 America/Chicago')::timestamptz,
                INTERVAL '15 minutes'
            ) AS bucket
        ),
        aggregated AS (
            SELECT 
                time_bucket('15 minutes', timestamp) AS bucket,
                AVG(elevation_ft) AS elevation_ft,
                AVG(water_temp_f) AS water_temp_f,
                AVG(surface_pressure_hpa) AS surface_pressure_hpa,
                AVG(release_cfs) AS release_cfs,
                AVG(inflow_cfs) AS inflow_cfs,
                AVG(dissolved_oxygen_mg_l) AS dissolved_oxygen_mg_l,
                AVG(cloud_cover_pct) AS cloud_cover_pct
            FROM lake_readings
            WHERE lake_code = %s 
              AND timestamp >= (%s || ' 00:00:00 America/Chicago')::timestamptz - INTERVAL '2 hours'
              AND timestamp <= (%s || ' 23:59:59 America/Chicago')::timestamptz
            GROUP BY 1
        )
        SELECT 
            ts.bucket,
            ROUND(COALESCE(
                a.elevation_ft, 
                (SELECT elevation_ft FROM lake_readings WHERE lake_code = %s AND timestamp <= ts.bucket ORDER BY timestamp DESC LIMIT 1)
            )::numeric, 2) AS elevation_ft,
            ROUND(COALESCE(
                a.surface_pressure_hpa, 
                (SELECT surface_pressure_hpa FROM lake_readings WHERE lake_code = %s AND timestamp <= ts.bucket ORDER BY timestamp DESC LIMIT 1)
            )::numeric, 1) AS surface_pressure_hpa,
            ROUND(a.water_temp_f::numeric, 1) AS water_temp_f,
            ROUND(a.release_cfs::numeric, 1) AS release_cfs,
            ROUND(a.inflow_cfs::numeric, 1) AS inflow_cfs,
            ROUND(a.dissolved_oxygen_mg_l::numeric, 2) AS dissolved_oxygen_mg_l,
            ROUND(a.cloud_cover_pct::numeric, 0) AS cloud_cover_pct
        FROM time_slots ts
        LEFT JOIN aggregated a ON ts.bucket = a.bucket
        ORDER BY ts.bucket ASC;
        """
        cur.execute(query, (target_date, target_date, lake_code, target_date, target_date, lake_code, lake_code))
    else:
        bucket_interval = "1 hour" if days <= 7 else ("3 hours" if days <= 30 else "6 hours")
        query = f"""
        SELECT 
            time_bucket('{bucket_interval}', timestamp) AS bucket,
            ROUND(AVG(elevation_ft)::numeric, 2) AS elevation_ft,
            ROUND(AVG(water_temp_f)::numeric, 1) AS water_temp_f,
            ROUND(AVG(air_temp_f)::numeric, 1) AS air_temp_f,
            ROUND(AVG(surface_pressure_hpa)::numeric, 1) AS surface_pressure_hpa,
            ROUND(AVG(release_cfs)::numeric, 1) AS release_cfs,
            ROUND(AVG(inflow_cfs)::numeric, 1) AS inflow_cfs,
            ROUND(AVG(dissolved_oxygen_mg_l)::numeric, 2) AS dissolved_oxygen_mg_l,
            ROUND(AVG(cloud_cover_pct)::numeric, 0) AS cloud_cover_pct
        FROM lake_readings
        WHERE lake_code = %s AND timestamp >= NOW() - INTERVAL '{days} days'
        GROUP BY 1
        ORDER BY 1 ASC;
        """
        cur.execute(query, (lake_code,))

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows



