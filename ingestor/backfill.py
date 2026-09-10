#!/usr/bin/env python3
"""
Unified historical telemetry backfill for OK Bite Index.

Backfills the same authoritative fields used by the live ingestor without
updating source-freshness state or persisting modeled/estimated values.

Modes:
  --levels   CWMS reservoir elevation/reference-level difference for USACE lakes,
             plus Lake Hefner USGS elevation (00065) and measured reservoir
             temperature (00011).
  --flows    CWMS computed reservoir inflow and reservoir outflow/release.
  --weather  Open-Meteo historical surface weather used by the live ingestor.
  --all      Run levels, then flows, then weather.

Examples:
  python -u backfill.py --all --days 60
  python -u backfill.py --levels --lake HEFN --days 7 --dry-run
  python -u backfill.py --weather --start 2026-08-01 --end 2026-08-15
"""

import argparse
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

from ingest import (
    get_db_connection,
    fetch_cwms_levels,
    fetch_cwms_reference_level,
    CWMS_LAKE_MAP,
    CWMS_BASE_URL,
    CWMS_OFFICE,
    HEFNER_USGS_SITE,
    HEFNER_NORMAL_POOL_FT,
)


DEFAULT_DAYS = 60
CWMS_WINDOW_DAYS = 14
USGS_WINDOW_DAYS = 7
MAX_REASONABLE_FLOW_CFS = 500000.0

CWMS_ELEVATION_SUFFIX = "Elev.Inst.1Hour.0.Ccp-Rev"
CWMS_INFLOW_SUFFIX = "Flow-Res In.Ave.1Hour.1Hour.Rev-Regi-Computed"
CWMS_RELEASE_SUFFIX = "Flow-Res Out.Ave.1Hour.1Hour.Rev-Regi-Flowgroup"

USGS_HEFNER_ELEVATION = "00065"
USGS_HEFNER_WATER_TEMP = "00011"

HTTP_RETRY_ATTEMPTS = 5
HTTP_RETRY_BACKOFF_SECONDS = 2.0


def request_with_retry(method, url, *, label="HTTP request", **kwargs):
    """
    Perform an external HTTP request with bounded exponential backoff.

    Retries transient transport failures, HTTP 429, and HTTP 5xx responses.
    Other HTTP errors fail immediately.
    """
    retry_statuses = {429, 500, 502, 503, 504}
    last_error = None

    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            response = requests.request(
                method,
                url,
                **kwargs,
            )

            if response.status_code not in retry_statuses:
                response.raise_for_status()
                return response

            last_error = requests.HTTPError(
                f"{response.status_code} Server Error",
                response=response,
            )

            if attempt == HTTP_RETRY_ATTEMPTS:
                response.raise_for_status()

            delay = HTTP_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))

            print(
                f"{label} transient HTTP {response.status_code} | "
                f"attempt {attempt}/{HTTP_RETRY_ATTEMPTS} | "
                f"retrying in {delay:.1f}s",
                flush=True,
            )

            time.sleep(delay)

        except (
            requests.Timeout,
            requests.ConnectionError,
        ) as exc:
            last_error = exc

            if attempt == HTTP_RETRY_ATTEMPTS:
                raise

            delay = HTTP_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))

            print(
                f"{label} transient request error: {exc} | "
                f"attempt {attempt}/{HTTP_RETRY_ATTEMPTS} | "
                f"retrying in {delay:.1f}s",
                flush=True,
            )

            time.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f"{label} failed without a response")


