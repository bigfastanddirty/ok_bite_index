from odwc_regs import sync_odwc_regs
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
                print(
                    f"[{lake_code}] CWMS elevation stale | "
                    f"age {age_hours:.1f} h | "
                    f"{series_name}",
                    flush=True,
                )

                return None

            return round(
                float(row[1]),
                2
            )

    except Exception as exc:
        print(
            f"[{lake_code}] CWMS elevation error ({series_name}): {exc}",
            flush=True,
        )

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
                print(
                    f"[{lake_code}] CWMS "
                    f"{flow_type} stale | "
                    f"age {age_hours:.1f} h | "
                    f"{series_name}",
                    flush=True,
                )

                return None

            value = float(row[1])

            if value < 0 or value > 500000:
                print(
                    f"[{lake_code}] Rejecting invalid CWMS "
                    f"{flow_type} value "
                    f"{value:.1f} cfs",
                    flush=True,
                )

                continue

            return round(
                value,
                1
            )

    except Exception as exc:
        print(
            f"[{lake_code}] CWMS {flow_type} error "
            f"({series_name}): {exc}",
            flush=True,
        )

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
            print(
                f"[CWMS] Using cached reference levels | "
                f"age {cache_age.total_seconds() / 3600.0:.1f} h | "
                f"{len(cached_levels)} definitions",
                flush=True,
            )
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

        print(
            f"[CWMS] Refreshed reference-level cache | "
            f"{len(levels)} definitions",
            flush=True,
        )

        return levels

    except Exception as exc:
        print(
            f"[CWMS] Reference-level refresh error: {exc}",
            flush=True,
        )

        if cached_levels:
            print(
                f"[CWMS] Retaining stale reference-level cache | "
                f"{len(cached_levels)} definitions",
                flush=True,
            )
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
        print(
            f"[{lake_code}] No applicable CWMS reference level: {level_id}",
            flush=True,
        )
        return None

    _, definition = max(candidates, key=lambda item: item[0])

    value = definition.get("constant-value")

    if value is None:
        value = _evaluate_cwms_seasonal_level(definition, when)

    if value is None:
        print(
            f"[{lake_code}] Unable to evaluate CWMS reference level: {level_id}",
            flush=True,
        )
        return None

    # Current SWT level definitions returned by this endpoint are meters.
    units = str(definition.get("level-units-id", "")).lower()

    if units == "m":
        value_ft = float(value) / 0.3048
    elif units in ("ft", "feet"):
        value_ft = float(value)
    else:
        print(
            f"[{lake_code}] Unsupported CWMS level unit "
            f"{definition.get('level-units-id')!r}: {level_id}",
            flush=True,
        )
        return None

    return round(value_ft, 2)




