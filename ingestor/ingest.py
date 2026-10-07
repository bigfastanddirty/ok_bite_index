from logging_setup import log_message, setup_logging
from odwc_regs import sync_odwc_regs
from quality import *
from logging_setup import cycle_context
from uuid import uuid4
logger = setup_logging("ingestor")
_observations = {}
import os
import sys
import time
import subprocess
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")


# ---------------------------------------------------------------------------
# Lake level sources
# ---------------------------------------------------------------------------

CWMS_BASE_URL = "https://cwms-data.usace.army.mil/cwms-data"
CWMS_OFFICE = "SWT"

CWMS_LAKE_MAP = {
    "ALTU": "ALTU",
    "ARBU": "ARBU",
    "ARCA": "ARCA",
    "BBOW": "BROK",
    "BIRC": "BIRC",
    "CANT": "CANT",
    "COPA": "COPA",
    "EUFA": "EUFA",
    "FOSS": "FOSS",
    "FTGB": "FGIB",
    "GRND": "PENS",
    "HEYB": "HEYB",
    "HUGO": "HUGO",
    "HULA": "HULA",
    "KAWW": "KAWL",
    "KERR": "ROBE",
    "KEYW": "KEYS",
    "OOLO": "OOLO",
    "PINE": "PINE",
    "SKIA": "SKIA",
    "TBIR": "THUN",
    "TENK": "TENK",
    "TEXO": "DENI",
    "WAUR": "WAUR",
    "WIST": "WIST",
}

CWMS_REFERENCE_LEVELS = {
    "KERR": "ROBE.Elev.Inst.0.Top of Navigation",
}

HEFNER_USGS_SITE = "07159550"
HEFNER_NORMAL_POOL_FT = 1199.0
CWMS_MAX_AGE_HOURS = 4.0

# External API cadence controls.
#
# The ingestion loop still writes a database row every 15 minutes.
# Hourly CWMS values are carried forward between real CWMS polls.
CWMS_POLL_INTERVAL_MINUTES = 60
CWMS_LEVELS_CACHE_HOURS = 24

_cwms_levels_cache = {
    "fetched_at": None,
    "levels": [],
}


def _parse_cwms_observation_time(value):
    """
    Parse a CWMS timeseries timestamp.

    CWMS v2 commonly returns epoch milliseconds, but ISO timestamps
    are accepted as well so freshness validation remains resilient to
    response-format changes.
    """
    if value is None:
        return None

    try:
        text = str(value).strip()

        numeric = (
            isinstance(value, (int, float))
            or text.replace(".", "", 1).isdigit()
        )

        if numeric:
            epoch_value = float(value)

            # CWMS commonly returns Unix epoch milliseconds.
            if epoch_value > 100000000000:
                epoch_value /= 1000.0

            return datetime.fromtimestamp(
                epoch_value,
                tz=timezone.utc
            )

    except (TypeError, ValueError, OSError):
        pass

    try:
        parsed = datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed.astimezone(
            timezone.utc
        )

    except (TypeError, ValueError):
        return None


def _cwms_age_hours(value, now_utc=None):
    """Return observation age in hours, or None if unparseable."""
    observed = _parse_cwms_observation_time(
        value
    )

    if observed is None:
        return None

    if now_utc is None:
        now_utc = datetime.now(
            timezone.utc
        )

    return (
        now_utc - observed
    ).total_seconds() / 3600.0


