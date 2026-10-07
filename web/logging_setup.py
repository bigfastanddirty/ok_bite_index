"""JSON application logs; keep stdout and bounded persistent files in sync."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys

cycle_context = ContextVar('cycle_id', default=None)
_FIELDS = ('event', 'lake', 'source', 'metric', 'job', 'cycle_id', 'duration_ms',
           'lakes_expected', 'lakes_committed', 'fresh_metrics', 'stale_metrics',
           'missing_metrics', 'carried_metrics', 'degraded_lakes', 'cache_age_seconds',
           'attempt', 'max_attempts', 'status_code', 'method', 'path', 'component', 'index',
           'updated', 'unchanged', 'no_url', 'no_species', 'errors', 'dry_run', 'active_projects')


def redact(text):
    text = str(text)
    for name, value in os.environ.items():
        if value and any(part in name.upper() for part in ('PASSWORD', 'TOKEN', 'API_KEY', 'SECRET')):
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return re.sub(r'(?i)((?:password|api[_-]?key|token|secret)\s*[=:]\s*)[^\s&\"\'<>]+',
                  r'\1[REDACTED]', text)


class JsonFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        data = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
            'level': record.levelname,
            'service': self.service,
            'event': getattr(record, 'event', 'runtime'),
            'message': record.getMessage(),
        }
        if cycle_context.get():
            data['cycle_id'] = cycle_context.get()
        for field in _FIELDS:
            if hasattr(record, field):
                data[field] = getattr(record, field)
        if record.exc_info:
            data['exception_type'] = record.exc_info[0].__name__
            data['exception'] = self.formatException(record.exc_info)
        # Redact each value before serialization, including traceback and context.
        def clean(value):
            if isinstance(value, str):
                return redact(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        return json.dumps(clean(data), default=str, ensure_ascii=False)


def setup_logging(service, filename='application.log', max_bytes=10 * 1024 * 1024, backup_count=5):
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
    formatter = JsonFormatter(service)
    directory = Path(os.getenv('LOG_DIR', '/app/logs'))
    directory.mkdir(parents=True, exist_ok=True)
    persistent = RotatingFileHandler(directory / filename, maxBytes=max_bytes,
                                    backupCount=backup_count, encoding='utf-8')
    stream = logging.StreamHandler(sys.stdout)
    for handler in (stream, persistent):
        handler.setFormatter(formatter)
        root.addHandler(handler)
    for name in ('uvicorn', 'uvicorn.error'):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger('uvicorn.access').disabled = True
    def report_uncaught(exception_type, exception, traceback):
        if issubclass(exception_type, KeyboardInterrupt):
            return sys.__excepthook__(exception_type, exception, traceback)
        logging.getLogger(service).critical('Application process terminated unexpectedly',
            extra={'event': 'process_failed'}, exc_info=(exception_type, exception, traceback))
    sys.excepthook = report_uncaught
    return logging.getLogger(service)


def log_message(*parts, sep=' ', end='\n', flush=False, file=None,
                level='INFO', event='script_message', exc_info=False, **fields):
    """Keep maintenance-script messages, adding severity and exception context."""
    message = sep.join(str(part) for part in parts).strip()
    if not message or set(message) == {'='}:
        return
    context = {'event': event, **fields}
    prefix = re.match(r'\[([^]]+)\]', message)
    if prefix:
        if re.fullmatch(r'[A-Z0-9]{4}', prefix[1]):
            context['lake'] = prefix[1]
        else:
            context['component'] = prefix[1]
    logging.getLogger('application').log(getattr(logging, level), message,
                                        extra=context, exc_info=exc_info)
