"""Source validation and consistent time-based environmental features."""
import hashlib
import json
import math
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

MODEL_VERSION = 'quality-2026-10-06-v1'
USGS_BASE = 'https://api.waterdata.usgs.gov/ogcapi/v1/collections'
USGS_SERIES = {
    'ccfb0a6ea2324babbf008f4083d670b0': ('elevation_ft', '00065', 'ft'),
    '057d50ae5e28495988abcd9f411245ba': ('water_temp_f', '00011', 'degF'),
}
BOUNDS = {
    'elevation_ft': (300, 2000), 'water_temp_f': (32, 110),
    'air_temp_f': (-60, 140), 'wind_speed_mph': (0, 150),
    'wind_gust_mph': (0, 200), 'wind_direction_deg': (0, 360),
    'surface_pressure_hpa': (700, 1100), 'cloud_cover_pct': (0, 100),
    'precipitation_in': (0, 10), 'precipitation_1h_in': (0, 20),
    'relative_humidity_pct': (0, 100), 'uv_index': (0, 30),
    'inflow_cfs': (0, 500000), 'release_cfs': (0, 500000),
    'diff_from_normal_ft': (-100, 100),
}


def utc(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000 if value > 1e11 else value, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    # Legacy forecast timestamps were Central; new provider requests use epoch UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo('America/Chicago'))
    return dt.astimezone(timezone.utc)


def is_fresh(observed_at, now=None, max_hours=4):
    if not observed_at:
        return False
    try:
        age = (utc(now or datetime.now(timezone.utc)) - utc(observed_at)).total_seconds()/3600
        return -0.25 <= age <= max_hours
    except (ValueError, TypeError, OverflowError):
        return False


def valid_value(metric, value):
    try:
        value = float(value)
        lo, hi = BOUNDS[metric]
        return value if math.isfinite(value) and lo <= value <= hi else None
    except (ValueError, TypeError, KeyError):
        return None


def pressure_delta(times, values, when):
    """Three-hour point difference, interpolated only between nearby hourly points."""
    points = sorted((utc(t), valid_value('surface_pressure_hpa', v)) for t,v in zip(times,values))
    points = [(t,v) for t,v in points if v is not None]
    def at(target):
        for t,v in points:
            if t == target:
                return v
        for (a,va),(b,vb) in zip(points,points[1:]):
            if a <= target <= b and (b-a).total_seconds() <= 3600:
                return va + (vb-va)*(target-a).total_seconds()/(b-a).total_seconds()
        return None
    current = at(utc(when))
    previous = at(utc(when)-timedelta(hours=3))
    return round(current-previous, 3) if current is not None and previous is not None else None


def rain_hour(times, values, when):
    """Sum four complete preceding 15-minute accumulation intervals."""
    end = utc(when)
    points = {utc(t): valid_value('precipitation_in',v) for t,v in zip(times,values)}
    selected = [points.get(end-timedelta(minutes=i)) for i in (0,15,30,45)]
    return round(sum(selected),5) if all(v is not None for v in selected) else None


def usgs_observation(properties):
    spec = USGS_SERIES.get(properties.get('time_series_id'))
    if not spec or properties.get('monitoring_location_id') != 'USGS-07159550':
        return None
    metric, parameter, unit = spec
    if properties.get('parameter_code') != parameter or properties.get('unit_of_measure') != unit:
        return None
    value = valid_value(metric, properties.get('value'))
    if value is None or not properties.get('time'):
        return None
    return dict(metric=metric, value=value, observed_at=utc(properties['time']),
                source='usgs', payload=properties)


def fetch_usgs(collection='latest-continuous', begin=None, end=None):
    import requests
    params = {'monitoring_location_id':'USGS-07159550', 'limit':10000, 'f':'json'}
    if begin is not None:
        params['datetime'] = utc(begin).isoformat()+'/'+utc(end).isoformat()
    headers = {}
    if os.getenv('USGS_API_KEY'):
        headers['X-Api-Key'] = os.environ['USGS_API_KEY']
    url = USGS_BASE+'/'+collection+'/items'
    seen = set()
    while url:
        if url in seen:
            raise ValueError('USGS pagination loop')
        seen.add(url)
        # Only follow pagination within the authoritative API host.
        if not url.startswith('https://api.waterdata.usgs.gov/'):
            raise ValueError('Unexpected USGS pagination host')
        response = requests.get(url,params=params,headers=headers,timeout=30)
        response.raise_for_status()
        data = response.json()
        for feature in data.get('features',[]):
            observation = usgs_observation(feature.get('properties',{}))
            if observation is not None:
                yield observation
        url = next((link['href'] for link in data.get('links',[]) if link.get('rel')=='next'),None)
        params = None


def ensure_quality_schema(conn):
    with conn.cursor() as cur:
        for name, kind in [('quality','JSONB'),('relative_humidity_pct','NUMERIC'),
                           ('precipitation_1h_in','NUMERIC'),('pressure_delta_3h','NUMERIC')]:
            cur.execute(f'ALTER TABLE lake_readings ADD COLUMN IF NOT EXISTS {name} {kind}')
        cur.execute('''CREATE TABLE IF NOT EXISTS lake_metric_state (
            lake_code TEXT NOT NULL, metric TEXT NOT NULL, value NUMERIC NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL, retrieved_at TIMESTAMPTZ NOT NULL,
            source TEXT NOT NULL, payload JSONB NOT NULL, PRIMARY KEY(lake_code,metric))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS lake_observations (
            id BIGSERIAL PRIMARY KEY, lake_code TEXT NOT NULL, metric TEXT NOT NULL,
            value NUMERIC NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
            retrieved_at TIMESTAMPTZ NOT NULL, source TEXT NOT NULL,
            payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
            UNIQUE(lake_code,metric,source,observed_at,payload_hash))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS bite_predictions (
            id BIGSERIAL PRIMARY KEY, lake_code TEXT NOT NULL,
            captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), model_version TEXT NOT NULL,
            prediction_kind TEXT NOT NULL, payload JSONB NOT NULL, payload_hash TEXT NOT NULL,
            UNIQUE(lake_code,model_version,prediction_kind,payload_hash))''')
    conn.commit()


def record_observation(cur, lake, observation, retrieved_at, update_state=True):
    payload = json.dumps(observation['payload'],sort_keys=True,default=str)
    values=(lake,observation['metric'],observation['value'],observation['observed_at'],
            retrieved_at,observation['source'],payload)
    cur.execute('''INSERT INTO lake_observations
        (lake_code,metric,value,observed_at,retrieved_at,source,payload,payload_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        values+(hashlib.sha256(payload.encode()).hexdigest(),))
    if update_state:
        cur.execute('''INSERT INTO lake_metric_state
            (lake_code,metric,value,observed_at,retrieved_at,source,payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(lake_code,metric) DO UPDATE SET
            value=EXCLUDED.value, observed_at=EXCLUDED.observed_at,
            retrieved_at=EXCLUDED.retrieved_at,source=EXCLUDED.source,payload=EXCLUDED.payload
            WHERE EXCLUDED.observed_at >= lake_metric_state.observed_at''',values)


def record_prediction(conn, lake, kind, payload):
    encoded=json.dumps(payload,sort_keys=True,default=str)
    with conn.cursor() as cur:
        cur.execute('''INSERT INTO bite_predictions
            (lake_code,model_version,prediction_kind,payload,payload_hash)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (lake,MODEL_VERSION,kind,encoded,hashlib.sha256(encoded.encode()).hexdigest()))
    conn.commit()
