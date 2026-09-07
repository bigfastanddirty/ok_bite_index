from odwc_regs import check_and_sync_odwc_regs
import re

def fetch_usace_bulletin_release(lake_code):
    try:
        url = f"https://www.swt-wc.usace.army.mil/{lake_code.upper()}.lakepage.html"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if r.status_code == 200:
            text = r.text
            m = re.search(r'Reservoir release is\s*(?:<[^>]+>\s*)*([\d,\.]+)', text, re.IGNORECASE)
            if m:
                return float(m.group(1).replace(',', ''))
            m_alt = re.search(r'Total\s+Release[\s\S]*?([\d,\.]+)\s*(?:cfs)?', text, re.IGNORECASE)
            if m_alt:
                val = m_alt.group(1).replace(',', '')
                if val:
                    return float(val)
    except Exception:
        pass
    return None

import os
import sys
import time
import subprocess
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

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


def check_and_sync_odwc_species():
    """
    Run the standalone ODWC species scraper once per calendar month.

    Scheduler state is stored in PostgreSQL so container restarts/rebuilds
    do not reset the monthly schedule.
    """
    conn = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.app_sync_state (
                sync_name TEXT PRIMARY KEY,
                last_run TIMESTAMPTZ NOT NULL
            );
        """)
        conn.commit()

        cur.execute("""
            SELECT last_run
            FROM public.app_sync_state
            WHERE sync_name = 'odwc_species';
        """)
        row = cur.fetchone()

        # First scheduler run: mark the current month complete.
        # The species data was already populated manually in September 2026.
        if row is None:
            cur.execute("""
                INSERT INTO public.app_sync_state (sync_name, last_run)
                VALUES ('odwc_species', NOW());
            """)
            conn.commit()
            cur.close()

            print(
                "[ODWC Species] Monthly scheduler initialized; "
                "current month marked complete.",
                flush=True
            )
            return

        cur.execute("""
            SELECT
                date_trunc('month', last_run AT TIME ZONE 'America/Chicago')
                <
                date_trunc('month', NOW() AT TIME ZONE 'America/Chicago')
            FROM public.app_sync_state
            WHERE sync_name = 'odwc_species';
        """)
        should_run = cur.fetchone()[0]
        cur.close()

        if not should_run:
            return

        print(
            "[ODWC Species] New calendar month detected. "
            "Starting species synchronization...",
            flush=True
        )

        result = subprocess.run(
            [sys.executable, "/app/odwc_species.py"],
            check=False
        )

        if result.returncode != 0:
            print(
                f"[ODWC Species] Sync failed with exit code "
                f"{result.returncode}; last_run was NOT updated.",
                flush=True
            )
            return

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO public.app_sync_state (sync_name, last_run)
            VALUES ('odwc_species', NOW())
            ON CONFLICT (sync_name)
            DO UPDATE SET last_run = EXCLUDED.last_run;
        """)
        conn.commit()
        cur.close()

        print(
            "[ODWC Species] Monthly synchronization completed successfully.",
            flush=True
        )

    except Exception as e:
        if conn:
            conn.rollback()

        print(
            f"[ODWC Species] Monthly scheduler error: {e}",
            flush=True
        )

    finally:
        if conn:
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


