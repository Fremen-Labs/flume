import logging
import json
import sys
import os
from datetime import datetime, timezone
# AP-6: RotatingFileHandler removed — all log output goes to stdout/stderr only (12-factor).
# K8s log aggregators (Loki, Datadog, CloudWatch) collect from container stdout/stderr.

SENSITIVE_FRAGMENTS = ["key", "token", "secret", "password", "pat", "credential", "auth"]
MASKED_VALUE = "***REDACTED***"

def scrub_data(data):
    """Recursively scrub sensitive keys from a dictionary or list."""
    if isinstance(data, dict):
        return {
            k: (MASKED_VALUE if any(f in k.lower() for f in SENSITIVE_FRAGMENTS) else scrub_data(v))
            for k, v in data.items()
        }
    elif isinstance(data, list):
        return [scrub_data(item) for item in data]
    return data

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
            
        # Natively map deeply structured fields into the JSON Payload if available
        structured = getattr(record, 'structured_data', {})
        if structured:
            log_record.update(scrub_data(structured))
            
        # Trace ID propagation
        if hasattr(record, 'trace_id'):
            log_record["trace_id"] = record.trace_id
            
        return json.dumps(log_record)

class ConsoleFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',
        'INFO': '\033[92m',
        'WARNING': '\033[93m',
        'ERROR': '\033[91m',
        'CRITICAL': '\033[95m',
        'RESET': '\033[0m'
    }

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
        lvl = record.levelname[:3] # Short level: INF, DBG, ERR
        
        msg = record.getMessage()
        structured = getattr(record, 'structured_data', {})
        if structured:
            scrubbed = scrub_data(structured)
            msg += f" {json.dumps(scrubbed)}"
            
        return f"{color}[{lvl}] {ts} {record.name} - {msg}{self.COLORS['RESET']}"

def set_global_log_level(level_name: str):
    """Update all active Flume loggers to the new level."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    # Update the root flume logger and its children
    logging.getLogger("flume").setLevel(level)
    # Also update any other loggers that might be active
    for name in logging.root.manager.loggerDict:
        if name.startswith("flume") or "worker" in name:
            logging.getLogger(name).setLevel(level)

def get_logger(name: str, file_path: str = None) -> logging.Logger:
    """Return a structured logger that writes to stdout only."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    # Check for initial level from env
    env_level = os.environ.get('FLUME_LOG_LEVEL', 'INFO').upper()
    logger.setLevel(getattr(logging, env_level, logging.INFO))

    # Check if we are in ELK/JSON output mode for the terminal
    json_mode = os.environ.get('FLUME_JSON_LOGS', 'false').lower() == 'true'

    # Stdout stream handler
    stream_handler = logging.StreamHandler(sys.stdout)
    if json_mode:
        stream_handler.setFormatter(JSONFormatter())
    else:
        stream_handler.setFormatter(ConsoleFormatter())
    logger.addHandler(stream_handler)

    return logger
