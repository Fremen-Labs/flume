import logging
import json
import sys
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
        
    logger.setLevel(logging.INFO)
    
    # Check if we are in ELK/JSON output mode for the terminal
    json_mode = os.environ.get('FLUME_JSON_LOGS', 'false').lower() == 'true'
    
    # 1. Standard Output Stream Handler
    stream_handler = logging.StreamHandler(sys.stdout)
    if json_mode:
        stream_handler.setFormatter(JSONFormatter())
    else:
        stream_handler.setFormatter(ConsoleFormatter())
    logger.addHandler(stream_handler)
    
    # 2. Disk Persistence JSON Rotating File Handler
    if file_path:
        try:
            log_path = Path(file_path).resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=5)
            # Physical disk logs are ALWAYS strictly mandated as JSON to support Filebeat/ELK parsing securely.
            file_handler.setFormatter(JSONFormatter())
            logger.addHandler(file_handler)
        except PermissionError:
            stream_handler.setLevel(logging.WARNING)
            logger.warning(f"Insufficient permissions to bootstrap File Handler at {file_path}. Operating with Terminal streams only.")
            stream_handler.setLevel(logging.INFO)
            
    return logger