def fetch_cwms_elevation(lake_code):
    """Return latest revised hourly CWMS reservoir elevation in feet."""
    cwms_id = CWMS_LAKE_MAP.get(lake_code)
    if not cwms_id:
        return None

    series_name = f"{cwms_id}.Elev.Inst.1Hour.0.Ccp-Rev"

    try:
        r = requests.get(
            f"{CWMS_BASE_URL}/timeseries",
            params={
                "office": CWMS_OFFICE,
                "name": series_name,
                "begin": "PT-48H",
                "unit": "ft",
            },
            headers={"Accept": "application/json;version=2"},
            timeout=12,
        )
        r.raise_for_status()

        values = r.json().get("values", [])
        if str(r.json().get('units','')).lower() not in ('ft','feet'):
            raise ValueError('Unexpected CWMS elevation units')
        now_utc = datetime.now(timezone.utc)

        for row in reversed(values):
            if len(row) < 2 or row[1] is None:
                continue

            age_hours = _cwms_age_hours(
                row[0],
                now_utc
            )

            if age_hours is None:
                continue

            if (
                age_hours < -0.5
                or age_hours > CWMS_MAX_AGE_HOURS
            ):
                log_message(
                    f"[{lake_code}] CWMS elevation stale | "
                    f"age {age_hours:.1f} h | "
                    f"{series_name}",
                    flush=True, level='WARNING', event='script_message')

                return None

            value = valid_value('elevation_ft', row[1])
            if value is None:
                continue
            _observations[(lake_code, 'elevation_ft')] = dict(
                metric='elevation_ft', value=value, source='cwms',
                observed_at=_parse_cwms_observation_time(row[0]),
                payload={'series':series_name,'units':r.json().get('units'),
                         'row':row,'vertical_datum':r.json().get('vertical-datum-info')})
            return round(value, 2)

    except Exception as exc:
        logger.warning('CWMS elevation retrieval failed', extra={
            'event': 'provider_failed', 'lake': lake_code, 'source': 'cwms',
            'metric': 'elevation_ft'}, exc_info=True)

    return None



def fetch_cwms_flow(lake_code, flow_type):
    """
    Return the latest valid revised hourly CWMS reservoir flow in cfs.

    flow_type:
        "inflow"  -> computed reservoir inflow
        "release" -> reservoir outflow/release
    """
    cwms_id = CWMS_LAKE_MAP.get(lake_code)

    if not cwms_id:
        return None

    if flow_type == "inflow":
        series_name = (
            f"{cwms_id}.Flow-Res In."
            f"Ave.1Hour.1Hour.Rev-Regi-Computed"
        )
    elif flow_type == "release":
        series_name = (
            f"{cwms_id}.Flow-Res Out."
            f"Ave.1Hour.1Hour.Rev-Regi-Flowgroup"
        )
    else:
        raise ValueError(f"Unknown CWMS flow type: {flow_type}")

    try:
        r = requests.get(
            f"{CWMS_BASE_URL}/timeseries",
            params={
                "office": CWMS_OFFICE,
                "name": series_name,
                "begin": "PT-48H",
                "unit": "cfs",
            },
            headers={"Accept": "application/json;version=2"},
            timeout=12,
        )
        r.raise_for_status()

        values = r.json().get("values", [])
        if str(r.json().get('units','')).lower() not in ('cfs','ft3/s'):
            raise ValueError('Unexpected CWMS flow units')
        now_utc = datetime.now(timezone.utc)

        for row in reversed(values):
            if len(row) < 2 or row[1] is None:
                continue

            age_hours = _cwms_age_hours(
                row[0],
                now_utc
            )

            if age_hours is None:
                continue

            if (
                age_hours < -0.5
                or age_hours > CWMS_MAX_AGE_HOURS
            ):
                log_message(
                    f"[{lake_code}] CWMS "
                    f"{flow_type} stale | "
                    f"age {age_hours:.1f} h | "
                    f"{series_name}",
                    flush=True, level='WARNING', event='script_message')

                return None

            value = float(row[1])

            if valid_value(flow_type+'_cfs' if flow_type == 'inflow' else 'release_cfs', value) is None:
                log_message(
                    f"[{lake_code}] Rejecting invalid CWMS "
                    f"{flow_type} value "
                    f"{value:.1f} cfs",
                    flush=True, level='INFO', event='script_message')

                continue

            metric = 'inflow_cfs' if flow_type == 'inflow' else 'release_cfs'
            _observations[(lake_code, metric)] = dict(
                metric=metric, value=value, source='cwms',
                observed_at=_parse_cwms_observation_time(row[0]),
                payload={'series':series_name,'units':r.json().get('units'),'row':row})
            return round(value, 1)

    except Exception as exc:
        logger.warning('CWMS flow retrieval failed', extra={
            'event': 'provider_failed', 'lake': lake_code, 'source': 'cwms',
            'metric': 'inflow_cfs' if flow_type == 'inflow' else 'release_cfs'}, exc_info=True)

    return None



