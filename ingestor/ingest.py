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
import time
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

def run_sync():
    conn = get_db_connection()
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
        print(f"[{lake['lake_code']}] Sync complete")

    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    print("Multi-Lake Telemetry daemon started.")
    while True:
        run_sync()
        check_and_sync_odwc_regs()
        print("Waiting 15 minutes for next scheduled cycle...")
        time.sleep(900)
