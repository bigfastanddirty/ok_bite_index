"""Exercise actual error/commit paths with no production database or network."""
import ast
from contextvars import ContextVar
from datetime import datetime, timezone
import io
import json
import logging
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

def load_function(path, name, namespace):
    tree = ast.parse(path.read_text())
    function = next(node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[name]

class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self):
        from test_logging import ApplicationLoggingTests
        module = ApplicationLoggingTests().load_module()
        self.stream = io.StringIO()
        self.logger = logging.getLogger('runtime-test')
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(module.JsonFormatter('test'))
        self.logger.addHandler(handler)

    def events(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]

    def test_cycle_failure_reports_committed_count_without_false_success(self):
        cursor = Mock()
        cursor.fetchall.side_effect = [[{'lake_code': 'ARCA', 'latitude': 35.6, 'longitude': -97.4}], [], [], []]
        conn = Mock()
        conn.cursor.return_value = cursor
        conn.commit.side_effect = RuntimeError('commit failed')
        ns = dict(logger=self.logger, cycle_context=ContextVar('test_cycle', default=None),
                  uuid4=uuid4, time=time, datetime=datetime, timezone=timezone,
                  get_db_connection=lambda: conn, ensure_quality_schema=lambda c: None,
                  RealDictCursor=object, _observations={}, fetch_weather_batch=lambda lakes: {},
                  fetch_cwms_levels=lambda: {}, fetch_cwms_elevation=lambda code: None,
                  fetch_cwms_flow=lambda *a: None, is_fresh=lambda *a: False,
                  utc=lambda value: None, json=json)
        run = load_function(ROOT / 'ingestor/ingest.py', 'run_sync', ns)
        with self.assertRaisesRegex(RuntimeError, 'commit failed'):
            run()
        events = self.events()
        self.assertEqual([event['event'] for event in events], ['cycle_started', 'cycle_failed'])
        self.assertEqual(events[-1]['lakes_expected'], 1)
        self.assertEqual(events[-1]['lakes_committed'], 0)
        self.assertEqual(events[-1]['exception_type'], 'RuntimeError')
        conn.close.assert_called_once()
        self.assertIsNone(ns['cycle_context'].get())

    def test_forecast_failure_records_bounded_cache_age(self):
        cache = {('ARCA',35.6,-97.4): {'stored_at': time.monotonic()-1200, 'data': {'hourly': {}}}}
        class ProviderUnavailable:
            Request = urllib.request.Request
            @staticmethod
            def urlopen(*args, **kwargs):
                raise TimeoutError('provider timed out')
        from types import SimpleNamespace
        ns = dict(logger=self.logger, time=time, _forecast_cache=cache,
                  _forecast_cache_lock=threading.Lock(), FORECAST_CACHE_TTL_SECONDS=900,
                  FORECAST_STALE_MAX_SECONDS=3600, urllib=SimpleNamespace(request=ProviderUnavailable), json=json)
        fetch = load_function(ROOT / 'web/app.py', 'fetch_open_meteo_forecast_cached', ns)
        data, state, age = fetch('ARCA',35.6,-97.4)
        self.assertEqual(state,'STALE')
        self.assertGreaterEqual(age,1200)
        event=self.events()[-1]
        self.assertEqual(event['event'],'forecast_stale_cache')
        self.assertEqual(event['lake'],'ARCA')
        self.assertEqual(event['source'],'open-meteo')
        self.assertEqual(event['exception_type'],'TimeoutError')

if __name__ == '__main__':
    unittest.main()