def fetch_cwms_levels():
    """
    Return SWT CWMS location-level definitions.

    The /levels payload changes very slowly, so cache it in memory for
    24 hours. If refresh fails but an older cache exists, retain the old
    definitions rather than blanking pool-reference calculations.
    """
    global _cwms_levels_cache

    now_utc = datetime.now(timezone.utc)
    cached_at = _cwms_levels_cache.get("fetched_at")
    cached_levels = _cwms_levels_cache.get("levels") or []

    if cached_at is not None and cached_levels:
        cache_age = now_utc - cached_at

        if cache_age < timedelta(hours=CWMS_LEVELS_CACHE_HOURS):
            log_message(
                f"[CWMS] Using cached reference levels | "
                f"age {cache_age.total_seconds() / 3600.0:.1f} h | "
                f"{len(cached_levels)} definitions",
                flush=True, level='INFO', event='script_message')
            return cached_levels

    try:
        r = requests.get(
            f"{CWMS_BASE_URL}/levels",
            params={
                "office": CWMS_OFFICE,
                "page-size": 5000,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()

        levels = r.json().get("levels", [])

        if not levels:
            raise ValueError("CWMS returned no location levels")

        _cwms_levels_cache = {
            "fetched_at": now_utc,
            "levels": levels,
        }

        log_message(
            f"[CWMS] Refreshed reference-level cache | "
            f"{len(levels)} definitions",
            flush=True, level='INFO', event='script_message')

        return levels

    except Exception as exc:
        log_message(
            f"[CWMS] Reference-level refresh error: {exc}",
            flush=True, level='WARNING', event='provider_failed', exc_info=True)

        if cached_levels:
            log_message(
                f"[CWMS] Retaining stale reference-level cache | "
                f"{len(cached_levels)} definitions",
                flush=True, level='WARNING', event='provider_failed', exc_info=True)
            return cached_levels

        return []

def _parse_cwms_datetime(value):
    """Parse a CWMS ISO timestamp as an aware UTC datetime."""
    if not value:
        return None

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _add_months(dt, months):
    """
    Add calendar months without external dependencies.

    CWMS seasonal offsets are month-based, so timedelta alone is not
    sufficient.
    """
    import calendar

    month_index = (dt.month - 1) + months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])

    return dt.replace(year=year, month=month, day=day)


def _seasonal_point(origin, item):
    """Convert a CWMS seasonal offset into an absolute datetime."""
    point = _add_months(origin, int(item.get("offset-months", 0)))
    point += timedelta(minutes=int(item.get("offset-minutes", 0)))
    return point


def _evaluate_cwms_seasonal_level(definition, when):
    """
    Evaluate a repeating CWMS seasonal level curve.

    interpolate-string == T means linearly interpolate between seasonal
    control points. Otherwise use the most recent control-point value.
    """
    seasonal = definition.get("seasonal-values") or []
    if not seasonal:
        return None

    origin_raw = definition.get("interval-origin")
    if not origin_raw:
        return None

    origin = _parse_cwms_datetime(origin_raw)
    when = when.astimezone(timezone.utc)

    interval_months = int(definition.get("interval-months") or 12)
    interpolate = str(
        definition.get("interpolate-string", "F")
    ).upper() == "T"

    # Build enough adjacent annual/period cycles to bracket 'when'.
    approx_cycles = (
        (when.year - origin.year) * 12 + (when.month - origin.month)
    ) // interval_months

    points = []

    for cycle in range(approx_cycles - 2, approx_cycles + 3):
        cycle_origin = _add_months(origin, cycle * interval_months)

        for item in seasonal:
            value = item.get("value")
            if value is None:
                continue

            points.append((
                _seasonal_point(cycle_origin, item),
                float(value),
            ))

    points.sort(key=lambda x: x[0])

    previous = None
    following = None

    for point in points:
        if point[0] <= when:
            previous = point
        elif point[0] > when:
            following = point
            break

    if previous is None:
        return None

    if not interpolate or following is None:
        return previous[1]

    t0, v0 = previous
    t1, v1 = following

    if t1 <= t0 or v0 == v1:
        return v0

    fraction = (when - t0).total_seconds() / (t1 - t0).total_seconds()

    return v0 + ((v1 - v0) * fraction)


