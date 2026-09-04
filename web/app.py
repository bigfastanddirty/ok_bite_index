
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
    if water_temp_f is None:
        water_temp_f = 70.0
    wt = float(water_temp_f)
    
    if month in (12, 1, 2) or wt < 50:
        return {
            "phase": "Winter Torpor / Staging",
            "tactic": "Metabolism suppressed. Baitfish suspended over deep wintering river channels and main-lake basins."
        }
    elif month in (3, 4) and wt < 60:
        return {
            "phase": "Pre-Spawn Movement",
            "tactic": "Bass and crappie staging along secondary points and creek channel contours leading into protected pockets."
        }
    elif (month in (4, 5) and 60 <= wt <= 72) or (month == 5 and wt < 75):
        return {
            "phase": "Active Spawn / Bedding",
            "tactic": "Shallow nest guarding in protected coves, flat pockets, and hard-bottom sand/gravel banks."
        }
    elif month in (5, 6) and wt > 72:
        return {
            "phase": "Post-Spawn Recovery",
            "tactic": "Fish sliding to immediate outside weedlines, brush piles, and secondary drops to feed on shad spawns."
        }
    elif month in (6, 7, 8) or (month == 9 and wt > 82):
        return {
            "phase": "Summer Thermal Refuge",
            "tactic": "Predators holding near current breaks, deep brush, or thermoclines during mid-day heat."
        }
    elif month in (9, 10, 11):
        return {
            "phase": "Fall Forage Push",
            "tactic": "Aggressive predatory migration tracking schooling threadfin shad into creek channels and wind-blown pockets."
        }
    else:
        return {
            "phase": "Transitional",
            "tactic": "Scattered forage holding along primary breaklines and secondary creek points."
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

@app.api_route("/methodology", methods=["GET", "HEAD"], response_class=HTMLResponse)
@app.api_route("/about", methods=["GET", "HEAD"], response_class=HTMLResponse)
def get_methodology():
    try:
        with open("methodology.html", "r") as f:
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


@app.get("/api/lakes/{lake_code}/analysis")
def get_lake_analysis(lake_code: str, response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT name, latitude, longitude, normal_pool_ft FROM lakes WHERE lake_code = %s", (lake_code,))
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
        row["spawn_phase"] = calculate_spawn_phase(w_temp, ts)
        row["lunar"] = get_lunar_telemetry(ts)

        try:
            _wt = float(row.get("water_temp_f") or w_temp or 75.0)
            _diff = float(row.get("diff_from_normal_ft") or 0.0)
            _cfs = float(row.get("release_cfs") or rel_cfs or 0.0)
            _p_3h = float(row.get("press_avg_3h") or 0.0)
            _cur_p = float(row.get("press_avg_now") or row.get("surface_pressure_hpa") or 0.0)
            _delta_p = (_cur_p - _p_3h) if (_cur_p and _p_3h) else 0.0
            _month = ts.month if hasattr(ts, 'month') else 4

            # Check for active surge (major flood pool > 4.5ft or rapid inflow surge > 2000 CFS)
            if _diff > 4.5 or _cfs > 2000:
                _species = ["Largemouth Bass", "Blue Catfish", "Flathead Catfish"]
                _comm = "High inflow and elevated flood pool creating ambush points near flooded shoreline brush, riprap, and secondary creek channels. Target stained runoff lines."
            elif _diff > 1.5 and _cfs <= 500:
                # Elevated pool but stable flow (holding water)
                if (5 <= _month <= 9) or _wt >= 70.0:
                    _species = ["Largemouth Bass", "Channel Catfish", "White Bass / Sand Bass"]
                    _comm = "Elevated pool with stable flow. Fish submerged shoreline buckbrush, flooded willow lines, and secondary channel drop-offs."
                else:
                    _species = ["Largemouth Bass", "Crappie", "Saugeye"]
                    _comm = "Elevated lake level with stable baseflow. Work newly submerged timber, brush piles, and secondary channel transitions."
            elif (3 <= _month <= 5) and (62.0 <= _wt <= 69.0):
                _species = ["Largemouth Bass (Bedding)", "Black Crappie", "Redear Sunfish"]
                _comm = "Active Spawn Phase: Bass and crappie locked on shallow beds (1-5 ft). Target protected north-pocket coves, sandy gravel flats, and shoreline stumps with weightless plastics and tubes."
            elif (2 <= _month <= 4) and (52.0 <= _wt < 62.0):
                _species = ["Largemouth Bass (Pre-Spawn)", "White Crappie", "Walleye / Saugeye"]
                _comm = "Pre-Spawn Staging: Heavy females feeding aggressively along primary secondary points, channel swings, and brush at creek mouths (6-12 ft). Work suspending jerkbaits, lipless cranks, and chatterbaits."
            elif (5 <= _month <= 6) and (70.0 <= _wt < 78.0):
                _species = ["Largemouth Bass (Post-Spawn)", "White Bass", "Channel Catfish"]
                _comm = "Post-Spawn Recovery: Fish migrating from shallow coves toward secondary points and offshore ledges. Target shad spawn on riprap early, then switch to deep diving cranks and Carolina rigs on drop-offs."
            elif _wt >= 78.0:
                if _delta_p < -0.8:
                    _species = ["White Bass (Sand Bass)", "Striped Bass", "Largemouth Bass"]
                    _comm = "Pre-frontal drop triggering active feeding. Target early morning surface boils and windblown humps where shad school."
                else:
                    _species = ["Striped / Hybrid Bass", "Crappie", "Channel Catfish"]
                    _comm = "Warm summer pattern. Target thermocline breaks (15-25 ft), bridge pilings, and deep main-lake brush piles with vertical jigs or live shad."
            elif (9 <= _month <= 11) and (58.0 <= _wt < 78.0):
                _species = ["Largemouth Bass", "Crappie", "White Bass"]
                _comm = "Fall Shad Migration: Predators following massive shad balls into the backs of major creeks and flats. Target bait schools with squarebills and spinnerbaits."
            else:
                _species = ["Crappie", "Blue Catfish", "Walleye / Saugeye"]
                _comm = "Winter pattern: Slow roll vertical jigs around deep brush piles (20-35 ft), river channel bends, and sunken timber."

            row["target_species"] = _species
            row["primary_species"] = ", ".join(_species)
            row["analysis_commentary"] = _comm
            row["tactical_summary"] = _comm
        except Exception as _e:
            row["target_species"] = ["Largemouth Bass", "Crappie", "Striped Bass"]
            row["primary_species"] = "Largemouth Bass, Crappie, Striped Bass"
            row["analysis_commentary"] = "Stable conditions. Focus on main lake structure and depth breaks."
            row["tactical_summary"] = "Stable conditions. Focus on main lake structure and depth breaks."

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
    return {
        "lake_code": lake_code,
        "lake_name": lake["name"],
        "forecast": forecast_cards[:48]
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



@app.get("/methodology", response_class=HTMLResponse)
def get_methodology():
    return """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bite Score & Telemetry Methodology | OK Lakes</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 font-sans antialiased min-h-screen p-4 sm:p-8">
    <div class="max-w-3xl mx-auto space-y-6">
        <div class="flex items-center justify-between pb-4 border-b border-slate-800">
            <a href="/" class="flex items-center gap-2 text-sky-400 hover:text-sky-300 text-sm font-semibold transition">
                &larr; Back to Live Dashboard
            </a>
            <span class="px-2.5 py-1 rounded-full bg-slate-900 border border-slate-800 text-xs font-mono text-slate-400">v2.4 Telemetry Engine</span>
        </div>

        <div class="space-y-2">
            <h1 class="text-2xl sm:text-3xl font-bold tracking-tight text-white">Telemetry &amp; Bite Score Methodology</h1>
            <p class="text-sm text-slate-400">How the Oklahoma Lakes Dashboard models feeding activity, hydrology, and fish positioning in real time.</p>
        </div>

        <div class="grid gap-6">
            <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-2">
                <h2 class="text-base font-bold text-sky-400">1. Bite Score Algorithm (0–100 Scale)</h2>
                <p class="text-xs text-slate-300 leading-relaxed">
                    The aggregate bite score combines three core environmental inputs:
                </p>
                <ul class="text-xs text-slate-400 list-disc list-inside space-y-1">
                    <li><strong>Barometric Pressure Trend (40% Weight):</strong> Rapid 3-hour drops trigger pre-frontal feeding; rising spikes (&gt;1020 hPa post-front) suppress active chases.</li>
                    <li><strong>Solunar &amp; Lunar Alignment (35% Weight):</strong> Major feeding windows occur during Moon Overhead and Underfoot transits (&plusmn;90 min), amplified during New and Full Moon phases.</li>
                    <li><strong>Water Stability &amp; Flow (25% Weight):</strong> Moderate steady inflows concentrate baitfish; severe muddy flood surges penalize visual sight-feeders.</li>
                </ul>
            </div>

            <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-2">
                <h2 class="text-base font-bold text-cyan-400">2. Hydrology &amp; Inflow Telemetry</h2>
                <p class="text-xs text-slate-300 leading-relaxed">
                    Data is ingested continuously from dual upstream and downstream sensors:
                </p>
                <ul class="text-xs text-slate-400 list-disc list-inside space-y-1">
                    <li><strong>USACE Tulsa District:</strong> Reservoir water surface elevation (ft), pool delta vs. conservation pool, and tailrace dam release (CFS).</li>
                    <li><strong>USGS Water Services:</strong> Real-time tributary streamflow (CFS) from feeder river gages (e.g., Deep Fork River for Lake Arcadia).</li>
                </ul>
            </div>

            <div class="p-5 rounded-2xl bg-slate-900 border border-slate-800 space-y-2">
                <h2 class="text-base font-bold text-indigo-400">3. Tactical Engine &amp; Spawn Phase</h2>
                <p class="text-xs text-slate-300 leading-relaxed">
                    Recommendations evaluate water temperature bands, photoperiod, and water elevation deltas. In high pool conditions (+1.5 ft to +4 ft), tactical models prioritize flooded shoreline woody cover (buckbrush/willows) and secondary breaklines rather than false runoff warnings.
                </p>
            </div>
        </div>

        <div class="pt-6 border-t border-slate-800 text-center">
            <a href="/" class="inline-flex items-center px-4 py-2 rounded-xl bg-sky-500 hover:bg-sky-400 text-slate-950 font-bold text-xs transition">
                Return to Dashboard
            </a>
        </div>
    </div>
</body>
</html>"""
