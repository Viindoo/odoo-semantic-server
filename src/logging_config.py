# src/logging_config.py
"""Centralised logging configuration for odoo-semantic-mcp.

Controlled by LOG_FORMAT env var:
  text  (default): standard %(asctime)s %(levelname)s %(message)s
  json:             machine-readable JSON lines — one JSON object per log record.
"""
import json
import logging
import os
import time


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
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.root.handlers = [handler]
        logging.root.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(message)s",
        )
