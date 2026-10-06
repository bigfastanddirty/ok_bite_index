import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import astronomy
import ephem

class AstronomyChecks(unittest.TestCase):
    def test_events_agree_with_usno_fixture(self):
        # USNO /api/rstt/oneday, 2026-10-06, 35.5852,-97.5992, tz=-5.
        t=datetime(2026,10,6,12,tzinfo=timezone.utc)
        for body,event,expected in [(ephem.Sun(),'rising','2026-10-06T12:29:00+00:00'),
                                     (ephem.Sun(),'setting','2026-10-07T00:07:00+00:00'),
                                     (ephem.Moon(),'transit','2026-10-06T15:13:00+00:00'),
                                     (ephem.Moon(),'rising','2026-10-06T08:12:00+00:00'),
                                     (ephem.Moon(),'setting','2026-10-06T22:02:00+00:00')]:
            events=astronomy.nearest_events(t,-97.5992,35.5852,body,event)
            target=datetime.fromisoformat(expected)
            self.assertLess(min(abs((v-target).total_seconds()) for v in events),120)

    def test_winter_dusk_is_not_fixed_to_summer_clock(self):
        winter=datetime(2026,1,15,23,30,tzinfo=timezone.utc)
        self.assertEqual(astronomy.solar_factor(winter,-97.5992,35.5852),15)

    def test_lunar_window_near_computed_transit(self):
        t=datetime(2026,10,6,15,13,tzinfo=timezone.utc)
        points,window=astronomy.solunar_factor(t,-97.5992,35.5852)
        self.assertEqual(window,'MAJOR')
        self.assertGreater(points,15)
