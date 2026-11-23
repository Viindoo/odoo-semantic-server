# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_rules_source_parity.py
"""Lint rule catalogue (Odoo 8.0-20.0) - every premise is checked against source.

`src/indexer/spec_data/lint_rules_<N>.0.json` is curated editorial data: a rule
tells the author "this is wrong or discouraged at version N, use that instead".
ADR-0055: curated data needs an oracle wired to a test on real data, or a
written reason why none is possible. A rule that flags correct code, or names
an API that does not exist at N, makes `lint_check(odoo_version=N)` lie.

Three layers, all parametrized over the surveyed majors (`SURVEYED_MAJORS`):

1. Coverage (CI-safe, never skips): every curated `(version, rule_id)` pair is
   either guarded by a source oracle below, guarded by a named test of the
   v20-only module `test_lint_rules_v20_source_parity.py` (reused, not
   duplicated), or carries a written editorial reason in `_EDITORIAL`. A
   stale entry (unknown rule, version where the rule is not curated) fails.
2. Source oracles (`odoo_source`): per `(version, rule_id)` the premise the
   rule states is diffed against the odoo/odoo checkout of that version - the
   thing it flags exists and is discouraged at N, the fix it names exists at
   N, the boundary it names is the real one.
3. Dropped premises (CI-safe absence + `odoo_source` evidence): rules that were
   removed because their premise is false in source stay removed, and the
   source facts that make them false are re-checked at every version they used
   to be curated for.

`parse_lint_rules_for_version` must return one record per rule id at every
version, with and without the live checker layer.

Provenance: the oracles were written against the odoo/odoo `N.0` branch heads
8.0 9e8f70e4849b, 9.0 7132a15c38a3, 10.0 28c3f51c4878, 11.0 d07ffce41df4,
12.0 ba1ba88874e2, 13.0 4cc086d330b7, 14.0 cc0060e88960, 15.0 3a28e5b0adbb,
16.0 e75d5826d089, 17.0 d98686036042, 18.0 465ae31c1aea, 19.0 3535c63cdfa1,
20.0 0d7f0ac97c4d.
"""

from __future__ import annotations

import functools
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.constants import SURVEYED_MAJORS
from src.indexer.parser_lint_rules import parse_lint_rules_for_version
from tests import test_lint_rules_v20_source_parity as v20_guards
from tests._odoo_checkouts import checkout_root

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
_MAJORS = list(SURVEYED_MAJORS)


def _load(major: int) -> dict:
    return json.loads((_SPEC_DATA_DIR / f"lint_rules_{major}.0.json").read_text(encoding="utf-8"))


def _rules_of(major: int) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for rule in _load(major)["rules"]:
        assert rule["rule_id"] not in by_id, f"duplicate rule_id {rule['rule_id']} at {major}.0"
        by_id[rule["rule_id"]] = rule
    return by_id


_CURATED: dict[int, dict[str, dict]] = {m: _rules_of(m) for m in _MAJORS}


def _versions_of(rule_id: str) -> set[int]:
    return {m for m in _MAJORS if rule_id in _CURATED[m]}


# ---------------------------------------------------------------------------
# Source access
# ---------------------------------------------------------------------------


class Src:
    """Read-only view of the odoo/odoo checkout of one major."""

    def __init__(self, major: int, root: Path):
        self.major = major
        self.root = root
        self.core = "openerp" if major < 10 else "odoo"

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return (self.root / rel).exists()

    def text(self, rel: str) -> str:
        return _read_text(self.root / rel)

    def glob(self, pattern: str) -> list[Path]:
        return sorted(self.root.glob(pattern))

    def models(self) -> str:
        return self.text("odoo/orm/models.py" if self.major >= 19 else f"{self.core}/models.py")

    def fields(self) -> str:
        if self.major >= 19:
            return "\n".join(_read_text(p) for p in self.glob("odoo/orm/fields*.py"))
        return self.text(f"{self.core}/fields.py")

    def api(self) -> str:
        """Text of the module(s) defining the `odoo.api` decorators and `Environment`."""
        if self.major >= 19:
            rels = ("odoo/api/__init__.py", "odoo/orm/decorators.py", "odoo/orm/environments.py")
            return "\n".join(self.text(r) for r in rels)
        return self.text(f"{self.core}/api.py")

    def module_py(self) -> str:
        return self.text(f"{self.core}/modules/module.py")

    def defaults_block(self) -> str:
        """Text of the manifest-defaults dict literal in modules/module.py."""
        lines = self.module_py().splitlines()
        anchor = next(i for i, ln in enumerate(lines) if "'installable'" in ln)
        start = anchor
        while not re.search(r"=\s*\{\s*$", lines[start]):
            start -= 1
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while not (
            lines[end].startswith(" " * indent + "}")
            and len(lines[end]) - len(lines[end].lstrip()) == indent
        ):
            end += 1
        return "\n".join(lines[start : end + 1])

    def default_keys(self) -> set[str]:
        block = self.defaults_block()
        return set(re.findall(r"^\s*'(\w+)'\s*:", block, re.MULTILINE))


@functools.cache
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _src(major: int) -> Src:
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS or ODOO{major}_SRC)")
    return Src(major, root)


@dataclass
class Ctx:
    major: int
    rule: dict
    src: Src

    @property
    def message(self) -> str:
        return self.rule.get("message") or ""

    @property
    def said(self) -> str:
        """Everything the rule tells the author: message and fix template."""
        return f"{self.message}\n{self.rule.get('fix_template') or ''}"

    def other(self, major: int) -> Src:
        return _src(major)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ORACLES: dict[str, list[tuple[frozenset[int], Callable[[Ctx], None]]]] = {}
_EDITORIAL: dict[str, list[tuple[frozenset[int] | None, str]]] = {}
# Rules whose premise is guarded by a named test of the v20-only module.
_V20_GUARDS: dict[str, str] = {}
# Rules removed because the premise is false: rule_id -> versions it must stay absent at.
_DROPPED: dict[str, frozenset[int]] = {}


def oracle(rule_id: str, versions):
    def deco(fn: Callable[[Ctx], None]):
        _ORACLES.setdefault(rule_id, []).append((frozenset(versions), fn))
        return fn

    return deco


def dropped_premise(rule_id: str, versions):
    """Oracle proving the premise of a removed rule is false; the rule must stay absent."""
    _DROPPED[rule_id] = frozenset(versions)

    def deco(fn: Callable[[Ctx], None]):
        _ORACLES.setdefault(f"dropped:{rule_id}", []).append((frozenset(versions), fn))
        return fn

    return deco


def editorial(rule_id: str, reason: str, versions=None):
    _EDITORIAL.setdefault(rule_id, []).append(
        (None if versions is None else frozenset(versions), reason)
    )


def _has(pattern: str, text: str, flags: int = re.MULTILINE) -> bool:
    return bool(re.search(pattern, text, flags))


R = range


# ---------------------------------------------------------------------------
# Manifest loader (W8101-W8106)
# ---------------------------------------------------------------------------


