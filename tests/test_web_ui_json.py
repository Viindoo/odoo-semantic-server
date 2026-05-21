# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/web_ui/_json.py::_json_safe.

Covers all supported conversions: datetime, date, Decimal, UUID, bytes, and
nested combinations. No fixtures, no DB — pure unit tests.
"""
import datetime as dt
import json
import uuid
from decimal import Decimal

from src.web_ui._json import _json_safe


def test_datetime_to_iso():
    value = dt.datetime(2026, 5, 16, 12, 34, 56)
    assert _json_safe(value) == "2026-05-16T12:34:56"


def test_date_to_iso():
    value = dt.date(2026, 5, 16)
    assert _json_safe(value) == "2026-05-16"


def test_decimal_to_float():
    assert _json_safe(Decimal("1.5")) == 1.5


def test_uuid_to_string():
    uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert _json_safe(uid) == "12345678-1234-5678-1234-567812345678"


def test_bytes_to_hex():
    assert _json_safe(b"\x00\x01\xff") == "0001ff"


def test_bytes_empty():
    assert _json_safe(b"") == ""


def test_dict_recursive():
    uid = uuid.uuid4()
    payload = {
        "id": uid,
        "ts": dt.datetime(2026, 1, 1),
        "amt": Decimal("9.99"),
        "checksum": b"\xde\xad\xbe\xef",
    }
    out = _json_safe(payload)
    assert out == {
        "id": str(uid),
        "ts": "2026-01-01T00:00:00",
        "amt": 9.99,
        "checksum": "deadbeef",
    }
    # Must be json.dumps-able after conversion.
    json.dumps(out)


def test_list_with_uuids_and_bytes():
    uid1, uid2 = uuid.uuid4(), uuid.uuid4()
    payload = [uid1, b"\x01\x02", {"nested": uid2}]
    out = _json_safe(payload)
    assert out == [str(uid1), "0102", {"nested": str(uid2)}]
    json.dumps(out)


def test_tuple_preserved_with_conversion():
    out = _json_safe((b"\xab", Decimal("0.5")))
    assert out == ("ab", 0.5)
    assert isinstance(out, tuple)


def test_passthrough_natively_serialisable():
    for v in [None, True, False, 0, 1, -1, 1.5, "hello", "", [], {}]:
        assert _json_safe(v) == v


def test_nested_dict_with_bytes_inside_list():
    payload = {"items": [{"hash": b"\xff\x00"}, {"hash": b"\x12\x34"}]}
    out = _json_safe(payload)
    assert out == {"items": [{"hash": "ff00"}, {"hash": "1234"}]}
    json.dumps(out)


def test_uuid_as_dict_key_passes_through():
    # Dict keys are not converted (json.dumps would still fail, but caller
    # should ensure keys are strings; helper only converts values).
    key = uuid.uuid4()
    payload = {key: "value"}
    out = _json_safe(payload)
    assert out == {key: "value"}