def fetch_hefner_telemetry():
    """
    Fetch current Lake Hefner reservoir telemetry with one USGS request.

    00065 = gage/elevation value used for reservoir elevation
    00011 = verified tower water temperature in degrees Fahrenheit

    Both observations must be no more than 6 hours old.
    """
    result = {
        "elevation": None,
        "temp_f": None,
    }

    try:
        r = requests.get(
            "https://waterservices.usgs.gov/nwis/iv/",
            params={
                "format": "json",
                "sites": HEFNER_USGS_SITE,
                "parameterCd": "00065,00011",
                "period": "P2D",
                "siteStatus": "all",
            },
            timeout=12,
        )
        r.raise_for_status()

        series = (
            r.json()
            .get("value", {})
            .get("timeSeries", [])
        )

        now_utc = datetime.now(timezone.utc)

        for series_item in series:
            variable_codes = (
                series_item
                .get("variable", {})
                .get("variableCode", [])
            )

            parameter_code = None

            for code_item in variable_codes:
                value = str(code_item.get("value") or "").strip()

                if value in ("00065", "00011"):
                    parameter_code = value
                    break

            if parameter_code is None:
                continue

            values = (
                series_item
                .get("values", [{}])[0]
                .get("value", [])
            )

            for item in reversed(values):
                raw = item.get("value")
                observed_raw = item.get("dateTime")

                if raw in (None, "", "-999999") or not observed_raw:
                    continue

                try:
                    numeric_value = float(raw)

                    observed = datetime.fromisoformat(
                        observed_raw.replace("Z", "+00:00")
                    )

                    if observed.tzinfo is None:
                        observed = observed.replace(
                            tzinfo=timezone.utc
                        )

                    age_hours = (
                        now_utc
                        - observed.astimezone(timezone.utc)
                    ).total_seconds() / 3600.0

                except (TypeError, ValueError):
                    continue

                if age_hours < -0.25 or age_hours > 6.0:
                    print(
                        f"[HEFN] USGS {parameter_code} stale | "
                        f"age {age_hours:.1f} h | "
                        f"{observed_raw}",
                        flush=True,
                    )
                    break

                if parameter_code == "00065":
                    result["elevation"] = round(
                        numeric_value,
                        2
                    )

                    print(
                        f"[HEFN] USGS reservoir elevation | "
                        f"{result['elevation']:.2f} ft | "
                        f"age {age_hours:.1f} h",
                        flush=True,
                    )

                    break

                if parameter_code == "00011":
                    if not 32.0 <= numeric_value <= 110.0:
                        continue

                    result["temp_f"] = round(
                        numeric_value,
                        1
                    )

                    print(
                        f"[HEFN] USGS reservoir water temperature | "
                        f"{result['temp_f']:.1f} F | "
                        f"site {HEFNER_USGS_SITE} | "
                        f"age {age_hours:.1f} h | "
                        f"{observed_raw}",
                        flush=True,
                    )

                    break

    except Exception as exc:
        print(
            f"[HEFN] Combined USGS telemetry error: {exc}",
            flush=True,
        )

    return result

def get_db_connection():
    for attempt in range(1, 11):
        try:
            return psycopg2.connect(
                host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS, connect_timeout=5
            )
        except Exception as e:
            print(f"[{attempt}/10] Waiting for DB... ({e})")
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

            print(
                "[ODWC Regulations] Scheduler initialized.",
                flush=True
            )

        else:
            regs_age = (
                now_utc
                - last_regs.astimezone(timezone.utc)
            )

            if regs_age >= timedelta(days=7):
                try:
                    print(
                        "[ODWC Regulations] "
                        "7-day interval triggered.",
                        flush=True
                    )

                    sync_odwc_regs(conn)

                    _update_sync_state(
                        conn,
                        "odwc_regulations",
                        now_utc
                    )

                    print(
                        "[ODWC Regulations] "
                        "Synchronization completed.",
                        flush=True
                    )

                except Exception as exc:
                    conn.rollback()

                    print(
                        "[ODWC Regulations] "
                        f"Scheduler error: {exc}",
                        flush=True
                    )


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

            print(
                "[ODWC Species] Scheduler initialized.",
                flush=True
            )

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

                    print(
                        "[ODWC Species] "
                        "Synchronization completed.",
                        flush=True
                    )

                else:
                    print(
                        "[ODWC Species] "
                        "Sync failed with exit code "
                        f"{result.returncode}; "
                        "last_run unchanged.",
                        flush=True
                    )


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

                print(
                    "[Active Projects] "
                    "Synchronization completed.",
                    flush=True
                )

            else:
                print(
                    "[Active Projects] "
                    "Sync failed with exit code "
                    f"{result.returncode}; "
                    "last_run unchanged.",
                    flush=True
                )

    except Exception as exc:
        conn.rollback()

        print(
            f"[Maintenance] Scheduler error: {exc}",
            flush=True
        )

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
                result[lake["lake_code"]] = current

        print(
            f"[Open-Meteo] Batch weather loaded for "
            f"{len(result)}/{len(valid_lakes)} lakes",
            flush=True,
        )

        return result

    except Exception as exc:
        print(
            f"[Open-Meteo] Batch weather error: {exc}",
            flush=True,
        )

        return {}