@oracle("W8101", R(8, 21))
def _w8101(c: Ctx) -> None:
    """`name` is the one manifest key the loader never defaults."""
    assert "name" not in c.src.default_keys()


@oracle("W8102", R(8, 21))
def _w8102(c: Ctx) -> None:
    """The loader defaults `version` to `1.0`, so the key is not required."""
    assert re.search(r"'version'\s*:\s*'1\.0'", c.src.defaults_block())
    assert "falls back to `1.0`" in c.message


def _mandatory_listed(src: Src, key: str) -> bool:
    return _has(
        rf"# Mandatory fields \(with no defaults\):(?:\n\s*# - \w+)*\n\s*# - {key}\b",
        src.module_py(),
    )


def _manifest_key_wording(c: Ctx, key: str) -> None:
    mandatory = _mandatory_listed(c.src, key)
    assert ("mandatory" in c.message) is mandatory, (
        f"{key}: source says mandatory={mandatory}, message says {c.message!r}"
    )
    assert ("recommended" in c.message) is (not mandatory), c.message
    if not mandatory:
        # A recommended key has a loader default (in the dict or assigned after it).
        text = c.src.module_py()
        assert re.search(rf"'{key}'\s*:\s*'[^']*'|\['{key}'\]\s*=\s*'", text), (
            f"{key} has no loader default"
        )


@oracle("W8103", R(8, 21))
def _w8103(c: Ctx) -> None:
    _manifest_key_wording(c, "author")


@oracle("W8104", R(8, 21))
def _w8104(c: Ctx) -> None:
    _manifest_key_wording(c, "license")


@oracle("W8105", R(8, 21))
def _w8105(c: Ctx) -> None:
    assert re.search(r"'installable'\s*:\s*True", c.src.defaults_block())


@oracle("W8106", [10])
def _w8106(c: Ctx) -> None:
    """v10 renames the manifest of every core module; the loader still reads both names."""
    v9, v10 = c.other(9), c.src
    assert (
        v9.text("openerp/modules/module.py").count("__openerp__.py")
        and "MANIFEST_NAMES" not in v9.module_py()
    )
    assert not v9.glob("addons/*/__manifest__.py")
    assert len(v10.glob("addons/*/__manifest__.py")) > 100
    assert len(v10.glob("addons/*/__openerp__.py")) <= 5
    assert re.search(r"MANIFEST_NAMES = \('__manifest__\.py', '__openerp__\.py'\)", v10.module_py())
    assert "only recognizes" not in c.said


# ---------------------------------------------------------------------------
# Old-API removal boundaries (W8107-W8113, W8187, W8188)
# ---------------------------------------------------------------------------


def _decorator_defined(src: Src, name: str) -> bool:
    return _has(rf"^def {name}\(", src.api())


def _removal_boundary(c: Ctx, symbol: str) -> None:
    """The decorator exists at v12 and is gone from v13 on; a stated removal boundary is v13."""
    assert _decorator_defined(c.other(12), symbol), f"{symbol} must exist at v12"
    assert not _decorator_defined(c.src, symbol), f"{symbol} must not exist at v{c.major}"
    stated = re.findall(r"removed in v(\d+)", c.message)
    assert set(stated) <= {"13"}, f"{symbol}: real removal boundary is v13, message states {stated}"
    if c.major != 13:
        assert not stated, c.message


@oracle("W8107", R(13, 21))
def _w8107(c: Ctx) -> None:
    _removal_boundary(c, "multi")


@oracle("W8108", R(13, 21))
def _w8108(c: Ctx) -> None:
    _removal_boundary(c, "one")


@oracle("W8113", [9])
def _w8113_api_one(c: Ctx) -> None:
    assert re.search(r"def one\(method\):.*?\.\. deprecated:: 9\.0", c.src.api(), re.DOTALL)


@oracle("W8113", [10, 11, 12])
def _w8113_osv(c: Ctx) -> None:
    """`osv.osv` is a compatibility alias of `models.Model`, commented as deprecated."""
    assert _has(
        r"# Deprecated, kept for backward compatibility\.\nosv = Model",
        c.src.text(f"{c.src.core}/osv/osv.py"),
    )


@oracle("W8110", [8, 9, 10, 11, 12, 13, 14])
def _w8110(c: Ctx) -> None:
    supported = _has(r"^\s*_columns = \{\}", c.src.models())
    assert supported is (c.major <= 9)
    assert ("not supported" in c.message or "required in" in c.message) is (not supported), (
        c.message
    )


@oracle("W8111", [8, 9, 10, 11, 12, 13, 14])
def _w8111(c: Ctx) -> None:
    models = c.src.models()
    unsupported = "Model attribute '_constraints' is no longer supported" in models
    assert unsupported is (c.major >= 13)
    if not unsupported:
        assert _has(r"^\s*_constraints = \[\]", models)
    assert ("not supported" in c.message) is unsupported, c.message


@oracle("W8112", [8, 9, 10, 11, 12])
def _w8112(c: Ctx) -> None:
    """Old `(cr, uid, ids)` signatures are guessed and still callable through v12."""
    api = c.src.api()
    assert _has(r"^def guess\(", api)
    assert _has(r"^def cr_uid_ids_context\(", api)
    assert not re.search(r"not supported|requires new API", c.message), c.message
    if c.major >= 10:
        assert "Deprecated method %s.%s in module %s" in api
        assert "deprecated in v" in c.message
    assert not _has(r"^def guess\(", c.other(13).api())


@oracle("W8187", [10])
def _w8187(c: Ctx) -> None:
    assert _has(r"(?<![A-Za-z0-9_])_defaults\b", c.other(9).models())
    assert not _has(r"(?<![A-Za-z0-9_])_defaults\b", c.src.models())
    assert "not read by the v10 ORM" in c.message


@oracle("W8188", [10])
def _w8188(c: Ctx) -> None:
    v9 = c.other(9)
    for name in ("function", "related", "property"):
        assert _has(rf"^class {name}\(", v9.text("openerp/osv/fields.py"))
    assert not c.src.exists("odoo/osv/fields.py")
    assert not _has(r"^class function\(", c.src.fields())


# ---------------------------------------------------------------------------
# Security / ORM conventions that name a source fact
# ---------------------------------------------------------------------------


@oracle("W8120", R(8, 20))
def _w8120(c: Ctx) -> None:
    assert c.src.exists(f"{c.src.core}/addons/base/security/ir.model.access.csv")


@oracle("W8141", R(8, 21))
def _w8141(c: Ctx) -> None:
    dotted = "openerp.tools" if c.major < 10 else "odoo.tools.safe_eval"
    assert dotted in c.said
    rel = f"{c.src.core}/tools/safe_eval"
    assert c.src.exists(rel + ".py") or c.src.exists(rel + "/__init__.py")


@oracle("W8142", R(10, 21))
def _w8142(c: Ctx) -> None:
    """`sudo(user)` is only a valid fix where `sudo` takes a user."""
    takes_user = _has(r"def sudo\(self, user=", c.src.models())
    assert takes_user is (c.major <= 12)
    if "sudo(user)" in c.said:
        assert takes_user