OPEN_METEO_HOURLY_FIELDS = (
    "temperature_2m",
    "surface_pressure",
    "wind_speed_10m",
    "wind_gusts_10m",
    "wind_direction_10m",
    "cloud_cover",
    "precipitation",
    "uv_index",
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill historical OK Bite Index telemetry."
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--all", action="store_true", help="Run all backfills")
    mode.add_argument("--levels", action="store_true", help="Backfill reservoir levels")
    mode.add_argument("--flows", action="store_true", help="Backfill CWMS flows")
    mode.add_argument("--weather", action="store_true", help="Backfill historical weather")

    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--start",
        help="UTC start date/time (YYYY-MM-DD or ISO-8601). Overrides --days.",
    )
    parser.add_argument(
        "--end",
        help="UTC end date/time (YYYY-MM-DD or ISO-8601). Default: now.",
    )
    parser.add_argument(
        "--lake",
        action="append",
        dest="lakes",
        help="Limit to a lake code. Repeat for multiple lakes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate data but do not modify lake_readings.",
    )

    args = parser.parse_args()

    if args.days <= 0:
        parser.error("--days must be greater than zero")

    return args


def parse_utc(value, end_of_date=False):
    if not value:
        return None

    raw = value.strip()

    try:
        if len(raw) == 10:
            parsed = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            if end_of_date:
                parsed += timedelta(days=1)
            return parsed

        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    except ValueError as exc:
        raise SystemExit(f"Invalid date/time {value!r}: {exc}")


def resolve_window(args):
    end = parse_utc(args.end, end_of_date=True) or datetime.now(timezone.utc)
    begin = parse_utc(args.start) if args.start else end - timedelta(days=args.days)

    if begin >= end:
        raise SystemExit("Backfill start must be before end")

    # Current historical telemetry is hourly for CWMS/Open-Meteo. Align the
    # beginning to the hour while leaving the end at the requested instant.
    begin = begin.replace(minute=0, second=0, microsecond=0)

    return begin, end


def requested_lakes(args, db_lakes):
    known = {row[0].upper() for row in db_lakes}

    if not args.lakes:
        return known

    selected = {code.strip().upper() for code in args.lakes if code.strip()}
    unknown = sorted(selected - known)

    if unknown:
        raise SystemExit(f"Unknown lake code(s): {', '.join(unknown)}")

    return selected


def cwms_parse_values(payload):
    observations = {}

    for row in payload.get("values", []):
        if not isinstance(row, list) or len(row) < 2:
            continue

        raw_ts, raw_value = row[0], row[1]
        if raw_ts is None or raw_value is None:
            continue

        try:
            ts = datetime.fromtimestamp(float(raw_ts) / 1000.0, tz=timezone.utc)
            value = float(raw_value)
        except (TypeError, ValueError, OSError, OverflowError):
            continue

        observations[ts] = value

    return observations


def fetch_cwms_series(series_name, unit, begin, end, lake_code, label):
    observations = {}
    window_begin = begin
    window_number = 0
    window_size = timedelta(days=CWMS_WINDOW_DAYS)

    while window_begin < end:
        window_end = min(window_begin + window_size, end)
        window_number += 1

        response = requests.get(
            f"{CWMS_BASE_URL}/timeseries",
            params={
                "office": CWMS_OFFICE,
                "name": series_name,
                "begin": window_begin.isoformat(),
                "end": window_end.isoformat(),
                "unit": unit,
                "page-size": 500,
            },
            headers={"Accept": "application/json;version=2"},
            timeout=60,
        )
        response.raise_for_status()

        rows = cwms_parse_values(response.json())
        observations.update(rows)

        print(
            f"[{lake_code}] {label} window {window_number}: "
            f"{window_begin.isoformat()} -> {window_end.isoformat()} | "
            f"{len(rows)} observations",
            flush=True,
        )

        window_begin = window_end
        time.sleep(0.15)

    print(
        f"[{lake_code}] {label} returned {len(observations)} unique observations",
        flush=True,
    )

    return observations


