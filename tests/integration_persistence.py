"""Run explicitly against the disposable accuracy_test database; not test discovery."""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'ingestor'))
import ingest
import backfill
from quality import ensure_quality_schema,record_observation

conn=ingest.get_db_connection()
if conn.info.dbname != 'accuracy_test':
    conn.close()
    raise RuntimeError('Persistence checks require a disposable database named accuracy_test')
ensure_quality_schema(conn)
cur=conn.cursor()
now=datetime.now(timezone.utc)
metric=dict(metric='air_temp_f',value=80,observed_at=now,source='weather',payload={'test':'original'})
record_observation(cur,'TEST',metric,now)
record_observation(cur,'TEST',metric,now)
old=dict(metric,value=60,observed_at=now-timedelta(hours=3),payload={'test':'older'})
record_observation(cur,'TEST',old,now)
cur.execute("SELECT value FROM lake_metric_state WHERE lake_code='TEST' AND metric='air_temp_f'")
assert float(cur.fetchone()[0])==80,'Older observation replaced newer state'
cur.execute("SELECT count(*) FROM lake_observations WHERE lake_code='TEST'")
assert cur.fetchone()[0]==2,'Raw observation deduplication failed'
cur.execute('INSERT INTO lake_readings(timestamp,lake_code,air_temp_f) VALUES(%s,%s,%s)',(now,'TEST',80))
backfill.upsert_weather(cur,'TEST',{'timestamp':now,'air_temp_f':60},False)
cur.execute("SELECT air_temp_f FROM lake_readings WHERE lake_code='TEST' AND timestamp=%s",(now,))
assert float(cur.fetchone()[0])==80,'Reanalysis overwrote the original live observation'
cur.execute("SELECT count(*) FROM lake_observations WHERE lake_code='TEST' AND source='weather_reanalysis'")
assert cur.fetchone()[0]==1,'Reanalysis revision was not preserved separately'
conn.rollback()
conn.close()
print('PASS: original observations preserved, revisions archived, duplicate observations deduplicated')