@oracle("W8151", R(10, 21))
def _w8151(c: Ctx) -> None:
    """`env.company` exists from v13; before that the company comes from the user."""
    has_company = _has(r"def company\(self\)", c.src.api())
    assert has_company is (c.major >= 13)
    if "env.company" in c.said:
        assert has_company
    else:
        assert "company_id" in c.said


@oracle("W8161", R(8, 21))
@oracle("W8162", R(9, 21))
@oracle("W8163", R(9, 21))
@oracle("W8164", R(10, 21))
@oracle("W8172", R(10, 21))
@oracle("W8176", R(10, 21))
def _super_fix_is_valid_python(c: Ctx) -> None:
    """A fix template written with zero-argument `super()` needs a Python 3 series."""
    py3 = "python_requires" in c.src.text("setup.py")
    assert py3 is (c.major >= 11)
    fix = c.rule.get("fix_template") or ""
    if re.search(r"super\(\)\.", fix):
        assert py3, f"{c.rule['rule_id']}: zero-argument super() is invalid Python 2 at v{c.major}"


@oracle("W8172", R(10, 21))
def _w8172(c: Ctx) -> None:
    sig = re.search(r"    def search\(self, (\w+)\b", c.src.models())
    assert sig
    assert sig.group(1) == ("args" if c.major <= 15 else "domain")
    fix = c.rule.get("fix_template") or ""
    for param in ("args", "domain"):
        if f"search({param}" in fix or f"`{param}` arg" in c.said:
            assert param == sig.group(1), (
                f"fix names `{param}`, source parameter is `{sig.group(1)}`"
            )


@oracle("W8171", R(10, 21))
def _w8171(c: Ctx) -> None:
    """A compute field can declare its dependencies by `depends=` from v12."""
    alt = _has(r"if 'depends' in attrs:", c.src.fields())
    assert alt is (c.major >= 12)
    assert ("depends=" in c.message) is alt, c.message


@oracle("W8173", R(10, 21))
def _w8173(c: Ctx) -> None:
    fields_text = c.src.fields()
    required_restrict = bool(
        re.search(r"'restrict' if self\.required else 'set null'", fields_text)
    )
    assert required_restrict is (c.major >= 13)
    assert "'set null'" in fields_text
    assert ("restrict" in c.message) is required_restrict, c.message


@oracle("W8174", R(10, 21))
def _w8174(c: Ctx) -> None:
    """A `(total, decimal)` tuple is a documented, undeprecated `digits` form."""
    fields_text = c.src.fields()
    assert _has(r":param (?:str )?digits: a pair \(total, decimal\)", fields_text)
    assert not _has(r"digits.{0,80}deprecat|deprecat.{0,80}digits", fields_text, re.IGNORECASE)
    tuples = [
        p
        for p in [*c.src.glob("addons/*/models/*.py"), *c.src.glob("odoo/addons/*/models/*.py")]
        if re.search(r"digits\s*=\s*\(\s*\d+\s*,\s*\d+\s*\)", _read_text(p))
    ]
    assert len(tuples) >= 10, "core itself uses the tuple form"
    string_digits = _has(
        r":param (?:str )?digits: a pair \(total, decimal\),? or a string", fields_text
    )
    assert string_digits is (c.major >= 13)
    assert ("name string" in c.message) is string_digits, c.message
    helper = c.src.exists("odoo/addons/decimal_precision/__init__.py") or c.src.exists(
        "addons/decimal_precision/__init__.py"
    )
    assert helper is (c.major <= 12)
    assert ("get_precision" in c.said) is helper, c.said
    assert not re.search(r"deprecated", c.message, re.IGNORECASE)


@oracle("W8175", R(10, 21))
def _w8175(c: Ctx) -> None:
    """A unique SQL constraint exists at every version: `_sql_constraints` through v18,
    `models.Constraint` after."""
    if c.major <= 18:
        assert _has(r"^\s*_sql_constraints\b", c.src.models())
        assert "'_sql_constraints' is no longer supported" not in c.src.models()
    else:
        assert _has(r"^class Constraint\(", c.src.text("odoo/orm/table_objects.py"))
    if "_sql_constraints" in c.said:
        assert c.major <= 18


@oracle("W8204", R(10, 19))
def _w8204(c: Ctx) -> None:
    assert _has(r"^\s*_sql_constraints\b", c.src.models())
    assert _has(r"_local_sql_constraints = cls\.__dict__\.get\('_sql_constraints'", c.src.models())


# ---------------------------------------------------------------------------
# Translation lazy format (W8122, W8201)
# ---------------------------------------------------------------------------


def _lazy_translation_args(src: Src) -> bool:
    return _has(
        r"def __call__\(self, source[^)]*\*args", src.text(f"{src.core}/tools/translate.py")
    )


@oracle("W8122", R(8, 21))
@oracle("W8201", R(16, 21))
def _lazy_translation_wording(c: Ctx) -> None:
    """`_('msg %s', arg)` exists from v14; a rule recommending it names no earlier series."""
    lazy = _lazy_translation_args(c.src)
    assert lazy is (c.major >= 14)
    if "lazy" in c.said:
        assert lazy
    assert not re.search(r"Odoo 1[56]\+", c.said), "the lazy format is available since v14"


# ---------------------------------------------------------------------------
# ORM cache API (W8165), legacy web.Widget (W8166), Owl 2 lifecycle (W8191)
# ---------------------------------------------------------------------------


@oracle("W8165", R(16, 21))
def _w8165(c: Ctx) -> None:
    models = c.src.models()
    defined = _has(r"def invalidate_cache\(", models)
    assert defined is (c.major == 16)
    for replacement in (
        "invalidate_model",
        "invalidate_recordset",
        "flush_model",
        "flush_recordset",
    ):
        assert _has(rf"def {replacement}\(", models), replacement
    if defined:
        body = re.search(
            r"def invalidate_cache\(.*?(?=\n    @|\n    def )", models, re.DOTALL
        ).group(0)
        assert "DeprecationWarning" in body
        assert "deprecated in v16" in c.message and "removed" not in c.message
    else:
        assert "removed" in c.message or "does not exist" in c.message
    assert not _has(r"def invalidate_recordset\(", c.other(15).models())


@oracle("W8166", [15, 16, 17])
def _w8166(c: Ctx) -> None:
    """The widget lives under `legacy/` with no deprecation marker; the AMD name `web.Widget`
    is defined through v16 and gone at v17, where the module is an ES module."""
    text = c.src.text("addons/web/static/src/legacy/js/core/widget.js")
    assert "deprecat" not in text.lower()
    assert "deprecated" not in c.message
    assert not c.other(14).exists("addons/web/static/src/legacy"), (
        "v14 keeps widgets outside legacy/"
    )
    assert ("odoo.define('web.Widget'" in text) is (c.major <= 16)
    assert ("@web/legacy/js/core/widget" in c.message) is (c.major == 17)