def fetch_hefner_history(begin, end):
    """Return {timestamp: {elevation_ft, water_temp_f}} from verified USGS reservoir telemetry."""
    observations = {}
    window_begin = begin
    window_number = 0
    window_size = timedelta(days=USGS_WINDOW_DAYS)

    while window_begin < end:
        window_end = min(window_begin + window_size, end)
        window_number += 1

        response = request_with_retry(
            "GET",
            "https://waterservices.usgs.gov/nwis/iv/",
            label=f"[HEFN] USGS window {window_number}",
            params={
                "format": "json",
                "sites": HEFNER_USGS_SITE,
                "startDT": window_begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDT": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "parameterCd": f"{USGS_HEFNER_ELEVATION},{USGS_HEFNER_WATER_TEMP}",
                "siteStatus": "all",
            },
            timeout=60,
        )

        window_count = 0

        for series in response.json().get("value", {}).get("timeSeries", []):
            variable_codes = {
                str(item.get("value") or "").strip()
                for item in series.get("variable", {}).get("variableCode", [])
            }

            parameter_code = None
            if USGS_HEFNER_ELEVATION in variable_codes:
                parameter_code = USGS_HEFNER_ELEVATION
            elif USGS_HEFNER_WATER_TEMP in variable_codes:
                parameter_code = USGS_HEFNER_WATER_TEMP

            if parameter_code is None:
                continue

            for block in series.get("values", []):
                for item in block.get("value", []):
                    raw_value = item.get("value")
                    raw_time = item.get("dateTime")

                    if raw_value in (None, "", "-999999") or not raw_time:
                        continue

                    try:
                        value = float(raw_value)
                        ts = datetime.fromisoformat(
                            raw_time.replace("Z", "+00:00")
                        ).astimezone(timezone.utc)
                    except (TypeError, ValueError):
                        continue

                    if ts < begin or ts > end:
                        continue

                    rec = observations.setdefault(
                        ts,
                        {"elevation_ft": None, "water_temp_f": None},
                    )

                    if parameter_code == USGS_HEFNER_ELEVATION:
                        rec["elevation_ft"] = value
                    else:
                        rec["water_temp_f"] = value

                    window_count += 1

        print(
            f"[HEFN] USGS window {window_number}: "
            f"{window_begin.isoformat()} -> {window_end.isoformat()} | "
            f"{window_count} values",
            flush=True,
        )

        window_begin = window_end
        time.sleep(0.15)

    print(
        f"[HEFN] USGS returned {len(observations)} unique timestamps",
        flush=True,
    )

    return observations


def upsert_level(cur, lake_code, ts, elevation_ft, diff_ft, dry_run):
    if dry_run:
        return 1

    cur.execute(
        """
        INSERT INTO lake_readings (
            timestamp,
            lake_code,
            elevation_ft,
            diff_from_normal_ft
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (lake_code, timestamp)
        DO UPDATE SET
            elevation_ft = EXCLUDED.elevation_ft,
            diff_from_normal_ft = EXCLUDED.diff_from_normal_ft;
        """,
        (ts, lake_code, round(elevation_ft, 2), round(diff_ft, 2)),
    )

    return cur.rowcount


def upsert_hefner(cur, ts, data, dry_run):
    elevation = data.get("elevation_ft")
    temp_f = data.get("water_temp_f")

    if elevation is None and temp_f is None:
        return 0

    diff = (
        round(float(elevation) - HEFNER_NORMAL_POOL_FT, 2)
        if elevation is not None
        else None
    )

    if dry_run:
        return 1

    cur.execute(
        """
        INSERT INTO lake_readings (
            timestamp,
            lake_code,
            elevation_ft,
            diff_from_normal_ft,
            water_temp_f
        )
        VALUES (%s, 'HEFN', %s, %s, %s)
        ON CONFLICT (lake_code, timestamp)
        DO UPDATE SET
            elevation_ft = COALESCE(EXCLUDED.elevation_ft, lake_readings.elevation_ft),
            diff_from_normal_ft = COALESCE(EXCLUDED.diff_from_normal_ft, lake_readings.diff_from_normal_ft),
            water_temp_f = COALESCE(EXCLUDED.water_temp_f, lake_readings.water_temp_f);
        """,
        (
            ts,
            round(float(elevation), 2) if elevation is not None else None,
            diff,
            round(float(temp_f), 1) if temp_f is not None else None,
        ),
    )

    return cur.rowcount