def fetch_cwms_reference_level(lake_code, when, all_levels):
    """
    Return the applicable CWMS operating reference elevation in feet.

    Selects the newest definition effective at 'when', then evaluates either
    its constant value or repeating seasonal curve.
    """
    cwms_id = CWMS_LAKE_MAP.get(lake_code)
    if not cwms_id:
        return None

    level_id = CWMS_REFERENCE_LEVELS.get(
        lake_code,
        f"{cwms_id}.Elev.Inst.0.Top of Conservation",
    )

    candidates = []

    for level in all_levels:
        if level.get("location-level-id") != level_id:
            continue

        effective = _parse_cwms_datetime(level.get("level-date"))
        if effective is None:
            continue

        if effective <= when:
            candidates.append((effective, level))

    if not candidates:
        log_message(
            f"[{lake_code}] No applicable CWMS reference level: {level_id}",
            flush=True, level='INFO', event='script_message')
        return None

    _, definition = max(candidates, key=lambda item: item[0])

    value = definition.get("constant-value")

    if value is None:
        value = _evaluate_cwms_seasonal_level(definition, when)

    if value is None:
        log_message(
            f"[{lake_code}] Unable to evaluate CWMS reference level: {level_id}",
            flush=True, level='INFO', event='script_message')
        return None

    # Current SWT level definitions returned by this endpoint are meters.
    units = str(definition.get("level-units-id", "")).lower()

    if units == "m":
        value_ft = float(value) / 0.3048
    elif units in ("ft", "feet"):
        value_ft = float(value)
    else:
        log_message(
            f"[{lake_code}] Unsupported CWMS level unit "
            f"{definition.get('level-units-id')!r}: {level_id}",
            flush=True, level='INFO', event='script_message')
        return None

    return round(value_ft, 2)




def fetch_hefner_telemetry():
    result={'elevation':None,'temp_f':None}
    try:
        now=datetime.now(timezone.utc)
        for observation in fetch_usgs():
            if not is_fresh(observation['observed_at'], now, 6):
                continue
            metric=observation['metric']
            _observations[('HEFN',metric)]=observation
            result['elevation' if metric=='elevation_ft' else 'temp_f']=observation['value']
    except Exception as exc:
        logger.warning('USGS telemetry retrieval failed', extra={
            'event': 'provider_failed', 'lake': 'HEFN', 'source': 'usgs'}, exc_info=True)
    return result

def get_db_connection():
    for attempt in range(1, 11):
        try:
            return psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS, connect_timeout=5
            )
        except Exception as e:
            log_message(f"[{attempt}/10] Waiting for DB... ({e})", level='WARNING', event='provider_failed', exc_info=True)
            time.sleep(3)
    raise Exception("DB unreachable.")


def ensure_app_sync_state_table(conn):
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS public.app_sync_state (
            sync_name TEXT PRIMARY KEY,
            last_run TIMESTAMPTZ NOT NULL
        );
        """
    )

    conn.commit()
    cur.close()


def _update_sync_state(conn, sync_name, when):
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO public.app_sync_state (
            sync_name,
            last_run
        )
        VALUES (%s, %s)

        ON CONFLICT (sync_name)
        DO UPDATE SET
            last_run = EXCLUDED.last_run;
        """,
        (
            sync_name,
            when,
        )
    )

    conn.commit()
    cur.close()