@oracle("W8191", [16, 17])
def _w8191(c: Ctx) -> None:
    """Owl 2 calls hooks only; the Owl 1 `mounted()` component method is not defined."""
    owl2 = c.src.text("addons/web/static/lib/owl/owl.js")
    assert re.search(r'version = "2\.', owl2)
    assert not _has(r"^\s*mounted\(\)\s*\{", owl2)
    assert _has(r"^\s*mounted\(\)\s*\{", c.other(15).text("addons/web/static/lib/owl/owl.js"))
    core_methods = [
        p
        for p in c.src.glob("addons/*/static/src/**/*.js")
        if "/tests/" not in str(p) and _has(r"^\s*(?:async )?mounted\s*\(\s*\)\s*\{", _read_text(p))
    ]
    assert not core_methods, core_methods[:3]
    assert "not call" in c.message


# ---------------------------------------------------------------------------
# Field attribute renames and view attributes (W8167-W8169, W8192, W8193, W8210)
# ---------------------------------------------------------------------------


@oracle("W8167", R(16, 21))
def _w8167(c: Ctx) -> None:
    mail12 = c.other(12).text("addons/mail/models/mail_thread.py")
    mail13 = c.other(13).text("addons/mail/models/mail_thread.py")
    assert (
        "getattr(field, 'track_visibility', False)" in mail12
        and "getattr(field, 'tracking'" not in mail12
    )
    assert "getattr(field, 'tracking', None) or getattr(field, 'track_visibility', None)" in mail13
    assert "v13" in c.message


@oracle("W8168", R(17, 21))
@oracle("W8169", R(17, 21))
def _attrs_states_removed(c: Ctx) -> None:
    check = 'Since 17.0, the "attrs" and "states" attributes are no longer used'
    assert check in c.src.text("odoo/addons/base/models/ir_ui_view.py")
    assert check not in c.other(16).text("odoo/addons/base/models/ir_ui_view.py")
    stated = re.findall(r"removed in v(\d+)", c.message)
    assert set(stated) <= {"17"}


@oracle("W8192", R(18, 21))
def _w8192(c: Ctx) -> None:
    view_py = c.src.text("odoo/addons/base/models/ir_ui_view.py")
    selection = re.search(r"type = fields\.Selection\(\[(.*?)\]", view_py, re.DOTALL).group(1)
    assert "('list'" in selection and "'tree'" not in selection
    assert "('tree'" in re.search(
        r"type = fields\.Selection\(\[(.*?)\]",
        c.other(17).text("odoo/addons/base/models/ir_ui_view.py"),
        re.DOTALL,
    ).group(1)
    assert not [
        p for p in c.src.glob("addons/*/views/*.xml") if re.search(r"<tree[\s/>]", _read_text(p))
    ]


@oracle("W8193", [18])
def _w8193(c: Ctx) -> None:
    """The v17 form parser special-cases `div.oe_chatter`; v18 core declares `<chatter/>`."""
    assert "oe_chatter" in c.other(17).text("addons/web/static/src/views/form/form_arch_parser.js")
    assert "oe_chatter" not in c.src.text("addons/web/static/src/views/form/form_arch_parser.js")
    views = [p for p in c.src.glob("addons/*/views/*.xml")]
    chatter = sum("<chatter" in _read_text(p) for p in views)
    legacy = sum("oe_chatter" in _read_text(p) for p in views)
    assert chatter >= 50 and legacy <= 3, (chatter, legacy)


@oracle("W8205", [18])
def _w8205(c: Ctx) -> None:
    assert "class TestInvisibleField" in c.src.text("odoo/addons/base/tests/test_views.py")
    assert "TestInvisibleField" not in "".join(
        _read_text(p) for p in c.other(17).glob("odoo/addons/base/tests/test_views.py")
    )


@oracle("W8210", R(18, 21))
def _w8210(c: Ctx) -> None:
    """`group_operator` -> `aggregator` is a v18 rename; v18 and v19 translate it, v20 does not."""
    fields_text = c.src.fields()
    assert "aggregator" in fields_text
    shim = "Since Odoo 18, 'group_operator' is deprecated, use 'aggregator' instead" in fields_text
    assert shim is (c.major in (18, 19))
    assert "group_operator" in c.other(17).fields() and "aggregator" not in c.other(17).fields()
    assert ("not translated" in c.message) is (not shim), c.message


@oracle("W8211", [19])
def _w8211(c: Ctx) -> None:
    """At v19 `read_group` keeps its v17 signature and is marked `@api.deprecated`."""
    models = c.src.models()
    assert '@api.deprecated("Since 19.0, read_group is deprecated' in models
    assert (
        "def read_group(self, domain, fields, groupby, offset=0, limit=None, "
        "orderby=False, lazy=True):" in models
    )
    assert re.search(r"def formatted_read_group\(", c.src.text("addons/web/models/models.py"))
    assert "lazy" not in c.message and "signature changed" not in c.message


@oracle("W8212", [18])
def _w8212(c: Ctx) -> None:
    config17 = c.other(17).text("odoo/tools/config.py")
    config18 = c.src.text("odoo/tools/config.py")
    assert "--longpolling-port" in config17 and "--gevent-port" in config17
    assert "--longpolling-port" not in config18 and "--gevent-port" in config18


@oracle("W8213", [18])
def _w8213(c: Ctx) -> None:
    """Hoot ships at v18 next to QUnit: the migration is under way, not finished."""
    assert c.src.exists("addons/web/static/lib/hoot")
    assert not c.other(17).exists("addons/web/static/lib/hoot")
    assert '"QUnit": "readonly"' in c.src.text("odoo/addons/test_lint/tests/eslintrc")
    still_qunit = [
        p
        for p in c.src.glob("addons/*/static/tests/**/*.js")
        if re.search(r"\bQUnit\s*\.", _read_text(p))
    ]
    assert len(still_qunit) >= 20
    assert "replaced" not in c.message


# ---------------------------------------------------------------------------
# Manifest assets (W8182)
# ---------------------------------------------------------------------------


@oracle("W8182", R(15, 21))
def _w8182(c: Ctx) -> None:
    """The `qweb` manifest key is read through v14 and ignored from v15, when `assets` starts."""
    v14 = c.other(14)
    assert "manifest_glob('qweb'" in v14.text("addons/web/controllers/main.py")
    assert re.search(
        r"(?:manifest\.get\('assets'|\['assets'\])",
        c.src.text("odoo/addons/base/models/ir_asset.py"),
    )
    assert not re.search(
        r"manifest_glob\('qweb'|\[.qweb.\]|get\('qweb'",
        "".join(_read_text(p) for p in c.src.glob("addons/web/controllers/*.py")),
    )
    declaring = [
        p
        for p in c.src.glob("addons/*/__manifest__.py")
        if re.search(r"^\s*'qweb'\s*:", _read_text(p), re.M)
    ]
    assert len(declaring) <= 1, "core manifests no longer declare qweb"


# ---------------------------------------------------------------------------
# Ruff / ESLint / vendored checkers
# ---------------------------------------------------------------------------


def _ruff_lint(src: Src) -> dict:
    return tomllib.loads(src.text("ruff.toml"))["lint"]