def run_levels(conn, selected_lakes, begin, end, dry_run):
    print("\n" + "=" * 70)
    print("LEVEL BACKFILL")
    print("=" * 70)

    cur = conn.cursor()
    total_written = 0
    total_reference_skipped = 0

    cwms_selected = [
        code for code in CWMS_LAKE_MAP
        if code in selected_lakes
    ]

    cwms_levels = []
    if cwms_selected:
        print("[CWMS] Downloading reference-level definitions...", flush=True)
        cwms_levels = fetch_cwms_levels()
        print(
            f"[CWMS] Loaded {len(cwms_levels)} reference-level definitions",
            flush=True,
        )

    try:
        for lake_code in cwms_selected:
            cwms_id = CWMS_LAKE_MAP[lake_code]
            series_name = f"{cwms_id}.{CWMS_ELEVATION_SUFFIX}"

            try:
                observations = fetch_cwms_series(
                    series_name,
                    "ft",
                    begin,
                    end,
                    lake_code,
                    "ELEVATION",
                )

                written = 0
                skipped = 0

                for ts, elevation in sorted(observations.items()):
                    reference = fetch_cwms_reference_level(
                        lake_code,
                        ts,
                        cwms_levels,
                    )

                    if reference is None:
                        skipped += 1
                        continue

                    written += upsert_level(
                        cur,
                        lake_code,
                        ts,
                        elevation,
                        float(elevation) - float(reference),
                        dry_run,
                    )

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                total_written += written
                total_reference_skipped += skipped

                print(
                    f"[{lake_code}] rows={'would_write' if dry_run else 'written'}={written} "
                    f"reference_skipped={skipped}",
                    flush=True,
                )

            except Exception as exc:
                conn.rollback()
                print(f"[{lake_code}] LEVEL BACKFILL ERROR: {exc}", flush=True)

            time.sleep(0.20)

        if "HEFN" in selected_lakes:
            try:
                observations = fetch_hefner_history(begin, end)
                written = 0

                for ts, data in sorted(observations.items()):
                    written += upsert_hefner(cur, ts, data, dry_run)

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                total_written += written

                measured_temp_rows = sum(
                    1 for data in observations.values()
                    if data.get("water_temp_f") is not None
                )

                print(
                    f"[HEFN] rows={'would_write' if dry_run else 'written'}={written} "
                    f"measured_temp_timestamps={measured_temp_rows}",
                    flush=True,
                )

            except Exception as exc:
                conn.rollback()
                print(f"[HEFN] LEVEL BACKFILL ERROR: {exc}", flush=True)

    finally:
        cur.close()

    print(
        f"LEVELS COMPLETE | {'would_write' if dry_run else 'written'}={total_written} "
        f"reference_skipped={total_reference_skipped}",
        flush=True,
    )


def flow_is_reasonable(value):
    return abs(float(value)) <= MAX_REASONABLE_FLOW_CFS


def upsert_flow(cur, lake_code, ts, column, value, dry_run):
    if column not in ("inflow_cfs", "release_cfs"):
        raise ValueError(f"Unsupported flow column: {column}")

    if dry_run:
        return 1

    # Column is selected only from the fixed allow-list above.
    cur.execute(
        f"""
        INSERT INTO lake_readings (
            timestamp,
            lake_code,
            {column}
        )
        VALUES (%s, %s, %s)
        ON CONFLICT (lake_code, timestamp)
        DO UPDATE SET
            {column} = EXCLUDED.{column};
        """,
        (ts, lake_code, round(float(value), 1)),
    )

    return cur.rowcount


