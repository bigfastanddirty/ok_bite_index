import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class ApplicationLoggingTests(unittest.TestCase):
    def load_module(self):
        path = ROOT / 'web/logging_setup.py'
        self.assertTrue(path.exists(), 'Structured application logger is missing')
        spec = importlib.util.spec_from_file_location('logging_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_json_context_and_exception_redaction(self):
        module = self.load_module()
        with patch.dict(os.environ, {'POSTGRES_PASSWORD': 'secret-for-test'}):
            record = logging.LogRecord('app', logging.ERROR, __file__, 1,
                                       'password=another-secret secret-for-test', (), None)
            record.event = 'provider_failed'
            record.lake = 'ARCA'
            try:
                raise ValueError('https://provider/?api_key=url-secret secret-for-test')
            except ValueError:
                import sys
                record.exc_info = sys.exc_info()
            output = module.JsonFormatter('web').format(record)
        data = json.loads(output)
        self.assertEqual(data['service'], 'web')
        self.assertEqual(data['level'], 'ERROR')
        self.assertEqual(data['lake'], 'ARCA')
        self.assertEqual(data['event'], 'provider_failed')
        self.assertTrue(data['timestamp'].endswith('Z'))
        self.assertIn('ValueError', data['exception'])
        for secret in ('secret-for-test', 'another-secret', 'url-secret'):
            self.assertNotIn(secret, output)

    def test_file_rotation_and_stdout_match_without_duplicate_handlers(self):
        module = self.load_module()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOG_DIR': directory}), patch('sys.stdout', new_callable=io.StringIO) as stream:
            logger = module.setup_logging('test', max_bytes=600, backup_count=2)
            module.setup_logging('test', max_bytes=600, backup_count=2)
            for index in range(20):
                logger.info('committed lake observation', extra={'event': 'lake_committed', 'lake': 'ARCA', 'index': index})
            paths = list(Path(directory).glob('application.log*'))
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(path.stat().st_size <= 600 for path in paths))
            lines = stream.getvalue().splitlines()
            self.assertEqual(len(lines), 20)
            latest = (Path(directory) / 'application.log').read_text().splitlines()[-1]
            self.assertEqual(latest, lines[-1])
            for handler in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(handler)
                handler.close()

    def test_uncaught_script_failure_is_persisted(self):
        import sys
        module = self.load_module()
        previous_hook = sys.excepthook
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'LOG_DIR': directory}), patch('sys.stdout', new_callable=io.StringIO):
                module.setup_logging('ingestor')
                try:
                    raise RuntimeError('database connection unavailable')
                except RuntimeError:
                    sys.excepthook(*sys.exc_info())
                records = [json.loads(line) for line in (Path(directory) / 'application.log').read_text().splitlines()]
                self.assertTrue(records, 'Uncaught script failures are not persisted')
                self.assertEqual(records[-1]['event'], 'process_failed')
                self.assertEqual(records[-1]['level'], 'CRITICAL')
                self.assertEqual(records[-1]['exception_type'], 'RuntimeError')
                for handler in list(logging.getLogger().handlers):
                    logging.getLogger().removeHandler(handler)
                    handler.close()
        finally:
            sys.excepthook = previous_hook

    def test_web_and_ingestor_formatter_have_identical_behavior(self):
        module = self.load_module()
        self.assertEqual((ROOT / 'web/logging_setup.py').read_bytes(),
                         (ROOT / 'ingestor/logging_setup.py').read_bytes())

if __name__ == '__main__':
    unittest.main()