@oracle("BLE001", R(17, 21))
@oracle("I001", R(17, 21))
@oracle("F401", R(17, 21))
@oracle("UP", R(18, 21))
def _ruff_rule_is_selected(c: Ctx) -> None:
    lint = _ruff_lint(c.src)
    rid = c.rule["rule_id"]
    assert rid not in set(lint["ignore"])
    assert any(rid == sel or rid.startswith(sel) for sel in lint["select"]), rid


@oracle("E8501", R(14, 21))
def _e8501(c: Ctx) -> None:
    """E8501 is the id of the vendored SQL-injection checker (`rule_id_source: upstream`)."""
    from src.indexer.parser_lint_rules import _parse_pylint_odoo_source

    ids = set()
    for path in c.src.glob("odoo/addons/test_lint/tests/_odoo_checker_*.py"):
        ids |= {
            r.rule_id
            for r in _parse_pylint_odoo_source(path.read_text(encoding="utf-8"), "x", str(path))
        }
    assert "E8501" in ids
    assert c.rule["rule_id_source"] == "upstream"


def _eslint_configs(src: Src) -> str:
    return "".join(
        _read_text(p)
        for p in (
            src.path("addons/web/tooling/_eslintrc.json"),
            src.path("odoo/addons/test_lint/tests/eslintrc"),
        )
        if p.is_file()
    )


@oracle("no-undef", R(15, 21))
def _no_undef(c: Ctx) -> None:
    assert '"no-undef": "error"' in _eslint_configs(c.src)


@oracle("no-unused-vars", R(15, 21))
def _no_unused_vars(c: Ctx) -> None:
    config = _eslint_configs(c.src)
    assert '"no-unused-vars"' in config or '"extends": ["eslint:recommended"' in config


@oracle("owl/no-unknown-component", R(14, 21))
def _owl_components(c: Ctx) -> None:
    assert c.src.exists("addons/web/static/lib/owl/owl.js")
    assert not c.other(13).exists("addons/web/static/lib/owl")


@oracle("W8202", R(16, 21))
def _w8202(c: Ctx) -> None:
    """`name_get()` is defined through v17 (DeprecationWarning only from v17), gone from v18."""
    models = c.src.models()
    defined = _has(r"^\s*def name_get\b", models)
    assert defined is (c.major <= 17)
    stated = set(re.findall(r"removed in v(\d+)", c.message))
    assert stated <= {"18"}, c.message
    if defined:
        assert not stated or c.major == 17
        body = re.search(r"def name_get\(.*?(?=\n    @|\n    def )", models, re.DOTALL).group(0)
        assert ("DeprecationWarning" in body) is (c.major == 17)
    else:
        assert "removed in v18" in c.message


@oracle("owl/raw-contenteditable", R(16, 20))
def _raw_contenteditable(c: Ctx) -> None:
    """The editor module the message names ships a `Wysiwyg` at that version."""
    legacy = "web_editor" in c.message
    modern = "html_editor" in c.message
    assert legacy is not modern, c.message
    if legacy:
        text = c.src.text("addons/web_editor/static/src/js/wysiwyg/wysiwyg.js")
        assert "web_editor.wysiwyg" in text or re.search(r"class Wysiwyg\b", text)
    else:
        assert re.search(
            r"export class Wysiwyg\b", c.src.text("addons/html_editor/static/src/wysiwyg.js")
        )
        assert not c.src.exists("addons/web_editor")
    assert c.src.exists("addons/web_editor") is (c.major <= 18)


@oracle("owl/dialog-body-slot", R(16, 21))
def _dialog_slots(c: Ctx) -> None:
    xml = c.src.text("addons/web/static/src/core/dialog/dialog.xml")
    slots = set(re.findall(r't-(?:call-)?slot="(\w+)"', xml))
    assert slots == {"header", "default", "footer"}, slots


@oracle("owl/no-bare-arrow-handler", R(16, 21))
def _bare_arrow(c: Ctx) -> None:
    """Core templates reach component members through `this.` in event arrows."""
    arrows = [
        p
        for p in c.src.glob("addons/web/static/src/**/*.xml")
        if re.search(r't-on-[a-z.]+="[^"]*\bthis\.', _read_text(p))
    ]
    assert len(arrows) >= 20


# ---------------------------------------------------------------------------
# v20-only rules: guarded by the v20 module (reused, not duplicated)
# ---------------------------------------------------------------------------

_V20_GUARDS.update(
    {
        "W8120": "test_w8221_w8222_ir_access_replaces_model_access_and_rules",
        "W8192": "test_w8192_tree_is_not_a_v20_view_type",
        "W8211": "test_w8211_read_group_signature_changed_at_v20",
        "W8213": "test_w8213_qunit_is_not_used_by_v20_source",
        "owl/raw-contenteditable": (
            "test_raw_contenteditable_rule_points_at_a_wysiwyg_that_exists_at_v20"
        ),
        "W8220": "test_w8220_api_deprecated_moved_to_tools_func",
        "W8221": "test_w8221_w8222_ir_access_replaces_model_access_and_rules",
        "W8222": "test_w8221_w8222_ir_access_replaces_model_access_and_rules",
        "W8223": "test_w8223_registry_clear_cache_is_replaced_by_transaction_invalidate_ormcache",
        "W8224": "test_w8224_replacement_api_exists_at_v20",
        "W8225": "test_removed_in_v20_symbols_are_defined_at_v19_and_gone_at_v20",
        "W8226": "test_removed_in_v20_symbols_are_defined_at_v19_and_gone_at_v20",
        "W8227": "test_w8227_deprecated_model_attributes_match_test_lint",
        "W8228": "test_w8228_sql_constraints_is_unsupported_and_constraint_replaces_it",
        "W8230": "test_w8230_base64_xml_type_is_deprecated_at_v20",
        "W8231": "test_w8231_w8232_test_lint_enforces_icons_and_orm_imports",
        "W8232": "test_w8231_w8232_test_lint_enforces_icons_and_orm_imports",
        "W8233": "test_w8233_model_private_properties_are_deprecated_in_v19_and_v20",
        "W8234": "test_w8234_search_date_filter_range_attributes_are_not_parsed_at_v20",
        "owl/use-effect-single-callback": "test_v20_eslintrc_matches_the_owl3_rules_and_globals",
        "owl/use-effect-no-params": "test_v20_eslintrc_matches_the_owl3_rules_and_globals",
        "owl/props-via-use-props": "test_owl3_facts_behind_the_v20_owl_rules",
    }
)


@oracle("W8229", [20])
def _w8229(c: Ctx) -> None:
    assert _has(r"^\s*sanitize: bool = True\b", c.src.fields())


# ---------------------------------------------------------------------------
# Editorial rules: convention about user code, no Odoo-source behaviour to diff
# ---------------------------------------------------------------------------

