import logging
import json
import sys
import os
from datetime import datetime

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
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
        ts = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        msg = super().format(record)
        return f"{color}[{ts}] {record.levelname} | {record.name} - {msg}{self.COLORS['RESET']}"

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
        
    logger.setLevel(logging.INFO)
    
    # Check if we are in ELK/JSON output mode
    json_mode = os.environ.get('FLUME_JSON_LOGS', 'false').lower() == 'true'
    
    handler = logging.StreamHandler(sys.stdout)
    if json_mode:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())
        
    logger.addHandler(handler)
    return logger