def run_sync():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT
            lake_code,
            name,
            latitude,
            longitude
        FROM lakes
        ORDER BY lake_code;
    """)
    lakes = cur.fetchall()

    insert_sql = """
        INSERT INTO lake_readings (
            timestamp, lake_code, elevation_ft, diff_from_normal_ft,
            release_cfs, inflow_cfs, water_temp_f, air_temp_f,
            wind_speed_mph, wind_gust_mph, wind_direction_deg,
            surface_pressure_hpa,
            dissolved_oxygen_mg_l,
            cloud_cover_pct, precipitation_in, uv_index
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s
        );
    """

    now = datetime.now(timezone.utc)

    # ---------------------------------------------------------
    # Latest stored values.
    #
    # Used to carry forward slow-moving CWMS telemetry between
    # actual hourly polls and to survive a transient weather/API
    # failure without creating a mostly-null 15-minute row.
    # ---------------------------------------------------------
    cur.execute("""
        SELECT
            l.lake_code,
            r.timestamp,
            r.elevation_ft,
            r.diff_from_normal_ft,
            r.release_cfs,
            r.inflow_cfs,
            r.water_temp_f,
            r.air_temp_f,
            r.wind_speed_mph,
            r.wind_gust_mph,
            r.wind_direction_deg,
            r.surface_pressure_hpa,
            r.dissolved_oxygen_mg_l,
            r.cloud_cover_pct,
            r.precipitation_in,
            r.uv_index

        FROM lakes l

        LEFT JOIN LATERAL (
            SELECT
                timestamp,
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
                cloud_cover_pct,
                precipitation_in,
                uv_index

            FROM lake_readings

            WHERE lake_code = l.lake_code

            ORDER BY timestamp DESC
            LIMIT 1
        ) r ON TRUE

        ORDER BY l.lake_code;
    """)

    latest_by_lake = {
        row["lake_code"]: row
        for row in cur.fetchall()
    }

    # ---------------------------------------------------------
    # Determine which CWMS lakes actually need a real API poll.
    # ---------------------------------------------------------
    cur.execute("""
        SELECT lake_code, last_success
        FROM lake_source_status
        WHERE source = 'cwms';
    """)

    cwms_last_success = {
        row["lake_code"]: row["last_success"]
        for row in cur.fetchall()
    }

    cwms_due_codes = set()

    for lake in lakes:
        lake_code = lake["lake_code"]

        if lake_code == "HEFN":
            continue

        last_success = cwms_last_success.get(lake_code)

        if last_success is None:
            cwms_due_codes.add(lake_code)
            continue

        try:
            age_minutes = (
                now
                - last_success.astimezone(timezone.utc)
            ).total_seconds() / 60.0
        except Exception:
            cwms_due_codes.add(lake_code)
            continue

        if age_minutes >= CWMS_POLL_INTERVAL_MINUTES:
            cwms_due_codes.add(lake_code)

    print(
        f"[CWMS] Real poll due for "
        f"{len(cwms_due_codes)} lake(s)",
        flush=True,
    )

    # The large /levels request is only relevant when at least one
    # lake is actually polling CWMS. fetch_cwms_levels() then applies
    # its own 24-hour in-memory cache.
    cwms_levels = (
        fetch_cwms_levels()
        if cwms_due_codes
        else []
    )

    # ---------------------------------------------------------
    # ONE Open-Meteo current-weather request for all lakes.
    # ---------------------------------------------------------
    weather_by_lake = fetch_weather_batch(lakes)

    # ---------------------------------------------------------
    # Process each lake and write the normal 15-minute DB row.
    # ---------------------------------------------------------
    for lake in lakes:
        lake_code = lake["lake_code"]
        previous = latest_by_lake.get(lake_code) or {}

        water_data = {
            "elevation": None,
            "diff": None,
            "release_cfs": None,
            "inflow_cfs": None,
            "temp_f": None,
            "do": None,
        }

        # -----------------------------------------------------
        # 1. Weather
        # -----------------------------------------------------
        w_data = weather_by_lake.get(lake_code) or {}

        if w_data:
            mark_source_success(
                cur,
                lake_code,
                "weather",
                now,
            )
        else:
            # API failure fallback: carry forward the previous
            # weather observation while leaving source freshness
            # untouched.
            w_data = {
                "temperature_2m": previous.get(
                    "air_temp_f"
                ),
                "wind_speed_10m": previous.get(
                    "wind_speed_mph"
                ),
                "wind_gusts_10m": previous.get(
                    "wind_gust_mph"
                ),
                "wind_direction_10m": previous.get(
                    "wind_direction_deg"
                ),
                "surface_pressure": previous.get(
                    "surface_pressure_hpa"
                ),
                "cloud_cover": previous.get(
                    "cloud_cover_pct"
                ),
                "precipitation": previous.get(
                    "precipitation_in"
                ),
                "uv_index": previous.get(
                    "uv_index"
                ),
            }

            print(
                f"[{lake_code}] Weather API unavailable; "
                f"carrying forward previous stored values",
                flush=True,
            )

        # -----------------------------------------------------
        # 2. Lake Hefner: ONE USGS request for elevation + temp
        # -----------------------------------------------------
        if lake_code == "HEFN":
            hefner = fetch_hefner_telemetry()

            elevation = hefner.get("elevation")
            hefner_temp = hefner.get("temp_f")

            if elevation is not None:
                water_data["elevation"] = elevation
                water_data["diff"] = round(
                    elevation - HEFNER_NORMAL_POOL_FT,
                    2,
                )

                print(
                    f"[HEFN] USGS elevation "
                    f"{elevation:.2f} ft | "
                    f"reference {HEFNER_NORMAL_POOL_FT:.2f} ft | "
                    f"diff {water_data['diff']:+.2f} ft",
                    flush=True,
                )
            else:
                print(
                    "[HEFN] USGS elevation unavailable",
                    flush=True,
                )

            if hefner_temp is not None:
                water_data["temp_f"] = hefner_temp

            if (
                elevation is not None
                or hefner_temp is not None
            ):
                mark_source_success(
                    cur,
                    lake_code,
                    "usgs",
                    now,
                )

        # -----------------------------------------------------
        # 3. CWMS reservoirs
        # -----------------------------------------------------
        else:
            if lake_code in cwms_due_codes:
                actual_cwms_success = False

                # -----------------------------
                # Elevation
                # -----------------------------
                elevation = fetch_cwms_elevation(
                    lake_code
                )

                if elevation is not None:
                    actual_cwms_success = True
                    water_data["elevation"] = elevation

                    reference = fetch_cwms_reference_level(
                        lake_code,
                        now,
                        cwms_levels,
                    )

                    if reference is not None:
                        water_data["diff"] = round(
                            elevation - reference,
                            2,
                        )

                        print(
                            f"[{lake_code}] CWMS elevation "
                            f"{elevation:.2f} ft | "
                            f"reference {reference:.2f} ft | "
                            f"diff {water_data['diff']:+.2f} ft",
                            flush=True,
                        )

                    else:
                        old_elevation = previous.get(
                            "elevation_ft"
                        )
                        old_diff = previous.get(
                            "diff_from_normal_ft"
                        )

                        if (
                            old_elevation is not None
                            and old_diff is not None
                        ):
                            previous_reference = (
                                float(old_elevation)
                                - float(old_diff)
                            )

                            water_data["diff"] = round(
                                elevation
                                - previous_reference,
                                2,
                            )

                            print(
                                f"[{lake_code}] CWMS elevation "
                                f"{elevation:.2f} ft | "
                                f"reference "
                                f"{previous_reference:.2f} ft "
                                f"(cached DB reference) | "
                                f"diff "
                                f"{water_data['diff']:+.2f} ft",
                                flush=True,
                            )

                else:
                    # Preserve last valid hydrology value in the new
                    # 15-minute row, but do not refresh cwms freshness.
                    water_data["elevation"] = previous.get(
                        "elevation_ft"
                    )
                    water_data["diff"] = previous.get(
                        "diff_from_normal_ft"
                    )

                    print(
                        f"[{lake_code}] CWMS elevation unavailable; "
                        f"carrying forward previous value",
                        flush=True,
                    )

                # -----------------------------
                # Inflow / release
                # -----------------------------
                inflow = fetch_cwms_flow(
                    lake_code,
                    "inflow",
                )

                release = fetch_cwms_flow(
                    lake_code,
                    "release",
                )

                if inflow is not None:
                    actual_cwms_success = True
                    water_data["inflow_cfs"] = inflow
                else:
                    water_data["inflow_cfs"] = previous.get(
                        "inflow_cfs"
                    )

                if release is not None:
                    actual_cwms_success = True
                    water_data["release_cfs"] = release
                else:
                    water_data["release_cfs"] = previous.get(
                        "release_cfs"
                    )

                if actual_cwms_success:
                    mark_source_success(
                        cur,
                        lake_code,
                        "cwms",
                        now,
                    )

                print(
                    f"[{lake_code}] CWMS poll | "
                    f"inflow={water_data['inflow_cfs']} cfs | "
                    f"release={water_data['release_cfs']} cfs",
                    flush=True,
                )

            else:
                # Hourly CWMS series do not need to be downloaded again
                # on every 15-minute database cycle.
                water_data["elevation"] = previous.get(
                    "elevation_ft"
                )
                water_data["diff"] = previous.get(
                    "diff_from_normal_ft"
                )
                water_data["inflow_cfs"] = previous.get(
                    "inflow_cfs"
                )
                water_data["release_cfs"] = previous.get(
                    "release_cfs"
                )

                last_success = cwms_last_success.get(
                    lake_code
                )

                if last_success is not None:
                    try:
                        source_age = (
                            now
                            - last_success.astimezone(
                                timezone.utc
                            )
                        ).total_seconds() / 60.0

                        age_text = (
                            f"{source_age:.0f} min"
                        )
                    except Exception:
                        age_text = "unknown"
                else:
                    age_text = "unknown"

                print(
                    f"[{lake_code}] CWMS reuse | "
                    f"source age {age_text} | "
                    f"inflow={water_data['inflow_cfs']} cfs | "
                    f"release={water_data['release_cfs']} cfs",
                    flush=True,
                )

        # -----------------------------------------------------
        # 4. Store normal 15-minute reading
        # -----------------------------------------------------
        cur.execute(
            insert_sql,
            (
                now,
                lake_code,
                water_data["elevation"],
                water_data["diff"],
                water_data["release_cfs"],
                water_data["inflow_cfs"],
                water_data["temp_f"],
                w_data.get("temperature_2m"),
                w_data.get("wind_speed_10m"),
                w_data.get("wind_gusts_10m"),
                w_data.get("wind_direction_10m"),
                w_data.get("surface_pressure"),
                water_data["do"],
                w_data.get("cloud_cover"),
                w_data.get("precipitation"),
                w_data.get("uv_index"),
            ),
        )


        print(
            f"[{lake_code}] Sync complete",
            flush=True,
        )

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    print(
        "Multi-Lake Telemetry daemon started.",
        flush=True
    )

    # One-time schema initialization.
    schema_conn = get_db_connection()

    try:
        ensure_source_status_table(schema_conn)
        ensure_app_sync_state_table(schema_conn)

    finally:
        schema_conn.close()

    maintenance_last = None

    while True:
        try:
            run_sync()

        except Exception as exc:
            print(
                f"[Telemetry] Sync cycle failed: {exc}",
                flush=True
            )

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
                print(
                    "[Maintenance] "
                    f"Scheduler failure: {exc}",
                    flush=True
                )

            maintenance_last = time.monotonic()

        print(
            "Waiting 15 minutes for next scheduled cycle...",
            flush=True
        )

        time.sleep(900)