editorial(
    "W8113",
    "v8: advises not browsing an empty id list - a usage convention with no framework "
    "behaviour behind it",
    [8],
)
editorial(
    "W8115",
    "a model class declares `_name` or `_inherit`; the rule states the convention, the "
    "loader accepts the class either way",
)
editorial(
    "W8116", "an `_inherit` without `_name` is legal; the rule asks the author to confirm intent"
)
editorial(
    "W8121",
    "every `ref=` must resolve; resolution depends on the user's module graph, not on a "
    "framework fact",
)
editorial("W8123", "translatable sentences are not concatenated: a translation-quality convention")
editorial(
    "W8130",
    "a manifest `data` entry must exist: a property of the user's module tree, enforced at install",
)
editorial(
    "W8131",
    "a manifest `demo` entry must exist: a property of the user's module tree, enforced at install",
    [8, 9, 10, 11, 12],
)
editorial(
    "W8140",
    "SQL built by string interpolation is an injection risk at every version: a security "
    "convention",
    R(8, 14),
)
editorial("W8150", "a literal record id breaks across databases: a portability convention")
editorial("W8160", "`_rec_name` names a field of the model: a consistency convention")
editorial(
    "W8170",
    "v8: JavaScript files must be listed in the manifest `js` / `qweb` keys, which the web "
    "controller reads through manifest_list",
    [8],
)
editorial("W8170", "an ORM call inside a record loop is a performance convention", R(10, 21))
editorial("W8179", "`_order` names fields of the model: a consistency convention")
editorial("W8183", "an xpath `position='replace'` removes the target: a reminder to confirm intent")
editorial("W8203", "archived records need `active_test=False`: a usage convention")
editorial(
    "BLE001",
    "general Python hygiene; no ruff configuration ships before v17 (ruff.toml exists from v17)",
    R(10, 17),
)
editorial(
    "I001",
    "import ordering follows the Odoo coding guidelines; no ruff configuration ships before v17",
    R(10, 17),
)
editorial(
    "F401",
    "unused imports are general Python hygiene; no ruff configuration ships before v17",
    R(10, 17),
)
editorial(
    "no-undef", "generic JavaScript hygiene; no ESLint configuration ships before v15", R(10, 15)
)
editorial(
    "no-unused-vars",
    "generic JavaScript hygiene; no ESLint configuration ships before v15",
    R(10, 15),
)
editorial(
    "owl/useservice-not-reactive",
    "advice about reactivity of service results; depends on the service, not on a framework fact",
)


def _is_editorial(rule_id: str, major: int) -> bool:
    return any(vs is None or major in vs for vs, _ in _EDITORIAL.get(rule_id, []))


def _is_oracled(rule_id: str, major: int) -> bool:
    return any(major in vs for vs, _ in _ORACLES.get(rule_id, []))


def _is_v20_guarded(rule_id: str, major: int) -> bool:
    return major == 20 and rule_id in _V20_GUARDS


# ---------------------------------------------------------------------------
# Layer 1 - coverage (CI-safe)
# ---------------------------------------------------------------------------


def test_every_curated_rule_is_guarded_by_an_oracle_or_carries_a_reason():
    """Business rule (ADR-0055): no curated `(version, rule)` pair ships without a
    source oracle or a written reason why none can exist."""
    unguarded = [
        f"{rid}@{m}.0"
        for m in _MAJORS
        for rid in _CURATED[m]
        if not (_is_oracled(rid, m) or _is_editorial(rid, m) or _is_v20_guarded(rid, m))
    ]
    assert not unguarded, unguarded


def test_oracle_and_editorial_entries_name_curated_rules_only():
    """Business rule: a guard for a rule the catalogue no longer curates at that
    version is stale and hides a deleted guarantee."""
    stale: list[str] = []
    for rid, entries in _ORACLES.items():
        if rid.startswith("dropped:"):
            continue
        for versions, fn in entries:
            missing = sorted(v for v in versions if v in _MAJORS and rid not in _CURATED[v])
            if missing:
                stale.append(f"oracle {rid}.{fn.__name__} covers uncurated {missing}")
    for rid, entries in _EDITORIAL.items():
        for versions, _ in entries:
            have = _versions_of(rid)
            if not have or (versions is not None and not versions <= have):
                stale.append(f"editorial {rid} {sorted(versions or [])} not curated")
    for rid, fn_name in _V20_GUARDS.items():
        if rid not in _CURATED[20]:
            stale.append(f"v20 guard {rid} not curated at 20")
        if not hasattr(v20_guards, fn_name):
            stale.append(f"v20 guard {rid} names missing test {fn_name}")
    assert not stale, stale


def test_every_editorial_reason_is_a_sentence():
    for rid, entries in _EDITORIAL.items():
        for _, reason in entries:
            assert len(reason) > 25 and reason.isascii(), (rid, reason)