def run_flows(conn, selected_lakes, begin, end, dry_run):
    print("\n" + "=" * 70)
    print("FLOW BACKFILL")
    print("=" * 70)

    cur = conn.cursor()
    total_inflow = 0
    total_release = 0
    total_skipped = 0

    try:
        for lake_code, cwms_id in CWMS_LAKE_MAP.items():
            if lake_code not in selected_lakes:
                continue

            inflow_series = f"{cwms_id}.{CWMS_INFLOW_SUFFIX}"
            release_series = f"{cwms_id}.{CWMS_RELEASE_SUFFIX}"

            try:
                inflow = fetch_cwms_series(
                    inflow_series,
                    "cfs",
                    begin,
                    end,
                    lake_code,
                    "INFLOW",
                )
                release = fetch_cwms_series(
                    release_series,
                    "cfs",
                    begin,
                    end,
                    lake_code,
                    "OUTFLOW",
                )

                inflow_written = 0
                release_written = 0
                skipped = 0

                for ts, value in sorted(inflow.items()):
                    if not flow_is_reasonable(value):
                        skipped += 1
                        print(
                            f"[{lake_code}] SKIP implausible inflow "
                            f"{value:.1f} cfs at {ts.isoformat()}",
                            flush=True,
                        )
                        continue

                    inflow_written += upsert_flow(
                        cur,
                        lake_code,
                        ts,
                        "inflow_cfs",
                        value,
                        dry_run,
                    )

                for ts, value in sorted(release.items()):
                    if not flow_is_reasonable(value):
                        skipped += 1
                        print(
                            f"[{lake_code}] SKIP implausible release "
                            f"{value:.1f} cfs at {ts.isoformat()}",
                            flush=True,
                        )
                        continue

                    release_written += upsert_flow(
                        cur,
                        lake_code,
                        ts,
                        "release_cfs",
                        value,
                        dry_run,
                    )

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                total_inflow += inflow_written
                total_release += release_written
                total_skipped += skipped

                print(
                    f"[{lake_code}] inflow_{'would_write' if dry_run else 'written'}={inflow_written} "
                    f"release_{'would_write' if dry_run else 'written'}={release_written} "
                    f"implausible_skipped={skipped}",
                    flush=True,
                )

            except Exception as exc:
                conn.rollback()
                print(f"[{lake_code}] FLOW BACKFILL ERROR: {exc}", flush=True)

            time.sleep(0.20)

    finally:
        cur.close()

    print(
        f"FLOWS COMPLETE | inflow_{'would_write' if dry_run else 'written'}={total_inflow} "
        f"release_{'would_write' if dry_run else 'written'}={total_release} "
        f"implausible_skipped={total_skipped}",
        flush=True,
    )