def run_maintenance_checks():
    """
    Run the daily/weekly/monthly maintenance scheduler using
    one PostgreSQL connection.

    The daemon invokes this function at most once per hour.
    """
    conn = get_db_connection()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT
                sync_name,
                last_run
            FROM public.app_sync_state;
            """
        )

        state = {
            row[0]: row[1]
            for row in cur.fetchall()
        }

        cur.close()

        now_utc = datetime.now(timezone.utc)
        central = ZoneInfo("America/Chicago")
        now_local = now_utc.astimezone(central)


        # ====================================================
        # ODWC REGULATIONS — EVERY 7 DAYS
        # ====================================================

        last_regs = state.get("odwc_regulations")

        if last_regs is None:
            _update_sync_state(
                conn,
                "odwc_regulations",
                now_utc
            )

            log_message(
                "[ODWC Regulations] Scheduler initialized.",
                flush=True, level='INFO', event='script_message')

        else:
            regs_age = (
                now_utc
                - last_regs.astimezone(timezone.utc)
            )

            if regs_age >= timedelta(days=7):
                try:
                    log_message(
                        "[ODWC Regulations] "
                        "7-day interval triggered.",
                        flush=True, level='INFO', event='script_message')

                    sync_odwc_regs(conn)

                    _update_sync_state(
                        conn,
                        "odwc_regulations",
                        now_utc
                    )

                    log_message(
                        "[ODWC Regulations] "
                        "Synchronization completed.",
                        flush=True, level='INFO', event='script_message')

                except Exception as exc:
                    conn.rollback()

                    log_message(
                        "[ODWC Regulations] "
                        f"Scheduler error: {exc}",
                        flush=True, level='WARNING', event='provider_failed', exc_info=True)


        # ====================================================
        # ODWC SPECIES — ONCE PER CALENDAR MONTH
        # ====================================================

        last_species = state.get("odwc_species")

        if last_species is None:
            _update_sync_state(
                conn,
                "odwc_species",
                now_utc
            )

            log_message(
                "[ODWC Species] Scheduler initialized.",
                flush=True, level='INFO', event='script_message')

        else:
            species_local = last_species.astimezone(central)

            species_due = (
                species_local.year,
                species_local.month
            ) != (
                now_local.year,
                now_local.month
            )

            if species_due:
                result = subprocess.run(
                    [
                        sys.executable,
                        "/app/odwc_species.py",
                    ],
                    check=False
                )

                if result.returncode == 0:
                    _update_sync_state(
                        conn,
                        "odwc_species",
                        now_utc
                    )

                    log_message(
                        "[ODWC Species] "
                        "Synchronization completed.",
                        flush=True, level='INFO', event='script_message')

                else:
                    log_message(
                        "[ODWC Species] "
                        "Sync failed with exit code "
                        f"{result.returncode}; "
                        "last_run unchanged.",
                        flush=True, level='ERROR', event='job_failed')


        # ====================================================
        # ACTIVE PROJECTS — ONCE PER CALENDAR DAY
        # ====================================================

        last_projects = state.get("active_projects")

        projects_due = (
            last_projects is None
            or last_projects.astimezone(central).date()
            != now_local.date()
        )

        if projects_due:
            result = subprocess.run(
                [
                    sys.executable,
                    "/app/active_projects.py",
                ],
                check=False
            )

            if result.returncode == 0:
                _update_sync_state(
                    conn,
                    "active_projects",
                    now_utc
                )

                log_message(
                    "[Active Projects] "
                    "Synchronization completed.",
                    flush=True, level='INFO', event='script_message')

            else:
                log_message(
                    "[Active Projects] "
                    "Sync failed with exit code "
                    f"{result.returncode}; "
                    "last_run unchanged.",
                    flush=True, level='ERROR', event='job_failed')

    except Exception as exc:
        conn.rollback()

        log_message(
            f"[Maintenance] Scheduler error: {exc}",
            flush=True, level='WARNING', event='provider_failed', exc_info=True)

    finally:
        conn.close()


def ensure_source_status_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lake_source_status (
            lake_code TEXT NOT NULL,
            source TEXT NOT NULL,
            last_success TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (lake_code, source)
        );
    """)
    conn.commit()
    cur.close()


