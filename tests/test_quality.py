import importlib.util
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

class QualityContracts(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'ingestor/quality.py'
        self.assertTrue(path.exists(), 'metric validation module missing')
        spec = importlib.util.spec_from_file_location('quality', path)
        self.q = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.q)

    def test_stale_and_future_values_rejected(self):
        now = datetime.now(timezone.utc)
        for age in [timedelta(hours=5), -timedelta(hours=1)]:
            self.assertFalse(self.q.is_fresh(now-age, now, 4))
        self.assertTrue(self.q.is_fresh(now-timedelta(hours=1), now, 4))

    def test_nonfinite_and_invalid_measurements_rejected(self):
        for value in [float('nan'), float('inf'), -1, 101]:
            self.assertIsNone(self.q.valid_value('cloud_cover_pct', value))
        self.assertEqual(self.q.valid_value('cloud_cover_pct', 50), 50)

    def test_pressure_window_uses_elapsed_time(self):
        t = datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
        times = [t-timedelta(hours=i) for i in [3,2,1,0]]
        self.assertAlmostEqual(self.q.pressure_delta(times, [1000,999.5,999,998.5],t),-1.5)
        self.assertIsNone(self.q.pressure_delta(times[2:], [999,998.5],t))

    def test_incomplete_rainfall_window_is_unknown(self):
        t=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)
        times=[t-timedelta(minutes=i) for i in [45,30,15,0]]
        self.assertAlmostEqual(self.q.rain_hour(times,[.025]*4,t),.1)
        self.assertIsNone(self.q.rain_hour(times[:-1],[.025]*3,t))

    def test_usgs_wrong_series_and_units_rejected(self):
        p={'time_series_id':'incorrect','monitoring_location_id':'USGS-07159550','parameter_code':'00065','unit_of_measure':'ft','time':datetime.now(timezone.utc).isoformat(),'value':'1192'}
        self.assertIsNone(self.q.usgs_observation(p))
        p['time_series_id']='ccfb0a6ea2324babbf008f4083d670b0'
        self.assertEqual(self.q.usgs_observation(p)['value'],1192)
        p['unit_of_measure']='m'
        self.assertIsNone(self.q.usgs_observation(p))