def fetch_weather_history(lat, lon, start_date, end_date, begin, end):
    response = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start_date,
            "end_date": end_date,
            "hourly": ",".join(OPEN_METEO_HOURLY_FIELDS),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": "UTC",
        },
        timeout=60,
    )
    response.raise_for_status()

    hourly = response.json().get("hourly", {})
    times = hourly.get("time", [])
    rows = []

    field_map = {
        "temperature_2m": "air_temp_f",
        "surface_pressure": "surface_pressure_hpa",
        "wind_speed_10m": "wind_speed_mph",
        "wind_gusts_10m": "wind_gust_mph",
        "wind_direction_10m": "wind_direction_deg",
        "cloud_cover": "cloud_cover_pct",
        "precipitation": "precipitation_in",
        "uv_index": "uv_index",
    }

    for i, ts_raw in enumerate(times):
        try:
            ts = datetime.fromisoformat(
                ts_raw + ":00+00:00"
                if len(ts_raw) == 16
                else ts_raw.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue

        if ts < begin or ts > end:
            continue

        row = {"timestamp": ts}

        for source_field, db_field in field_map.items():
            values = hourly.get(source_field, [])
            row[db_field] = values[i] if i < len(values) else None

        rows.append(row)

    return rows


def upsert_weather(cur, lake_code, row, dry_run):
    if dry_run:
        return 1

    cur.execute(
        """
        INSERT INTO lake_readings (
            timestamp,
            lake_code,
            air_temp_f,
            wind_speed_mph,
            wind_gust_mph,
            wind_direction_deg,
            surface_pressure_hpa,
            cloud_cover_pct,
            precipitation_in,
            uv_index
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (lake_code, timestamp)
        DO UPDATE SET
            air_temp_f = EXCLUDED.air_temp_f,
            wind_speed_mph = EXCLUDED.wind_speed_mph,
            wind_gust_mph = EXCLUDED.wind_gust_mph,
            wind_direction_deg = EXCLUDED.wind_direction_deg,
            surface_pressure_hpa = EXCLUDED.surface_pressure_hpa,
            cloud_cover_pct = EXCLUDED.cloud_cover_pct,
            precipitation_in = EXCLUDED.precipitation_in,
            uv_index = EXCLUDED.uv_index;
        """,
        (
            row["timestamp"],
            lake_code,
            row.get("air_temp_f"),
            row.get("wind_speed_mph"),
            row.get("wind_gust_mph"),
            row.get("wind_direction_deg"),
            row.get("surface_pressure_hpa"),
            row.get("cloud_cover_pct"),
            row.get("precipitation_in"),
            row.get("uv_index"),
        ),
    )

    return cur.rowcount


def run_weather(conn, selected_lakes, db_lakes, begin, end, dry_run):
    print("\n" + "=" * 70)
    print("WEATHER BACKFILL")
    print("=" * 70)

    start_date = begin.date().isoformat()
    end_date = end.date().isoformat()

    cur = conn.cursor()
    total_written = 0

    try:
        for lake_code, lat, lon in db_lakes:
            lake_code = lake_code.upper()

            if lake_code not in selected_lakes:
                continue

            if lat is None or lon is None:
                print(f"[{lake_code}] Missing coordinates; skipping", flush=True)
                continue

            try:
                rows = fetch_weather_history(
                    float(lat),
                    float(lon),
                    start_date,
                    end_date,
                    begin,
                    end,
                )

                written = 0
                for row in rows:
                    written += upsert_weather(cur, lake_code, row, dry_run)

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                total_written += written

                print(
                    f"[{lake_code}] weather_{'would_write' if dry_run else 'written'}={written}",
                    flush=True,
                )

            except Exception as exc:
                conn.rollback()
                print(f"[{lake_code}] WEATHER BACKFILL ERROR: {exc}", flush=True)

            time.sleep(0.15)

    finally:
        cur.close()

    print(
        f"WEATHER COMPLETE | {'would_write' if dry_run else 'written'}={total_written}",
        flush=True,
    )


def main():
    args = parse_args()
    begin, end = resolve_window(args)

    conn = get_db_connection()

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT lake_code, latitude, longitude
            FROM lakes
            ORDER BY lake_code;
            """
        )
        db_lakes = cur.fetchall()
        cur.close()

        selected_lakes = requested_lakes(args, db_lakes)

        print("=" * 70)
        print("OK BITE INDEX UNIFIED HISTORICAL BACKFILL")
        print(f"Begin:   {begin.isoformat()}")
        print(f"End:     {end.isoformat()}")
        print(f"Lakes:   {', '.join(sorted(selected_lakes))}")
        print(f"Dry run: {'YES' if args.dry_run else 'NO'}")
        print("=" * 70)

        if args.all or args.levels:
            run_levels(conn, selected_lakes, begin, end, args.dry_run)

        if args.all or args.flows:
            run_flows(conn, selected_lakes, begin, end, args.dry_run)

        if args.all or args.weather:
            run_weather(
                conn,
                selected_lakes,
                db_lakes,
                begin,
                end,
                args.dry_run,
            )

    finally:
        conn.close()

    print("\nBackfill finished.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr, flush=True)
        raise SystemExit(130)

