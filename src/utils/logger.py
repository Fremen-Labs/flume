import logging
import json
import sys
import os
from datetime import datetime, timezone
# AP-6: RotatingFileHandler removed — all log output goes to stdout/stderr only (12-factor).
# K8s log aggregators (Loki, Datadog, CloudWatch) collect from container stdout/stderr.

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
        if hasattr(record, 'structured_data'):
            log_record.update(record.structured_data)
            
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
        ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        msg = super().format(record)
        return f"{color}[{ts}] {record.levelname} | {record.name} - {msg}{self.COLORS['RESET']}"

def get_logger(name: str, file_path: str = None) -> logging.Logger:
    """Return a structured logger that writes to stdout only.

    The *file_path* parameter is accepted but ignored — AP-6 (K8s readiness):
    rotating file handlers have been removed in favour of stdout/stderr so that
    the cluster log aggregator (Loki / Datadog / CloudWatch) can collect logs
    from container output without requiring a shared volume mount.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    # Check if we are in ELK/JSON output mode for the terminal
    json_mode = os.environ.get('FLUME_JSON_LOGS', 'false').lower() == 'true'

    # Stdout stream handler — the only sink in K8s-compatible deployments
    stream_handler = logging.StreamHandler(sys.stdout)
    if json_mode:
        stream_handler.setFormatter(JSONFormatter())
    else:
        stream_handler.setFormatter(ConsoleFormatter())
    logger.addHandler(stream_handler)

    return logger