def check_and_sync_active_projects():
    """
    Run the unified active-project scraper once per calendar day.

    Scheduler state is stored in public.app_sync_state so container
    restarts/rebuilds do not cause repeated runs.
    """
    conn = None

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS public.app_sync_state (
                sync_name TEXT PRIMARY KEY,
                last_run TIMESTAMPTZ NOT NULL
            );
        """)
        conn.commit()

        cur.execute("""
            SELECT
                date_trunc(
                    'day',
                    last_run AT TIME ZONE 'America/Chicago'
                )
                <
                date_trunc(
                    'day',
                    NOW() AT TIME ZONE 'America/Chicago'
                )
            FROM public.app_sync_state
            WHERE sync_name = 'active_projects';
        """)

        row = cur.fetchone()

        # First run, or a new local calendar day.
        should_run = (
            row is None
            or row[0] is True
        )

        cur.close()

        if not should_run:
            return

        print(
            "[Active Projects] Daily scheduler triggered. "
            "Starting project synchronization...",
            flush=True
        )

        result = subprocess.run(
            [sys.executable, "/app/active_projects.py"],
            check=False
        )

        if result.returncode != 0:
            print(
                f"[Active Projects] Sync failed with exit code "
                f"{result.returncode}; last_run was NOT updated.",
                flush=True
            )
            return

        cur = conn.cursor()

        cur.execute("""
            INSERT INTO public.app_sync_state (
                sync_name,
                last_run
            )
            VALUES (
                'active_projects',
                NOW()
            )
            ON CONFLICT (sync_name)
            DO UPDATE SET
                last_run = EXCLUDED.last_run;
        """)

        conn.commit()
        cur.close()

        print(
            "[Active Projects] Daily synchronization complete.",
            flush=True
        )

    except Exception as exc:
        print(
            f"[Active Projects] Daily scheduler error: {exc}",
            flush=True
        )

        if conn:
            conn.rollback()

    finally:
        if conn:
            conn.close()


def run_sync():
    conn = get_db_connection()
    ensure_source_status_table(conn)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT lake_code, name, usgs_site_id, latitude, longitude, normal_pool_ft FROM lakes;")
    lakes = cur.fetchall()

    insert_sql = """
    INSERT INTO lake_readings (
            timestamp, lake_code, elevation_ft, diff_from_normal_ft,
            release_cfs, inflow_cfs, water_temp_f, air_temp_f,
            wind_speed_mph, surface_pressure_hpa,
            dissolved_oxygen_mg_l, turbidity_fnu,
            cloud_cover_pct, precipitation_in, uv_index
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """

    now = datetime.now(timezone.utc)

    for lake in lakes:
        site = lake["usgs_site_id"]
        lat, lon = float(lake["latitude"]), float(lake["longitude"])
        normal_pool = float(lake["normal_pool_ft"])
        
        # 1. Fetch live Open-Meteo
        w_data = {}
        try:
            weather_url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,surface_pressure,wind_speed_10m,cloud_cover,precipitation,uv_index"
                f"&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
            )
            w_res = requests.get(weather_url, timeout=10).json()
            w_data = w_res.get("current", {})
            if w_data:
                mark_source_success(cur, lake["lake_code"], "weather", now)
        except Exception as e:
            print(f"Weather error for {lake['lake_code']}: {e}")

        # 2. Fetch live USGS parameters
        water_data = {
            "elevation": normal_pool,
            "diff": 0.0,
            "release_cfs": None,
            "inflow_cfs": None,
            "temp_f": None,
            "do": None,
            "turb": None
        }
        
        if site:
            try:
                usgs_url = f"https://waterservices.usgs.gov/nwis/iv/?format=json&sites={site}&parameterCd=00065,62614,00060,00010,00300,63680,00076"
                u_res = requests.get(usgs_url, timeout=12).json()
                series = u_res.get("value", {}).get("timeSeries", [])
                if series:
                    mark_source_success(cur, lake["lake_code"], "usgs", now)
                for s in series:
                    code = s["variable"]["variableCode"][0]["value"]
                    vals = s.get("values", [{}])[0].get("value", [])
                    if not vals:
                        continue
                    latest_raw = vals[-1].get("value")
                    if latest_raw in ["-999999", "", None]:
                        continue
                    val = float(latest_raw)

                    if code == "62614":
                        water_data["elevation"] = round(val, 2)
                        water_data["diff"] = round(val - normal_pool, 2)
                    elif code == "00065" and water_data["elevation"] == normal_pool:
                        if val > 300:
                            water_data["elevation"] = round(val, 2)
                            water_data["diff"] = round(val - normal_pool, 2)
                        else:
                            water_data["elevation"] = round(normal_pool + val, 2)
                            water_data["diff"] = round(val, 2)
                    elif code == "00060":
                        water_data["inflow_cfs"] = val
                        if water_data["release_cfs"] is None:
                            water_data["release_cfs"] = val
                        else:
                            water_data["cfs"] = val
                        water_data["cfs"] = val
                    elif code == "00010":
                        water_data["temp_f"] = round((val * 9/5) + 32, 1)
                    elif code == "00300":
                        water_data["do"] = val
                    elif code in ["63680", "00076"]:
                        water_data["turb"] = val
            except Exception as e:
                print(f"USGS error for {site}: {e}")

        # Check USACE bulletin for true dam outlet discharge
        usace_out = fetch_usace_bulletin_release(lake["lake_code"])
        if usace_out is not None:
            water_data["release_cfs"] = usace_out
            mark_source_success(cur, lake["lake_code"], "usace", now)

        cur.execute(insert_sql, (
            now,
            lake["lake_code"],
            water_data["elevation"],
            water_data["diff"],
            water_data["release_cfs"],
            water_data["inflow_cfs"],
            water_data["temp_f"],
            w_data.get("temperature_2m"),
            w_data.get("wind_speed_10m"),
            w_data.get("surface_pressure"),
            water_data["do"],
            water_data["turb"],
            w_data.get("cloud_cover"),
            w_data.get("precipitation"),
            w_data.get("uv_index")
        ))
        mark_source_success(cur, lake["lake_code"], "telemetry", now)
        print(f"[{lake['lake_code']}] Sync complete")

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("Multi-Lake Telemetry daemon started.")
    while True:
        run_sync()
        check_and_sync_odwc_regs()
        check_and_sync_odwc_species()
        check_and_sync_active_projects()
        print("Waiting 15 minutes for next scheduled cycle...")
        time.sleep(900)
