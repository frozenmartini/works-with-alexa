"""Development JSONL logs, activated only by the private deployment marker.

Disk I/O belongs to a queue listener thread. Structured API events deliberately
exclude bodies, headers, credentials, account identifiers and raw exception text.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
import queue
import re
import time
from typing import Any
from uuid import uuid4

_LOGGER = logging.getLogger(__name__)
_STATE_KEY = 'works_with_alexa_evidence_logging'
_REQUEST_ID: ContextVar[str | None] = ContextVar('works_with_alexa_request_id', default=None)
_SECRET = re.compile(
    r'(?i)(["\']?(?:refresh[_-]?token|access[_-]?token|source_token|adp_token|'
    r'adp_private_key|authorization|cookie|csrf|password|otp_secret|'
    r'authorization_code|openid\.oa2\.authorization_code)["\']?\s*[:=]\s*)[^\r\n]+')
_URL = re.compile(r'https?://[^\s\"\'<>]+')
_EMAIL = re.compile(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}')
_IDENTIFIER = re.compile(r'amzn1\.[\w.:-]+')
_SENSITIVE_KEYS = {
    'refreshtoken', 'accesstoken', 'sourcetoken', 'adptoken', 'adpprivatekey',
    'authorization', 'cookie', 'cookies', 'csrf', 'password', 'otpsecret',
    'authorizationcode', 'credentials', 'macdms', 'headers', 'payload', 'body',
    'params', 'redirecturl', 'deviceserial', 'endpointid', 'automationid',
}


def safe_text(value: str) -> str:
    """Redact common credentials and identity in existing free-text log messages."""
    value = _URL.sub(lambda match: match.group().split('?')[0].split('#')[0], value)
    value = _SECRET.sub(lambda match: match.group(1) + '[REDACTED]', value)
    value = re.sub(r'(?i)Bearer\s+\S+', 'Bearer [REDACTED]', value)
    value = _EMAIL.sub('[EMAIL]', value)
    return _IDENTIFIER.sub('[AMAZON_ID]', value)


def safe_fields(value: Any, key: str = '') -> Any:
    """Defense in depth if a future call site supplies a sensitive field."""
    if re.sub(r'[^a-z]', '', key.lower()) in _SENSITIVE_KEYS:
        return '[REDACTED]'
    if isinstance(value, dict):
        return {str(k): safe_fields(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_fields(item) for item in value]
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f'[{type(value).__name__}]'


def event(logger: logging.Logger, name: str, *, level: int = logging.DEBUG, **fields: Any) -> None:
    """Log safe structured fields, supplied explicitly at each call site."""
    if request_id := _REQUEST_ID.get():
        fields.setdefault('request_id', request_id)
    logger.log(level, name, extra={'dev_event': name, 'dev_fields': fields})


@contextmanager
def operation(logger: logging.Logger, name: str, **fields: Any):
    """Correlate a command with its transport, timing and acknowledgement."""
    token = _REQUEST_ID.set(_REQUEST_ID.get() or uuid4().hex)
    started = time.monotonic()
    event(logger, f'{name}.started', **fields)
    try:
        yield
    except Exception as err:
        event(logger, f'{name}.failed', level=logging.ERROR,
              error_type=type(err).__name__, elapsed_ms=round((time.monotonic() - started) * 1000, 3),
              **fields)
        raise
    else:
        event(logger, f'{name}.completed', elapsed_ms=round((time.monotonic() - started) * 1000, 3),
              **fields)
    finally:
        _REQUEST_ID.reset(token)


class JsonLineFormatter(logging.Formatter):
    def __init__(self, source: str = 'home_assistant') -> None:
        super().__init__()
        self.source = source

    def format(self, record: logging.LogRecord) -> str:
        data = {
            'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec='milliseconds'),
            'level': record.levelname,
            'logger': record.name,
            'event': getattr(record, 'dev_event', 'log'),
            'message': safe_text(record.getMessage()),
            'source': self.source,
        }
        data.update(safe_fields(getattr(record, 'dev_fields', {})))
        if record.exc_info:
            data['error_type'] = record.exc_info[0].__name__
        # Never format exception tracebacks; upstream messages can contain URLs.
        return json.dumps(data, ensure_ascii=False)


class SafeQueueHandler(QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # QueueHandler's default formats exception text into the message. Avoid
        # that, and sanitize before any data reaches the listener queue.
        prepared = copy.copy(record)
        prepared.msg = safe_text(record.getMessage())
        prepared.args = None
        prepared.exc_text = None
        if record.exc_info:
            prepared.dev_fields = {**getattr(record, 'dev_fields', {}),
                                   'error_type': record.exc_info[0].__name__}
        prepared.exc_info = None
        prepared.stack_info = None
        prepared.dev_fields = safe_fields(getattr(prepared, 'dev_fields', {}))
        return prepared

    def handleError(self, record: logging.LogRecord) -> None:
        # A full queue must not print an unsanitized record or stall HA.
        pass


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


class DrainingQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        # stop() runs in HA's executor, so it may wait for a full queue to drain.
        self.queue.put(self._sentinel)


class EvidenceLogging:
    """One integration-wide handler, retained across config entry reloads."""
    def __init__(self, directory: Path, logger_name: str, source: str = 'home_assistant',
                 *, filename: str = 'works_with_alexa.jsonl', archive_all: bool = False) -> None:
        self.directory = directory
        self.logger = logging.getLogger(logger_name)
        self.old_level = self.logger.level
        self.source = source
        self.filename = filename
        self.archive_all = archive_all
        self.file_handler: RotatingFileHandler | None = None
        self.handler: SafeQueueHandler | None = None
        self.listener: QueueListener | None = None

    def start(self) -> None:
        if self.listener is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / self.filename
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        handler = PrivateRotatingFileHandler(path, maxBytes=0 if self.archive_all else 5 * 1024 * 1024,
                                            backupCount=5, encoding='utf-8')
        handler.setFormatter(JsonLineFormatter(self.source))
        log_queue: queue.Queue = queue.Queue(maxsize=0 if self.archive_all else 10000)
        self.handler = SafeQueueHandler(log_queue)
        self.file_handler = handler
        self.listener = DrainingQueueListener(log_queue, handler)
        self.listener.start()
        self.logger.addHandler(self.handler)
        self.logger.setLevel(logging.DEBUG)

    def stop(self) -> None:
        if self.listener is None:
            return
        self.logger.removeHandler(self.handler)
        self.listener.stop()
        self.file_handler.close()
        self.logger.setLevel(self.old_level)
        self.listener = None


def development_directory(component: Path, config: Path) -> Path | None:
    """Resolve the private marker's relative path in the Core container."""
    marker = component / '.dev-install'
    if not marker.is_file():
        return None
    for line in marker.read_text().splitlines():
        key, _, value = line.partition(':')
        if key == 'evidence_log_dir':
            relative = Path(value.strip())
            if relative.is_absolute() or '..' in relative.parts or not value.strip():
                raise ValueError('Development log path must be relative to the HA config directory')
            return config / relative
    return None


