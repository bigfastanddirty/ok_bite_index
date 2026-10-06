import ast
import math
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def functions(path):
    tree = ast.parse(path.read_text())
    selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and not n.decorator_list], type_ignores=[])
    ns = dict(math=math, datetime=datetime, timezone=timezone, timedelta=timedelta,
              ZoneInfo=ZoneInfo, Optional=__import__('typing').Optional)
    import sys
    sys.path.insert(0, str(ROOT / 'ingestor'))
    from quality import utc
    ns['utc'] = utc
    exec(compile(selected, str(path), 'exec'), ns)
    return ns


class AccuracyRegression(unittest.TestCase):
    def test_best_window_excludes_past_hours(self):
        ns = functions(ROOT / 'web/app.py')
        cards = [{"time": "2000-01-01T06:00:00+00:00", "bite_score": 100},
                 {"time": "2099-01-01T06:00:00+00:00", "bite_score": 50}]
        best = ns['calculate_best_window'](cards, window_hours=1)
        self.assertEqual(best['start'], cards[1]['time'])

    def test_unknown_weather_has_no_rating(self):
        ns = functions(ROOT / 'web/app.py')
        result = ns['calculate_bite_score'](None, None, None, datetime.now(timezone.utc), -97.6, 35.6)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 'UNAVAILABLE')

    def test_missing_forecast_hours_do_not_extend_best_window_horizon(self):
        ns = functions(ROOT / 'web/app.py')
        start=datetime.now(timezone.utc).replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)
        cards=[{'time':(start+timedelta(hours=i)).isoformat(),'bite_score':None if i<24 else 99} for i in range(48)]
        self.assertIsNone(ns['calculate_best_window'](cards))

    def test_regulations_layout_failure_is_unknown(self):
        ns = functions(ROOT / 'ingestor/odwc_regs.py')
        ns.update(html_lib=__import__('html'), re=__import__('re'))
        self.assertIsNone(ns['extract_regs_from_html']('<html>temporarily unavailable</html>'))


if __name__ == '__main__':
    unittest.main()