def mark_source_success(cur, lake_code, source, when):
    cur.execute("""
        INSERT INTO lake_source_status (lake_code, source, last_success)
        VALUES (%s, %s, %s)
        ON CONFLICT (lake_code, source)
        DO UPDATE SET last_success = EXCLUDED.last_success;
    """, (lake_code, source, when))


def fetch_weather_batch(lakes):
    """
    Fetch current Open-Meteo weather for all lakes in one request.

    Open-Meteo returns responses in the same coordinate order supplied,
    so results are mapped back to lake_code by position.
    """
    valid_lakes = []

    for lake in lakes:
        if (
            lake.get("latitude") is None
            or lake.get("longitude") is None
        ):
            continue

        valid_lakes.append(lake)

    if not valid_lakes:
        return {}

    params = {
        "latitude": ",".join(
            str(float(lake["latitude"]))
            for lake in valid_lakes
        ),
        "longitude": ",".join(
            str(float(lake["longitude"]))
            for lake in valid_lakes
        ),
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "surface_pressure,"
            "wind_speed_10m,"
            "wind_gusts_10m,"
            "wind_direction_10m,"
            "cloud_cover,"
            "precipitation,"
            "uv_index"
        ),
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "hourly": "surface_pressure",
        "minutely_15": "precipitation",
        "past_hours": 6, "forecast_hours": 2,
        "past_minutely_15": 8, "forecast_minutely_15": 2,
        "timeformat": "unixtime", "timezone": "UTC",
    }

    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=20,
        )
        response.raise_for_status()

        payload = response.json()

        # Multiple coordinates return a list. Preserve support for
        # a one-location response for easier testing.
        if isinstance(payload, dict):
            payload = [payload]

        if not isinstance(payload, list):
            raise ValueError(
                f"Unexpected Open-Meteo response type: "
                f"{type(payload).__name__}"
            )

        if len(payload) != len(valid_lakes):
            raise ValueError(
                f"Open-Meteo returned {len(payload)} locations "
                f"for {len(valid_lakes)} requested lakes"
            )

        result = {}

        for lake, location_data in zip(valid_lakes, payload):
            current = (
                location_data.get("current", {})
                if isinstance(location_data, dict)
                else {}
            )

            if current:
                result[lake["lake_code"]] = location_data

        log_message(
            f"[Open-Meteo] Batch weather loaded for "
            f"{len(result)}/{len(valid_lakes)} lakes",
            flush=True, level='INFO', event='script_message')

        return result

    except Exception as exc:
        logger.warning('Batch weather retrieval failed', extra={
            'event': 'provider_failed', 'source': 'open-meteo'}, exc_info=True)

        return {}



