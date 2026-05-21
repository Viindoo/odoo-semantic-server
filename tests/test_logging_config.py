# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_logging_config.py
"""Unit tests for src.logging_config."""
import json
import logging

import pytest

from src.logging_config import _JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def _reset_root_handlers():
    """Isolate root logger handlers between tests to prevent cross-test contamination."""
    original_handlers = logging.root.handlers[:]
    original_level = logging.root.level
    yield
    logging.root.handlers = original_handlers
    logging.root.setLevel(original_level)


def test_json_format_produces_parseable_json(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    configure_logging(logging.DEBUG)
    # Verify JSON formatter is active on root logger
    handlers = [h for h in logging.root.handlers if isinstance(h.formatter, _JsonFormatter)]
    assert handlers, "JSON formatter should be active when LOG_FORMAT=json"


def test_json_formatter_output_is_valid_json():
    formatter = _JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname="", lineno=0,
        msg="hello world", args=(), exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "hello world"
    assert parsed["name"] == "test"
    assert "time" in parsed


def test_text_format_is_default(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    # Clear any existing JSON handlers before calling configure_logging
    logging.root.handlers = []
    configure_logging(logging.WARNING)
    json_handlers = [h for h in logging.root.handlers if isinstance(h.formatter, _JsonFormatter)]
    assert not json_handlers, "JSON formatter should not be active for text format"