@pytest.mark.parametrize("major", _MAJORS)
def test_curated_rule_ids_are_unique_per_version(major):
    ids = [r["rule_id"] for r in _load(major)["rules"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("major", _MAJORS)
def test_parse_lint_rules_for_version_has_no_duplicate_ids(major):
    """Business rule: the merged catalogue holds one record per rule id at every version."""
    ids = [r.rule_id for r in parse_lint_rules_for_version(f"{major}.0")]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, dupes


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", [m for m in _MAJORS if m >= 14])
def test_parse_lint_rules_for_version_with_live_checkers_has_no_duplicate_ids(major):
    src = _src(major)
    ids = [
        r.rule_id
        for r in parse_lint_rules_for_version(f"{major}.0", odoo_source_root=str(src.root))
    ]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, dupes


# ---------------------------------------------------------------------------
# Layer 2 - source oracles for every (version, rule)
# ---------------------------------------------------------------------------

_ORACLE_CASES = sorted(
    (m, rid, fn.__name__)
    for rid, entries in _ORACLES.items()
    if not rid.startswith("dropped:")
    for versions, fn in entries
    for m in versions
    if m in _MAJORS and rid in _CURATED[m]
)
_BY_NAME = {(rid, fn.__name__): fn for rid, entries in _ORACLES.items() for _, fn in entries}


@pytest.mark.odoo_source
@pytest.mark.parametrize(("major", "rule_id", "oracle_name"), _ORACLE_CASES)
def test_rule_premise_holds_in_source(major, rule_id, oracle_name):
    """Business rule: what a curated rule asserts about Odoo `major` is true in
    that version's source - the flagged construct exists and is discouraged
    there, the named replacement exists there, the named boundary is real."""
    ctx = Ctx(major=major, rule=_CURATED[major][rule_id], src=_src(major))
    _BY_NAME[(rule_id, oracle_name)](ctx)


# ---------------------------------------------------------------------------
# Layer 3 - dropped premises
# ---------------------------------------------------------------------------


def _core_py(src: Src) -> list[Path]:
    return [*src.glob("addons/*/models/*.py"), *src.glob("odoo/addons/*/models/*.py")]


@dropped_premise("W8107", [11, 12])
def _d_w8107(c: Ctx) -> None:
    """At v11/v12 `@api.multi` is the standard decorator, with no deprecation marker."""
    multi = re.search(
        r"^def multi\(method\):(.*?)^def ", c.src.api(), re.DOTALL | re.MULTILINE
    ).group(1)
    assert "deprecat" not in multi.lower()
    used = sum(len(re.findall(r"@api\.multi\b", _read_text(p))) for p in _core_py(c.src))
    assert used > 500


@dropped_premise("W8114", [10])
def _d_w8114(c: Ctx) -> None:
    """Core `@api.multi` methods return booleans and lists, e.g. `write` returns True."""
    models = c.src.models()
    write = re.search(
        r"@api\.multi\n    def write\(self, vals\):(.*?)\n    @api", models, re.DOTALL
    ).group(1)
    assert "return True" in write


@dropped_premise("W8165", [15])
def _d_w8165(c: Ctx) -> None:
    models = c.src.models()
    assert _has(r"def invalidate_cache\(", models)
    body = re.search(r"def invalidate_cache\(.*?(?=\n    @|\n    def )", models, re.DOTALL).group(0)
    assert "DeprecationWarning" not in body and "warn" not in body
    assert not _has(r"def invalidate_recordset\(", models)


@dropped_premise("W8177", R(10, 20))
def _d_w8177(c: Ctx) -> None:
    """Constraint messages are plain strings in core; `_()` in a class body cannot translate."""
    if c.major >= 19:
        assert "'_sql_constraints' is no longer supported" in c.src.text(
            "odoo/orm/model_classes.py"
        )
        return
    tuples = wrapped = 0
    for path in _core_py(c.src):
        for block in re.findall(
            r"_sql_constraints\s*=\s*\[(.*?)\n\s*\]", _read_text(path), re.DOTALL
        ):
            tuples += len(re.findall(r"\(\s*['\"]\w+['\"]\s*,", block))
            wrapped += len(re.findall(r"['\"]\s*,\s*_\(", block))
    assert tuples > 50 and wrapped / tuples < 0.15, (tuples, wrapped)


@dropped_premise("W8178", R(10, 20))
def _d_w8178(c: Ctx) -> None:
    """`fields.Html` sanitizes by default at every version."""
    fields_text = c.src.fields()
    assert _has(r"^\s*(?:sanitize: bool = True|sanitize = True|'sanitize': True)\b", fields_text)


@dropped_premise("W8180", R(10, 15))
def _d_w8180(c: Ctx) -> None:
    """A `<template>` takes its QWeb name from `id`; core never writes `t-name` on it."""
    convert = c.src.text(f"{c.src.core}/tools/convert.py")
    assert _has(r"tpl_id = el\.get\('id', el\.get\('t-name'", convert)
    assert "el.set('t-name', full_tpl_id)" in convert
    templates = reports = 0
    for path in c.src.glob("addons/*/report/*.xml"):
        text = _read_text(path)
        templates += len(re.findall(r"<template\b", text))
        reports += len(re.findall(r"<template\b[^>]*\bt-name=", text))
    assert templates > 20 and reports == 0


@dropped_premise("W8181", [14])
def _d_w8181(c: Ctx) -> None:
    """The v14 web client reads the manifest `qweb` key; the `assets` key does not exist yet."""
    assert "manifest_glob('qweb'" in c.src.text("addons/web/controllers/main.py")
    assert "'assets'" not in c.src.defaults_block()
    assert "manifest.get('assets', {})" not in "".join(
        _read_text(p) for p in c.src.glob("odoo/addons/base/models/*.py")
    )


@dropped_premise("W8184", R(10, 21))
def _d_w8184(c: Ctx) -> None:
    """Primary views are `ir.ui.view` records without `inherit_id`: correct code."""
    primary = 0
    for path in c.src.glob("addons/*/views/*.xml"):
        for record in re.findall(
            r'<record\b[^>]*model="ir\.ui\.view"[^>]*>(.*?)</record>', _read_text(path), re.DOTALL
        ):
            primary += "inherit_id" not in record
    assert primary > 100, primary


@dropped_premise("W8185", [10])
def _d_w8185(c: Ctx) -> None:
    """Group records without `users` or `implied_ids` are everyday core data."""
    bare = 0
    for path in c.src.glob("addons/*/security/*.xml"):
        for record in re.findall(
            r'<record\b[^>]*model="res\.groups"[^>]*>(.*?)</record>', _read_text(path), re.DOTALL
        ):
            bare += "implied_ids" not in record and 'name="users"' not in record
    assert bare >= 10, bare


@dropped_premise("W8186", [10])
def _d_w8186(c: Ctx) -> None:
    """`(1, '=', 1)` is the framework's own TRUE leaf; `(True, '=', True)` is not a leaf."""
    assert "TRUE_LEAF = (1, '=', 1)" in c.src.text("odoo/osv/expression.py")


@dropped_premise("W8190", R(13, 18))
def _d_w8190(c: Ctx) -> None:
    """Owl components are used through `static components`; most are never registered."""
    if c.major == 13:
        assert not c.src.exists("addons/web/static/lib/owl")
        return
    plain = registered = 0
    for path in c.src.glob("addons/*/static/src/**/*.js"):
        text = _read_text(path)
        if "/tests/" in str(path) or not re.search(r"class \w+ extends Component", text):
            continue
        if re.search(r"registry\.category\([^)]*\)\.add\(", text):
            registered += 1
        else:
            plain += 1
    assert plain > registered, (plain, registered)


@dropped_premise("W8191", [15])
def _d_w8191(c: Ctx) -> None:
    """Owl 1 (v15) calls the component `mounted()` method; core defines it widely."""
    methods = [
        p
        for p in c.src.glob("addons/*/static/src/**/*.js")
        if "/tests/" not in str(p) and _has(r"^\s*(?:async )?mounted\s*\(\s*\)\s*\{", _read_text(p))
    ]
    assert len(methods) >= 20
    assert re.search(r"version = '1\.", c.src.text("addons/web/static/lib/owl/owl.js"))


@dropped_premise("W8204", [19])
def _d_w8204(c: Ctx) -> None:
    assert "'_sql_constraints' is no longer supported" in c.src.text("odoo/orm/model_classes.py")


@dropped_premise("W8211", [18])
def _d_w8211(c: Ctx) -> None:
    """`read_group` keeps its v17 signature at v18 (with `lazy`)."""
    sig = (
        "def read_group(self, domain, fields, groupby, offset=0, limit=None, "
        "orderby=False, lazy=True):"
    )
    assert sig in c.src.models() and sig in c.other(17).models()


@dropped_premise("W8220", [19])
def _d_w8220(c: Ctx) -> None:
    """`@api.deprecated` is how v19 core marks deprecated symbols; using it is not a defect."""
    models = c.src.models()
    assert len(re.findall(r"@api\.deprecated\(", models)) >= 3
    assert _has(r"^\s*deprecated,", c.src.text("odoo/api/__init__.py"))


@dropped_premise("E501", R(10, 20))
def _d_e501(c: Ctx) -> None:
    """Odoo enforces no line length: ruff.toml ignores E501 (v17+) and core has many long lines."""
    if c.major >= 17:
        assert "E501" in _ruff_lint(c.src)["ignore"]
    lines = c.src.models().splitlines()
    long_lines = sum(len(ln) >= 80 for ln in lines)
    assert long_lines / len(lines) > 0.05


@dropped_premise("no-var", R(10, 21))
def _d_no_var(c: Ctx) -> None:
    """`var` is Odoo core style through the legacy JS era and no ESLint config selects `no-var`."""
    assert "no-var" not in _eslint_configs(c.src)
    files = [
        p
        for p in c.src.glob("addons/*/static/src/**/*.js")
        if "/lib/" not in str(p) and "/tests/" not in str(p)
    ]
    using = sum(bool(re.search(r"^\s*var\s+\w+", _read_text(p), re.MULTILINE)) for p in files)
    assert using >= 20, using
    if c.major <= 14:
        assert using / len(files) > 0.4


@dropped_premise("no-console", R(10, 21))
def _d_no_console(c: Ctx) -> None:
    """Core JS logs through `console.warn` / `console.error`; no ESLint config selects it."""
    assert "no-console" not in _eslint_configs(c.src)
    files = [
        p
        for p in c.src.glob("addons/*/static/src/**/*.js")
        if "/lib/" not in str(p) and "/tests/" not in str(p)
    ]
    using = sum(bool(re.search(r"\bconsole\s*\.", _read_text(p))) for p in files)
    assert using >= 20, using


@dropped_premise("no-restricted-globals", R(10, 16))
def _d_no_restricted_globals(c: Ctx) -> None:
    assert "no-restricted-globals" not in _eslint_configs(c.src)


@dropped_premise("odoo-no-service-get", [11])
def _d_odoo_no_service_get(c: Ctx) -> None:
    assert not [
        p for p in c.src.glob("addons/*/static/src/**/*.js") if "useService" in _read_text(p)
    ]
    assert not c.src.exists("addons/web/static/lib/owl")


@dropped_premise("owl/no-unknown-component", [13])
def _d_owl_unknown_component(c: Ctx) -> None:
    assert not c.src.exists("addons/web/static/lib/owl")


_DROPPED_CASES = sorted(
    (m, rid, fn.__name__)
    for key, entries in _ORACLES.items()
    if key.startswith("dropped:")
    for rid in [key.split(":", 1)[1]]
    for versions, fn in entries
    for m in versions
    if m in _MAJORS
)
_DROPPED_BY_NAME = {
    (key.split(":", 1)[1], fn.__name__): fn
    for key, entries in _ORACLES.items()
    if key.startswith("dropped:")
    for _, fn in entries
}


@pytest.mark.parametrize(
    ("rule_id", "major"), sorted((rid, m) for rid, vs in _DROPPED.items() for m in vs)
)
def test_dropped_rule_stays_dropped(rule_id, major):
    """Business rule: a rule whose premise is false at a version is not served there."""
    assert rule_id not in _CURATED[major]


@pytest.mark.odoo_source
@pytest.mark.parametrize(("major", "rule_id", "oracle_name"), _DROPPED_CASES)
def test_dropped_rule_premise_is_false_in_source(major, rule_id, oracle_name):
    """Business rule: the premise of a dropped rule really is false in that
    version's source - the flagged code is correct code there."""
    _DROPPED_BY_NAME[(rule_id, oracle_name)](Ctx(major=major, rule={}, src=_src(major)))


# ---------------------------------------------------------------------------
# `_note` version claims
# ---------------------------------------------------------------------------

_OWL_VERSION_CLAIMS = {
    15: {"1.4.11", "2.8.4"},
    19: {"2.8.4"},
    20: {"3.0.0-alpha.50"},
}


def _owl_version(src: Src) -> str:
    text = src.text("addons/web/static/lib/owl/owl.js")
    match = re.search(r"""version\s*=\s*["']([^"']+)["']""", text) or re.search(
        r"""__info__\.version = ['"]([^'"]+)['"]""", text
    )
    return match.group(1)


@pytest.mark.parametrize("major", sorted(_OWL_VERSION_CLAIMS))
def test_note_states_only_owl_versions_the_notes_claim(major):
    """Business rule: every Owl version a `_note` quotes is spelled exactly as quoted."""
    quoted = set(
        re.findall(
            r'(?:reports(?: version)?|Owl is) "?(\d+\.\d+\.\d+[\w.-]*)', _load(major)["_note"]
        )
    )
    assert quoted == _OWL_VERSION_CLAIMS[major], quoted


@pytest.mark.odoo_source
def test_note_owl_versions_match_owl_js_of_every_series():
    """Business rule: the Owl version a note states for series N is what `owl.js` of N reports."""
    expected = {
        14: "1.4.11",
        15: "1.4.11",
        16: "2.8.4",
        17: "2.8.4",
        18: "2.8.4",
        19: "2.8.4",
        20: "3.0.0-alpha.50",
    }
    for major, version in expected.items():
        assert _owl_version(_src(major)) == version, major
    assert not _src(13).exists("addons/web/static/lib/owl")
    assert "2.8.4" in _load(15)["_note"] and "2.8.2" not in _load(15)["_note"]
    assert "2.8.4" in _load(19)["_note"] and "2.8.2" not in _load(19)["_note"]


@pytest.mark.odoo_source
def test_note_manifest_and_python_claims_hold():
    """Business rule: the manifest-name, Python-version and ESLint-config note claims hold."""
    v9, v10, v11 = _src(9), _src(10), _src(11)
    assert not v9.glob("addons/*/__manifest__.py") and v9.glob("addons/*/__openerp__.py")
    assert "MANIFEST_NAMES" in v10.module_py()
    assert "execfile(" in v10.text("setup.py") and "python_requires" not in v10.text("setup.py")
    assert "python_requires='>=3.5'" in v11.text("setup.py")
    for major in (11, 12, 13, 14):
        src = _src(major)
        assert not [
            p
            for p in src.root.rglob("*")
            if p.name.startswith(".eslintrc") and "node_modules" not in str(p)
        ]
        assert not src.exists("addons/web/tooling/_eslintrc.json"), major
    assert _src(15).exists("addons/web/tooling/_eslintrc.json")


@pytest.mark.odoo_source
def test_note_api_and_legacy_claims_hold():
    """Business rule: the api / legacy-widget / active_test claims of the notes are true."""
    assert not _decorator_defined(_src(13), "multi") and not _decorator_defined(_src(13), "one")
    assert _decorator_defined(_src(12), "multi")
    assert "active_test" in _src(10).models()
    assert not _src(13).exists("addons/web/static/lib/owl") and _src(14).exists(
        "addons/web/static/lib/owl"
    )
    assert _src(14).exists("addons/web/static/src/js/core/widget.js")
    assert _src(15).exists("addons/web/static/src/legacy/js/core/widget.js")
    assert _has(r"^def model_create_multi\(", _src(12).api())
    assert _has(
        r"^def deprecated|^    class deprecated|from warnings import deprecated",
        _src(19).text("odoo/orm/decorators.py"),
    )
    assert not re.search(r"\bdeprecated\b", _src(18).text("odoo/api.py"))
    assert (
        _src(17).exists("ruff.toml")
        and _src(18).exists("ruff.toml")
        and not _src(16).exists("ruff.toml")
    )
    assert not _src(12).glob("addons/spreadsheet") and not _src(13).glob("addons/spreadsheet")