def run_sync():
    cycle_id = uuid4().hex[:12]
    token = cycle_context.set(cycle_id)
    started = time.monotonic()
    committed = degraded = expected = 0
    conn = None
    logger.info('Telemetry cycle started', extra={'event': 'cycle_started'})
    try:
        conn = get_db_connection()
        ensure_quality_schema(conn)
        cur=conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT lake_code, latitude, longitude FROM lakes ORDER BY lake_code')
        lakes=cur.fetchall()
        expected = len(lakes)
        now=datetime.now(timezone.utc)
        _observations.clear()
        weather=fetch_weather_batch(lakes)
        cur.execute("SELECT lake_code,last_success FROM lake_source_status WHERE source='cwms'")
        previous_poll={r['lake_code']:r['last_success'] for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT lake_code FROM lake_metric_state WHERE source='cwms'")
        initialized={r['lake_code'] for r in cur.fetchall()}
        due={l['lake_code'] for l in lakes if l['lake_code']!='HEFN' and
             (l['lake_code'] not in initialized or l['lake_code'] not in previous_poll or
              (now-previous_poll[l['lake_code']]).total_seconds()>=3600)}
        levels=fetch_cwms_levels()
        field_map={'temperature_2m':'air_temp_f','relative_humidity_2m':'relative_humidity_pct',
                   'surface_pressure':'surface_pressure_hpa','wind_speed_10m':'wind_speed_mph',
                   'wind_gusts_10m':'wind_gust_mph','wind_direction_10m':'wind_direction_deg',
                   'cloud_cover':'cloud_cover_pct','precipitation':'precipitation_in','uv_index':'uv_index'}
        for lake in lakes:
            code=lake['lake_code']; payload=weather.get(code) or {}; current=payload.get('current') or {}
            observed=current.get('time')
            if is_fresh(observed,now,1):
                for provider_field,metric in field_map.items():
                    value=valid_value(metric,current.get(provider_field))
                    if value is not None:
                        _observations[(code,metric)]=dict(metric=metric,value=value,source='weather',
                            observed_at=utc(observed),payload={'provider':'open-meteo',
                            'units':payload.get('current_units',{}).get(provider_field),
                            'current':current,'latitude':payload.get('latitude'),
                            'longitude':payload.get('longitude'),'elevation':payload.get('elevation')})
                mark_source_success(cur,code,'weather',now)
            if code=='HEFN':
                telemetry=fetch_hefner_telemetry()
                if any(v is not None for v in telemetry.values()):
                    mark_source_success(cur,code,'usgs',now)
                if telemetry['elevation'] is not None:
                    _observations[(code,'diff_from_normal_ft')]=dict(
                        metric='diff_from_normal_ft',value=telemetry['elevation']-HEFNER_NORMAL_POOL_FT,
                        source='usgs',observed_at=_observations[(code,'elevation_ft')]['observed_at'],
                        payload={'reference_ft':HEFNER_NORMAL_POOL_FT,'reference':'Hefner normal pool'})
            elif code in due:
                elevation=fetch_cwms_elevation(code)
                if elevation is not None:
                    reference=fetch_cwms_reference_level(code,now,levels)
                    if reference is not None:
                        _observations[(code,'diff_from_normal_ft')]=dict(
                            metric='diff_from_normal_ft',value=round(elevation-reference,2),source='cwms',
                            observed_at=_observations[(code,'elevation_ft')]['observed_at'],
                            payload={'reference_ft':reference,'reference':'CWMS applicable operating level',
                                     'evaluated_at':now.isoformat()})
                fetch_cwms_flow(code,'inflow'); fetch_cwms_flow(code,'release')
                if any(k[0]==code and v['source']=='cwms' for k,v in _observations.items()):
                    mark_source_success(cur,code,'cwms',now)
            for (lake_code,metric),observation in list(_observations.items()):
                if lake_code==code:
                    record_observation(cur,code,observation,now)
            cur.execute('SELECT * FROM lake_metric_state WHERE lake_code=%s',(code,))
            state={r['metric']:r for r in cur.fetchall()}
            values={};quality={}
            for metric,record in state.items():
                max_age=1 if record['source']=='weather' else (6 if record['source']=='usgs' else 4)
                fresh=is_fresh(record['observed_at'],now,max_age)
                values[metric]=record['value'] if fresh else None
                quality[metric]={'source':record['source'],'observed_at':record['observed_at'].isoformat(),
                    'retrieved_at':record['retrieved_at'].isoformat(),'fresh':fresh,
                    'carried_forward':(code,metric) not in _observations,
                    'source_metadata':record['payload']}
            hourly=payload.get('hourly',{});quarter=payload.get('minutely_15',{})
            if code != 'HEFN' and values.get('elevation_ft') is not None:
                reference=fetch_cwms_reference_level(code,now,levels)
                if reference is not None:
                    values['diff_from_normal_ft']=round(float(values['elevation_ft'])-reference,2)
                    quality['diff_from_normal_ft']=dict(quality.get('elevation_ft',{}))
                    quality['diff_from_normal_ft']['source_metadata']={'reference_ft':reference,
                        'evaluated_at':now.isoformat(),'reference':'CWMS applicable operating level'}
            values['pressure_delta_3h']=pressure_delta(hourly.get('time',[]),hourly.get('surface_pressure',[]),observed) if observed else None
            values['precipitation_1h_in']=rain_hour(quarter.get('time',[]),quarter.get('precipitation',[]),observed) if observed else None
            for metric in ['pressure_delta_3h','precipitation_1h_in']:
                quality[metric]={'source':'weather','observed_at':utc(observed).isoformat() if observed else None,
                    'fresh':is_fresh(observed,now,1) and values[metric] is not None,
                    'window_hours':3 if metric=='pressure_delta_3h' else 1}
                if not quality[metric]['fresh']:
                    values[metric]=None
            fields=list(field_map.values())+['elevation_ft','diff_from_normal_ft','inflow_cfs',
                     'release_cfs','water_temp_f','pressure_delta_3h','precipitation_1h_in']
            cur.execute('INSERT INTO lake_readings (timestamp,lake_code,'+','.join(fields)+',quality) VALUES ('+
                        ','.join(['%s']*(len(fields)+3))+')',
                        [now,code]+[values.get(f) for f in fields]+[json.dumps(quality,default=str)])
            conn.commit()  # Log success only after the lake transaction commits.
            committed += 1
            stale = sorted(metric for metric, detail in quality.items() if not detail.get('fresh'))
            missing = sorted(metric for metric in fields if values.get(metric) is None)
            carried = sorted(metric for metric, detail in quality.items()
                             if detail.get('fresh') and detail.get('carried_forward'))
            if any(values.get(metric) is None for metric in
                   ('pressure_delta_3h', 'wind_speed_mph', 'cloud_cover_pct')):
                degraded += 1
            logger.info('Lake observation committed', extra={
                'event': 'lake_committed', 'lake': code,
                'fresh_metrics': sum(values.get(metric) is not None for metric in fields),
                'stale_metrics': stale, 'missing_metrics': missing, 'carried_metrics': carried})
        logger.info('Telemetry cycle completed', extra={
            'event': 'cycle_completed', 'duration_ms': round((time.monotonic() - started) * 1000),
            'lakes_expected': expected, 'lakes_committed': committed, 'degraded_lakes': degraded})
    except Exception:
        logger.exception('Telemetry cycle failed; earlier lake commits are preserved', extra={
            'event': 'cycle_failed', 'duration_ms': round((time.monotonic() - started) * 1000),
            'lakes_expected': expected, 'lakes_committed': committed})
        raise
    finally:
        if conn is not None:
            conn.close()
        cycle_context.reset(token)

if __name__ == "__main__":
    logger.info('Telemetry daemon started; interval 900 seconds', extra={'event': 'service_started'})

    # One-time schema initialization.
    schema_conn = get_db_connection()

    try:
        ensure_quality_schema(schema_conn)
        ensure_source_status_table(schema_conn)
        ensure_app_sync_state_table(schema_conn)

    finally:
        schema_conn.close()

    maintenance_last = None

    while True:
        try:
            run_sync()

        except Exception:
            pass  # run_sync logs the failed cycle, then retry on the normal schedule.

        now_monotonic = time.monotonic()

        # Daily/weekly/monthly maintenance only needs
        # scheduler-state checks once per hour.
        if (
            maintenance_last is None
            or (
                now_monotonic
                - maintenance_last
            ) >= 3600
        ):
            try:
                run_maintenance_checks()

            except Exception as exc:
                log_message(
                    "[Maintenance] "
                    f"Scheduler failure: {exc}",
                    flush=True, level='WARNING', event='job_failed', exc_info=True)

            maintenance_last = time.monotonic()

        logger.debug('Waiting 900 seconds for the next scheduled cycle', extra={'event': 'cycle_wait'})

        time.sleep(900)
