import os
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone

DB_HOST = os.getenv("DB_HOST", "ok_lakes_db")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("POSTGRES_DB", "ok_fishing_db")
DB_USER = os.getenv("POSTGRES_USER", "lake_admin")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "")

def get_db():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS
    )

def backfill_weather_30_days():
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT lake_code, name, latitude, longitude FROM lakes;")
    lakes = cur.fetchall()
    
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
    
    print(f"Starting 35-day weather backfill ({start_date} to {end_date})...")

    update_sql = """
    UPDATE lake_readings
    SET cloud_cover_pct = %s,
        precipitation_in = %s,
        uv_index = %s
    WHERE lake_code = %s
      AND timestamp >= %s::timestamptz - INTERVAL '30 minutes'
      AND timestamp <= %s::timestamptz + INTERVAL '30 minutes';
    """

    for lake in lakes:
        code = lake["lake_code"]
        lat, lon = float(lake["latitude"]), float(lake["longitude"])
        
        url = (
            f"https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={lat}&longitude={lon}&start_date={start_date}&end_date={end_date}"
            f"&hourly=cloud_cover,precipitation,uv_index"
            f"&precipitation_unit=inch"
        )
        
        try:
            res = requests.get(url, timeout=15).json()
            hourly = res.get("hourly", {})
            times = hourly.get("time", [])
            clouds = hourly.get("cloud_cover", [])
            precip = hourly.get("precipitation", [])
            uvs = hourly.get("uv_index", [])
            
            matched_count = 0
            for i in range(len(times)):
                ts_str = times[i] + ":00Z" # e.g. "2026-08-01T12:00:00Z"
                c_val = clouds[i] if clouds[i] is not None else 0.0
                p_val = precip[i] if precip[i] is not None else 0.0
                u_val = uvs[i] if uvs[i] is not None else 0.0
                
                cur.execute(update_sql, (c_val, p_val, u_val, code, ts_str, ts_str))
                matched_count += cur.rowcount
                
            conn.commit()
            print(f"[{code}] Backfilled {lake['name']}: {matched_count} rows updated.")
            time.sleep(1) # API courtesy pause
        except Exception as e:
            print(f"[{code}] Error backfilling: {e}")
            conn.rollback()

    cur.close()
    conn.close()
    print("30-Day Historical Weather Backfill Complete!")

if __name__ == "__main__":
    backfill_weather_30_days()