async def async_enable_evidence_logging(hass: Any) -> None:
    """Initialize once, off the event loop, without making logging a setup dependency."""
    if _STATE_KEY in hass.data:
        return
    # No awaits before publishing the sentinel: simultaneous entries cannot
    # install duplicate handlers. Logging remains active until Core stops.
    hass.data[_STATE_KEY] = None
    try:
        directory = await hass.async_add_executor_job(
            development_directory, Path(__file__).parent, Path(hass.config.path()))
        if directory is None:
            return
        evidence = EvidenceLogging(directory, __package__)
        await hass.async_add_executor_job(evidence.start)
        auth_evidence = EvidenceLogging(directory, __package__ + '.auth_watch',
                                       filename='auth.jsonl', archive_all=True)
        try:
            await hass.async_add_executor_job(auth_evidence.start)
        except OSError:
            await hass.async_add_executor_job(evidence.stop)
            raise
    except (OSError, ValueError):
        _LOGGER.warning('Development evidence logging could not be initialized')
        hass.data.pop(_STATE_KEY, None)
        return
    hass.data[_STATE_KEY] = (evidence, auth_evidence)
    event(_LOGGER, 'logging.started', level=logging.INFO, directory=str(directory))

    async def stop_logging(_event: Any) -> None:
        event(_LOGGER, 'logging.stopped', level=logging.INFO)
        await hass.async_add_executor_job(evidence.stop)
        await hass.async_add_executor_job(auth_evidence.stop)
        hass.data.pop(_STATE_KEY, None)

    hass.bus.async_listen_once('homeassistant_stop', stop_logging)
