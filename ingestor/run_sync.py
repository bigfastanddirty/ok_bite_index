import os, sys, requests, psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone

DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

conn = psycopg2.connect(
    host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
)
cur = conn.cursor(cursor_factory=RealDictCursor)
cur.execute("SELECT lake_code, name, usgs_site_id, latitude, longitude, normal_pool_ft FROM lakes;")
lakes = cur.fetchall()

print(f"Syncing {len(lakes)} lakes with live multi-sensor data...")

insert_sql = """
INSERT INTO lake_readings (
    timestamp, lake_code, elevation_ft, diff_from_normal_ft,
    release_cfs, water_temp_f, air_temp_f,
    wind_speed_mph, surface_pressure_hpa,
    dissolved_oxygen_mg_l, turbidity_fnu,
    cloud_cover_pct, precipitation_in, uv_index
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
"""

now = datetime.now(timezone.utc)

for lake in lakes:
    site = lake["usgs_site_id"]
    lat, lon = float(lake["latitude"]), float(lake["longitude"])
    normal_pool = float(lake["normal_pool_ft"])
    
    # 1. Fetch live Open-Meteo
    weather_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,surface_pressure,wind_speed_10m,cloud_cover,precipitation,uv_index&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch"
    w_data = {}
    try:
        w_res = requests.get(weather_url, timeout=10).json()
        w_data = w_res.get("current", {})
    except Exception as e:
        print(f"Weather error for {lake['lake_code']}: {e}")

    # 2. Fetch live USGS parameters
    water_data = {
        "elevation": normal_pool,
        "diff": 0.0,
        "cfs": None,
        "temp_f": None,
        "do": None,
        "turb": None
    }
    
    if site:
        usgs_url = f"https://waterservices.usgs.gov/nwis/iv/?format=json&sites={site}&parameterCd=00065,62614,00060,00010,00300,63680,00076"
        try:
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
                    water_data["cfs"] = val
                elif code == "00010":
                    water_data["temp_f"] = round((val * 9/5) + 32, 1)
                elif code == "00300":
                    water_data["do"] = val
                elif code in ["63680", "00076"]:
                    water_data["turb"] = val
        except Exception as e:
            print(f"USGS error for {site}: {e}")

    cur.execute(insert_sql, (
        now,
        lake["lake_code"],
        water_data["elevation"],
        water_data["diff"],
        water_data["cfs"],
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
    print(f"-> Inserted {lake['lake_code']} ({lake['name']}): Elev={water_data['elevation']}ft, Temp={water_data['temp_f']}F, DO={water_data['do']}")

conn.commit()
cur.close()
conn.close()
print("Multi-sensor live sync complete.")

# --- ODWC Regulations Periodic Sync ---
try:
    import time
    from odwc_regs import sync_odwc_regs
    LOCK_FILE = "/tmp/odwc_regs_sync.timestamp"
    WEEK_IN_SECONDS = 7 * 24 * 3600

    should_run = True
    if os.path.exists(LOCK_FILE):
        with open(LOCK_FILE, "r") as lf:
            last_run = float(lf.read().strip() or 0)
            if time.time() - last_run < WEEK_IN_SECONDS:
                should_run = False

    if should_run:
        sync_odwc_regs(conn)
        with open(LOCK_FILE, "w") as lf:
            lf.write(str(time.time()))
except Exception as e:
    print(f"Failed to execute ODWC regulations sync: {e}")
