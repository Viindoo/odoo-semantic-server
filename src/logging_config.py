# src/logging_config.py
"""Centralised logging configuration for odoo-semantic-mcp.

Controlled by LOG_FORMAT env var:
  text  (default): standard %(asctime)s %(levelname)s %(message)s
  json:             machine-readable JSON lines — one JSON object per log record.

Controlled by LOG_FILE env var (optional):
  If set, a rotating file handler is added (50 MB max, 5 backups, UTF-8).
  Works alongside the stream handler — both receive all log records.
"""
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        })


def configure_logging(level: int = logging.WARNING) -> None:
    """Configure root logger.

    Args:
        level: Logging level (e.g. logging.INFO, logging.WARNING).

    Behaviour:
        LOG_FORMAT=json  → JSON handler replaces all root handlers.
        LOG_FORMAT=text  → standard basicConfig format.
        (unset)          → falls back to 'text' behaviour.
    """
    fmt = os.getenv("LOG_FORMAT", "text").lower()
    if fmt == "json":
        formatter: logging.Formatter = _JsonFormatter()
        handler: logging.Handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logging.root.handlers = [handler]
        logging.root.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(message)s",
        )
        if logging.root.handlers:
            formatter = logging.root.handlers[0].formatter
        else:
            formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Optional rotating file handler (activated by LOG_FILE env var)
    log_file = os.environ.get("LOG_FILE")
    if log_file:
        try:
            fh = RotatingFileHandler(
                log_file,
                maxBytes=50 * 1024 * 1024,  # 50 MB per file
                backupCount=5,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            logging.root.addHandler(fh)
        except Exception as exc:
            # Don't fail startup if log file is not writable
            logging.root.warning("Could not open LOG_FILE=%r: %s", log_file, exc)
