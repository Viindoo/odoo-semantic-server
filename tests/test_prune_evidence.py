# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source oracle of the lifecycle prune (ADR-0057, ``src/indexer/prune_evidence.py``).

Business rule: **delete what git proves gone, keep what the source still
defines.** The oracle reads the module's files at HEAD from the git object
database and answers, for every entity the parse no longer produced, whether
the source still defines it. It is one-directional: it may answer ``present``
for something git deleted (a ghost is kept and reported as a parser gap), but
it must NEVER answer ``gone`` for something the code still has - that would
delete a live entity from OSM.

No database: every test builds a small real git repository in ``tmp_path``.
The property tests at the bottom run the REAL OSM parsers over fixture modules
written in unusual-but-legal forms and assert the oracle calls every entity and
relationship they produce ``present`` - the guard against a pattern that is too
narrow (the dangerous direction). Expected verdicts come from the rule above,
never from what the implementation returns.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from src.indexer import prune_evidence as pe
from src.indexer.prune_evidence import (
    VERDICT_GONE,
    VERDICT_PRESENT,
    VERDICT_SUPERSEDED,
    StaleNode,
    StaleRel,
    classify,
    load_module_source,
)

MOD = "viin_demo"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class Repo:
    """A git repository with one Odoo module directory ``viin_demo/``."""

    def __init__(self, root: Path) -> None:
        self.path = root
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        self.write("__manifest__.py", repr({"name": "Demo", "depends": ["base"]}))
        self.write("__init__.py", "")

    def write(self, rel: str, text: str, *, module: str = MOD) -> Path:
        target = self.path / module / rel if module else self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(text))
        return target

    def write_bytes(self, rel: str, data: bytes) -> None:
        target = self.path / MOD / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def rm(self, rel: str) -> None:
        (self.path / MOD / rel).unlink()

    def commit(self, msg: str = "c") -> str:
        _git(self.path, "add", "-A")
        _git(self.path, "commit", "-q", "--allow-empty", "-m", msg)
        return _git(self.path, "rev-parse", "HEAD")

    def source(self, previous=()):
        return load_module_source(self.path, MOD, MOD, previous)


def _verdict(src, node: StaleNode) -> str:
    return classify(src, [node]).nodes[node.id]


def _rel_verdict(src, rel: StaleRel) -> str:
    return classify(src, [], [rel]).rels[rel.id]


def field(name, model="demo.thing"):
    return StaleNode(id=f"f:{name}", label="Field", name=name, model=model)


def method(name, model="demo.thing"):
    return StaleNode(id=f"m:{name}", label="Method", name=name, model=model)


def model(name):
    return StaleNode(id=f"M:{name}", label="Model", name=name)


# ---------------------------------------------------------------------------
# Core rule
# ---------------------------------------------------------------------------

def test_a_field_removed_by_a_commit_is_gone_and_the_ones_kept_are_present(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("models/thing.py", """\
        from odoo import fields, models

        class Thing(models.Model):
            _name = "demo.thing"
            keep_me = fields.Char()
            drop_me = fields.Char()
    """)
    repo.commit("add")
    repo.write("models/thing.py", """\
        from odoo import fields, models

        class Thing(models.Model):
            _name = "demo.thing"
            keep_me = fields.Char()
    """)
    repo.commit("[REM] drop_me")
    src = repo.source()

    assert src.available, src.reason
    assert _verdict(src, field("drop_me")) == VERDICT_GONE
    assert _verdict(src, field("keep_me")) == VERDICT_PRESENT


def test_the_oracle_reads_head_not_the_working_tree(tmp_path):
    """A file deleted on disk but still committed is still defined at HEAD
    (the parser may have missed it - keep); a field added on disk but not
    committed does not exist for git."""
    repo = Repo(tmp_path / "r")
    repo.write("models/a.py", "class A:\n    _name = 'demo.a'\n    only_in_head = fields.Char()\n")
    repo.commit()
    repo.rm("models/a.py")
    repo.write("models/b.py", "class B:\n    _name = 'demo.b'\n    only_on_disk = fields.Char()\n")
    src = repo.source()

    assert _verdict(src, field("only_in_head", "demo.a")) == VERDICT_PRESENT
    assert _verdict(src, field("only_on_disk", "demo.b")) == VERDICT_GONE


def test_no_baseline_is_needed_a_rewritten_history_is_judged_by_head_alone(tmp_path):
    """Force-push / unknown baseline: HEAD on an unrelated root still decides."""
    repo = Repo(tmp_path / "r")
    repo.write("models/a.py", "class A:\n    _name = 'demo.a'\n    old = fields.Char()\n")
    repo.commit()
    _git(repo.path, "checkout", "-q", "--orphan", "rewritten")
    _git(repo.path, "rm", "-rq", "--cached", ".")
    repo.write("models/a.py", "class A:\n    _name = 'demo.a'\n    new = fields.Char()\n")
    repo.commit("rewritten root")
    src = repo.source()

    assert _verdict(src, field("old", "demo.a")) == VERDICT_GONE
    assert _verdict(src, field("new", "demo.a")) == VERDICT_PRESENT


# ---------------------------------------------------------------------------
# Every form a parser reads an identifier from is "present"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "class T:\n    f_name = fields.Char()\n",
    "class T:\n    f_name=fields.Char()\n",
    "class T:\n    f_name: fields.Many2one = fields.Many2one('res.partner')\n",
    "class T:\n    (f_name) = fields.Char()\n",
    "class T:\n    f_name \\\n        = fields.Char()\n",
    "class T:\n    _columns = {\n        'f_name': fields.char('X'),\n    }\n",
    "class T:\n    _columns = {\n        'f_name'\n        : fields.char('X'),\n    }\n",
    "class T:\n    _columns.update({\"f_name\": fields.integer()})\n",
], ids=["assign", "no-space", "annotated", "parenthesized", "backslash", "columns",
        "columns-newline", "columns-update"])
def test_every_field_declaration_form_counts_as_present(tmp_path, text):
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", text)
    repo.commit()
    assert _verdict(repo.source(), field("f_name")) == VERDICT_PRESENT


@pytest.mark.parametrize("text", [
    "class T:\n    def do_it(self):\n        pass\n",
    "class T:\n    @api.depends(\n        'a',\n    )\n    def do_it(self):\n        pass\n",
    "class T:\n    async def do_it(self):\n        pass\n",
    "class T:\n    def \\\n            do_it(self):\n        pass\n",
], ids=["plain", "decorated-multiline", "async", "backslash"])
def test_every_method_definition_form_counts_as_present(tmp_path, text):
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", text)
    repo.commit()
    assert _verdict(repo.source(), method("do_it")) == VERDICT_PRESENT


def test_a_method_only_called_but_no_longer_defined_is_gone(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", "class T:\n    def caller(self):\n        return self.do_it()\n")
    repo.commit()
    assert _verdict(repo.source(), method("do_it")) == VERDICT_GONE


def test_a_model_named_through_a_constant_in_another_file_is_present(tmp_path):
    """viin_ai 17.0 (commit 8ee88bae6): ``_name = INTROSPECT_SERVICE`` with the
    literal in a constants module of the same addon. The model still exists,
    so it must be kept (never deleted as 'gone')."""
    repo = Repo(tmp_path / "r")
    repo.write("const.py", "INTROSPECT_SERVICE = 'viin.ai.ui.introspect'\n")
    repo.write("models/ui.py", textwrap.dedent("""\
        from ..const import INTROSPECT_SERVICE

        class UiIntrospect(models.AbstractModel):
            _name = INTROSPECT_SERVICE
    """))
    repo.commit()
    assert _verdict(repo.source(), model("viin.ai.ui.introspect")) == VERDICT_PRESENT


def test_a_model_name_is_matched_whole_not_as_the_prefix_of_another(tmp_path):
    """freight patch1: only ``'sale.order.line'`` is left - ``sale.order`` is gone."""
    repo = Repo(tmp_path / "r")
    repo.write("models/l.py", "class L(models.Model):\n    _inherit = 'sale.order.line'\n")
    repo.commit()
    src = repo.source()
    assert _verdict(src, model("sale.order")) == VERDICT_GONE
    assert _verdict(src, model("sale.order.line")) == VERDICT_PRESENT


@pytest.mark.parametrize("text", [
    "class X(models.Model):\n    _inherit = 'res.partner'\n",
    "class X(models.Model):\n    _inherit = ['mail.thread', 'res.partner']\n",
    "class X(models.Model):\n    _name = \"res.partner\"\n",
    "<odoo><record model=\"res.partner\" id=\"p\"/></odoo>\n",
], ids=["inherit-only", "inherit-list", "double-quotes", "xml"])
def test_inherited_only_and_other_model_forms_are_present(tmp_path, text):
    repo = Repo(tmp_path / "r")
    repo.write("data/x.xml" if text.startswith("<") else "models/x.py", text)
    repo.commit()
    assert _verdict(repo.source(), model("res.partner")) == VERDICT_PRESENT


def test_a_field_left_only_in_a_comment_is_gone_where_python_parses_the_file(tmp_path):
    """A comment defines nothing: in a file Python parses, the class body is
    read through the AST, so ``# legacy = fields.Char()`` does not keep a
    deleted field."""
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", "class T:\n    # legacy = fields.Char()  (removed)\n    pass\n")
    repo.commit()
    assert _verdict(repo.source(), field("legacy")) == VERDICT_GONE


def test_a_field_left_only_in_a_comment_of_a_python2_file_is_kept(tmp_path):
    """The safe direction where the AST is not available: in a file Python 3
    cannot parse (v8-v10 Python 2 source) only the text rule applies, which
    is weaker than any parser - a mention in a comment keeps the field (it is
    reported as a parser gap) rather than proving it gone."""
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", "class T:\n    # legacy = fields.Char()  (removed)\n"
                              "    def f(self):\n        print 'py2'\n")
    repo.commit()
    assert _verdict(repo.source(), field("legacy")) == VERDICT_PRESENT


# ---------------------------------------------------------------------------
# Class identity the oracle cannot evaluate: nothing about it is proven
# ---------------------------------------------------------------------------

def _drop_unresolved_rule(monkeypatch):
    """Break the rule under test: an identity the oracle cannot evaluate is
    treated as naming no model at all (instead of 'may name any model')."""
    real = pe._meta_names
    monkeypatch.setattr(pe, "_meta_names", lambda index, cls: real(index, cls) or set())


_CROSS_ADDON_INHERIT = """\
    from odoo import fields, models
    from odoo.addons.viin_other.const import PARTNER_MODEL, PARENT_MODEL


    class Partner(models.Model):
        _inherit = PARTNER_MODEL

        loyalty_code = fields.Char()

        def action_loyalty(self):
            return True


    class Child(models.Model):
        _name = "viin.demo.child"
        _inherit = PARENT_MODEL
"""


def _cross_addon_checks(src):
    return {
        "model": _verdict(src, model("res.partner")),
        "field": _verdict(src, field("loyalty_code", "res.partner")),
        "method": _verdict(src, method("action_loyalty", "res.partner")),
        "inherits": _rel_verdict(src, StaleRel(
            id="i", type="INHERITS", start_label="Model", start_name="viin.demo.child",
            end_labels=("Model",), end_name="viin.other.parent")),
    }


def test_an_inherit_constant_imported_from_another_addon_keeps_its_model_members_and_parent(
    tmp_path,
):
    """``_inherit = PARTNER_MODEL`` imported from ``odoo.addons.viin_other``:
    the literal lives in the other addon, so this module's text never names
    ``res.partner`` - yet the class still extends it. Nothing may be proven
    gone: the model, its field, its method and the child's parent are kept."""
    repo = Repo(tmp_path / "r")
    repo.write("models/partner.py", _CROSS_ADDON_INHERIT)
    repo.commit()
    assert _cross_addon_checks(repo.source()) == {
        "model": VERDICT_PRESENT, "field": VERDICT_PRESENT,
        "method": VERDICT_PRESENT, "inherits": VERDICT_PRESENT,
    }


def test_the_cross_addon_constant_test_depends_on_the_unresolved_rule(tmp_path, monkeypatch):
    repo = Repo(tmp_path / "r")
    repo.write("models/partner.py", _CROSS_ADDON_INHERIT)
    repo.commit()
    _drop_unresolved_rule(monkeypatch)
    assert _cross_addon_checks(repo.source()) == {
        "model": VERDICT_GONE, "field": VERDICT_GONE,
        "method": VERDICT_GONE, "inherits": VERDICT_GONE,
    }


_UNEVALUABLE_NAMES = {
    "call": """\
        from odoo import fields, models
        from ..tools import model

        class Char(models.Model):
            _name = model("char")
            size = fields.Integer()
    """,
    "fstring": """\
        from odoo import fields, models
        KIND = "char"

        class Char(models.Model):
            _name = f"viin.demo.{KIND}"
            size = fields.Integer()
    """,
    "attribute": """\
        from odoo import fields, models
        from .. import const

        class Char(models.Model):
            _name = const.CHAR_MODEL
            size = fields.Integer()
    """,
}


@pytest.mark.parametrize("form", sorted(_UNEVALUABLE_NAMES))
def test_a_name_built_at_runtime_keeps_its_model_and_members(tmp_path, monkeypatch, form):
    """``_name = model('char')`` / an f-string / ``const.NAME``: the model
    ``viin.demo.char`` is never spelled in the module, but the class defines
    it. Kept - and, once the rule is dropped, called gone (break proof)."""
    repo = Repo(tmp_path / "r")
    repo.write("tools.py", "def model(kind):\n    return 'viin.demo.' + kind\n")
    repo.write("const.py", "CHAR_MODEL = 'viin.demo.' + 'char'\n")
    repo.write("models/char.py", _UNEVALUABLE_NAMES[form])
    repo.commit()

    def checks():
        src = repo.source()
        return (_verdict(src, model("viin.demo.char")),
                _verdict(src, field("size", "viin.demo.char")))

    assert checks() == (VERDICT_PRESENT, VERDICT_PRESENT)
    _drop_unresolved_rule(monkeypatch)
    assert checks() == (VERDICT_GONE, VERDICT_GONE)


def test_a_constant_of_the_same_addon_is_evaluated_not_treated_as_unknown(tmp_path):
    """Control: a constant the oracle CAN evaluate (same addon) names exactly
    its model - a field the class no longer defines is still proven gone, and
    a model nothing names any more is gone, so the unresolved rule does not
    keep everything in a module that uses constants."""
    repo = Repo(tmp_path / "r")
    repo.write("const.py", "SERVICE = 'viin.demo.service'\n")
    repo.write("models/s.py", """\
        from odoo import fields, models
        from odoo.addons.viin_demo.const import SERVICE

        class Service(models.AbstractModel):
            _name = SERVICE
            _inherit = _name
            kept = fields.Char()
    """)
    repo.commit()
    src = repo.source()
    assert _verdict(src, field("kept", "viin.demo.service")) == VERDICT_PRESENT
    assert _verdict(src, field("dropped", "viin.demo.service")) == VERDICT_GONE
    assert _verdict(src, model("viin.demo.removed")) == VERDICT_GONE


@pytest.mark.parametrize("binder", [
    "from odoo.addons.viin_base.const import *\n",
    "def CHAR_MODEL():\n    return 'viin.base.' + 'char'\n",
], ids=["star-import-of-another-addon", "def"])
def test_a_name_the_file_does_not_assign_is_not_taken_from_another_file(tmp_path, binder):
    """``_name = CHAR_MODEL`` where this file neither assigns nor imports
    CHAR_MODEL from the addon (a star import of another addon, a def): a
    same-named constant elsewhere in the module says nothing about its value,
    so the model is kept rather than judged by that other constant."""
    repo = Repo(tmp_path / "r")
    repo.write("const.py", "CHAR_MODEL = 'viin.demo.other'\n")
    repo.write("models/char.py", "from odoo import fields, models\n" + binder + """
class Char(models.Model):
    _name = CHAR_MODEL
    size = fields.Integer()
""")
    repo.commit()
    src = repo.source()
    assert _verdict(src, model("viin.base.char")) == VERDICT_PRESENT
    assert _verdict(src, field("size", "viin.base.char")) == VERDICT_PRESENT


def test_a_python2_file_with_a_constant_name_keeps_the_models(tmp_path, monkeypatch):
    """A file Python 3 cannot parse has no AST: a non-literal ``_name`` found
    by the text fallback makes the module's models unprovable too."""
    repo = Repo(tmp_path / "r")
    repo.write("models/legacy.py", """\
        from openerp.addons.viin_other.const import LEGACY_MODEL

        class legacy(osv.osv):
            _name = LEGACY_MODEL
            def f(self, cr, uid):
                print 'py2'
    """)
    repo.commit()
    assert _verdict(repo.source(), model("viin.legacy.model")) == VERDICT_PRESENT
    monkeypatch.setattr(pe, "_text_meta_unresolvable", lambda text: False)
    assert _verdict(repo.source(), model("viin.legacy.model")) == VERDICT_GONE


@pytest.mark.parametrize(("rhs", "unresolvable"), [
    ("'viin.a'", False),
    ("['viin.a', \"viin.b\"]  # a comment", False),
    ("(\n        'viin.a',\n        'viin.b',\n    )", False),
    ("u'viin.a'", False),
    ("None", False),
    ("LEGACY", True),
    ("['viin.a',\n        OTHER]", True),
    ("'viin.' + kind", True),
])
def test_the_text_fallback_tells_literal_identities_from_the_rest(rhs, unresolvable):
    text = f"class legacy(osv.osv):\n    _inherit = {rhs}\n"
    assert pe._text_meta_unresolvable(text) is unresolvable


# ---------------------------------------------------------------------------
# Fields and methods are class-scoped
# ---------------------------------------------------------------------------

_TWO_MODELS_BEFORE = """\
    from odoo import fields, models


    class Container(models.Model):
        _name = "viin.container"
        package_type_id = fields.Many2one("stock.package.type")

        def _compute_package(self):
            return True


    class Line(models.Model):
        _inherit = "viin.container.line"
        name = fields.Char()
"""

_TWO_MODELS_AFTER = """\
    from odoo import fields, models


    class Container(models.Model):
        _name = "viin.container"
        name = fields.Char()


    class Line(models.Model):
        _inherit = "viin.container.line"
        package_type_id = fields.Many2one("stock.package.type")

        def _compute_package(self):
            return True
"""


def _moved_verdicts(src):
    return {
        ("field", "viin.container"): _verdict(src, field("package_type_id", "viin.container")),
        ("field", "viin.container.line"):
            _verdict(src, field("package_type_id", "viin.container.line")),
        ("method", "viin.container"): _verdict(src, method("_compute_package", "viin.container")),
        ("method", "viin.container.line"):
            _verdict(src, method("_compute_package", "viin.container.line")),
    }


def test_a_field_moved_to_another_model_of_the_module_is_gone_from_the_first(tmp_path):
    """viin_freight_mixed_container_patch1@17.0: ``package_type_id`` left one
    model for another in the same module. The old model's Field / Method
    nodes are gone - the name still occurs in the module, but no class that
    can define it on that model has it any more; the new model's are present."""
    repo = Repo(tmp_path / "r")
    repo.write("models/container.py", _TWO_MODELS_BEFORE)
    repo.commit()
    repo.write("models/container.py", _TWO_MODELS_AFTER)
    repo.commit("[MOV] package_type_id")
    assert _moved_verdicts(repo.source()) == {
        ("field", "viin.container"): VERDICT_GONE,
        ("field", "viin.container.line"): VERDICT_PRESENT,
        ("method", "viin.container"): VERDICT_GONE,
        ("method", "viin.container.line"): VERDICT_PRESENT,
    }


def test_the_moved_field_test_depends_on_class_scoping(tmp_path, monkeypatch):
    """Break proof: with module-wide presence (any class, the old text rule)
    the moved field is never gone from its first model."""
    repo = Repo(tmp_path / "r")
    repo.write("models/container.py", _TWO_MODELS_AFTER)
    repo.commit()
    monkeypatch.setattr(pe._PyIndex, "member_present", lambda self, *a: True)
    verdicts = _moved_verdicts(repo.source())
    assert verdicts[("field", "viin.container")] == VERDICT_PRESENT
    assert verdicts[("method", "viin.container")] == VERDICT_PRESENT


def test_a_python2_file_falls_back_to_the_text_rule(tmp_path, monkeypatch):
    """A file Python 3 cannot parse (v8-v10 Python 2): no class scope can be
    read, so the field is present wherever its declaration text occurs."""
    repo = Repo(tmp_path / "r")
    repo.write("models/legacy.py", """\
        class legacy_line(osv.osv):
            _name = 'viin.legacy.line'
            _columns = {
                'partner_id': fields.many2one('res.partner', 'Partner'),
            }

            def action_print(self, cr, uid, ids, context=None):
                print "py2 only"
    """)
    repo.commit()
    src = repo.source()
    assert _verdict(src, field("partner_id", "viin.legacy.thing")) == VERDICT_PRESENT
    assert _verdict(src, method("action_print", "viin.legacy.thing")) == VERDICT_PRESENT
    # Break proof: read as if Python could parse it (an empty module), the
    # same nodes would be called gone.
    monkeypatch.setattr(pe, "_parse_py", lambda text: pe.ast.Module(body=[], type_ignores=[]))
    src = repo.source()
    assert _verdict(src, field("partner_id", "viin.legacy.thing")) == VERDICT_GONE


def test_members_inherited_through_python_classes_of_the_module_are_present(tmp_path):
    """Python inheritance carries fields and methods: a field declared on a
    local base class (or a nameless mixin) is defined on every model of the
    module that derives from it, and a nameless class proves nothing."""
    repo = Repo(tmp_path / "r")
    repo.write("models/base.py", """\
        from odoo import fields, models

        class CommonMixin:
            shared_note = fields.Text()

        class Box(models.TransientModel):
            _name = "viin.box"
            amount = fields.Float()

            def run(self):
                return True
    """)
    repo.write("models/box_in.py", """\
        from odoo import models
        from .base import Box, CommonMixin

        class BoxIn(Box, CommonMixin):
            _name = "viin.box.in"
    """)
    repo.write("models/helper.py", "class Loose:\n    orphan_note = fields.Text()\n")
    repo.commit()
    src = repo.source()
    assert _verdict(src, field("amount", "viin.box.in")) == VERDICT_PRESENT
    assert _verdict(src, method("run", "viin.box.in")) == VERDICT_PRESENT
    assert _verdict(src, field("shared_note", "viin.box.in")) == VERDICT_PRESENT
    assert _verdict(src, field("orphan_note", "viin.any")) == VERDICT_PRESENT
    # ... but not on an unrelated model of the module.
    assert _verdict(src, field("amount", "viin.unrelated")) == VERDICT_GONE


@pytest.mark.parametrize(("rel", "text"), [
    ("views/v.xml", '<odoo><record id="view_thing_form" model="ir.ui.view"/></odoo>'),
    ("views/v.xml", '<odoo><record id="viin_demo.view_thing_form" model="ir.ui.view"/></odoo>'),
    ("views/v.xml", '<odoo><template id="view_thing_form"/></odoo>'),
    ("views/v.xml", '<odoo><record id="w1" model="ir.ui.view">'
                    '<field name="key">viin_demo.view_thing_form</field></record></odoo>'),
    ("static/src/x.xml", '<templates><t t-name="viin_demo.view_thing_form"/></templates>'),
    ("data/other_file.xml", '<odoo><record id = "view_thing_form" model="ir.ui.view"/></odoo>'),
], ids=["record-id", "qualified-id", "template", "qweb-key", "t-name", "other-file-spaced"])
def test_every_xmlid_form_counts_as_present(tmp_path, rel, text):
    repo = Repo(tmp_path / "r")
    repo.write(rel, text)
    repo.commit()
    src = repo.source()
    for label in ("View", "QWebTmpl", "Report"):
        node = StaleNode(id=label, label=label, xmlid=f"{MOD}.view_thing_form")
        assert _verdict(src, node) == VERDICT_PRESENT, label


def test_a_view_removed_from_its_file_is_gone(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("views/v.xml", '<odoo><record id="view_kept" model="ir.ui.view"/></odoo>')
    repo.commit()
    node = StaleNode(id="v", label="View", xmlid=f"{MOD}.view_dropped")
    assert _verdict(repo.source(), node) == VERDICT_GONE


def test_non_utf8_text_is_still_searched_and_binary_blobs_are_ignored(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write_bytes("models/latin.py",
                     "# caf\xe9\nclass T:\n    legacy_f = fields.Char()\n".encode("latin-1"))
    repo.write_bytes("static/img.png", b"\x89PNG\x00\x00 gone_f = 1")
    repo.commit()
    src = repo.source()
    assert _verdict(src, field("legacy_f")) == VERDICT_PRESENT
    assert _verdict(src, field("gone_f")) == VERDICT_GONE


def test_translation_catalogues_do_not_keep_a_deleted_entity(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("i18n/vi.po", '#: model:ir.ui.view,arch_db:viin_demo.view_dropped\n'
               '#: model:ir.model,name:model_demo_gone\nmsgid "demo.gone"\nmsgstr ""\n')
    repo.commit()
    src = repo.source()
    assert _verdict(src, StaleNode(id="v", label="View", xmlid=f"{MOD}.view_dropped")) == \
        VERDICT_GONE
    assert _verdict(src, model("demo.gone")) == VERDICT_GONE


# ---------------------------------------------------------------------------
# File-keyed labels, tests, moves, renames
# ---------------------------------------------------------------------------

def test_file_keyed_nodes_follow_their_file_in_the_head_tree(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("static/src/scss/a.scss", ".a { color: red; }\n")
    repo.write("static/src/scss/b.scss", ".b { color: red; }\n")
    repo.write("static/tests/a.test.js", "test('x', () => {});\n")
    repo.commit()
    _git(repo.path, "mv", f"{MOD}/static/src/scss/b.scss", f"{MOD}/static/src/scss/c.scss")
    repo.rm("static/tests/a.test.js")
    repo.commit("[MOV] rename b, drop the js test")
    src = repo.source()

    def node(label, rel):
        return StaleNode(id=rel, label=label, file_path=f"{MOD}/{rel}")

    assert _verdict(src, node("Stylesheet", "static/src/scss/a.scss")) == VERDICT_PRESENT
    assert _verdict(src, node("Stylesheet", "static/src/scss/b.scss")) == VERDICT_GONE
    assert _verdict(src, node("JsTestSuite", "static/tests/a.test.js")) == VERDICT_GONE
    absolute = StaleNode(id="abs", label="Stylesheet",
                         file_path=str(repo.path / MOD / "static/src/scss/a.scss"))
    assert _verdict(src, absolute) == VERDICT_PRESENT


def test_a_path_outside_the_module_is_proven_gone_only_when_head_has_nothing_there(tmp_path):
    """A path outside the module's directory is undecidable (kept) while the
    repo's HEAD has something at it, a submodule could hold it, or it is an
    absolute path outside the repo."""
    sub = Repo(tmp_path / "sub")
    sub.commit()
    repo = Repo(tmp_path / "r")
    repo.write("other_module/static/x.scss", ".x {}\n", module="")
    repo.commit()
    _git(repo.path, "submodule", "add", "-q", str(sub.path), "vendor")
    repo.commit("vendor")
    src = repo.source()
    for fp in ("other_module/static/x.scss", "vendor/viin_demo/static/x.scss",
               "/elsewhere/x.scss", "../escape/x.scss", None):
        node = StaleNode(id=str(fp), label="Stylesheet", file_path=fp)
        assert _verdict(src, node) == VERDICT_PRESENT, fp


@pytest.mark.parametrize("label", ["Stylesheet", "JsTestSuite", "JSPatch", "LintViolation"])
def test_a_file_of_the_copy_in_the_repo_the_module_moved_from_is_gone(tmp_path, label):
    """Cross-repo move: the module came from a repo
    that nested it under ``addons/``; its old copy's file-keyed nodes keep
    that path, which maps into no directory of the module here and which this
    repo's HEAD does not have. No copy this repo ships can produce them, so
    they are gone - kept, they were served as ghosts beside the new copy's
    nodes and reported as parser gaps for ever."""
    repo = Repo(tmp_path / "r")
    repo.write("static/src/scss/rag.scss", ".rag {}\n")
    repo.commit()
    old = StaleNode(id="old", label=label, file_path=f"addons/{MOD}/static/src/scss/rag.scss",
                    target="Panel", xmlid=f"{MOD}.view")
    assert _verdict(repo.source(), old) == VERDICT_GONE


def test_a_test_class_of_the_copy_in_the_repo_the_module_moved_from_is_superseded(tmp_path):
    """The moved copy's test class has a live twin here; the old copy's node
    (old repo's path) is superseded instead of kept beside it."""
    repo = Repo(tmp_path / "r")
    repo.write("tests/test_rag.py", "class TestRag(TransactionCase):\n"
               "    def test_x(self):\n        pass\n")
    repo.commit()
    src = repo.source()
    old = dict(file_path=f"addons/{MOD}/tests/test_rag.py", twin=True)
    assert _verdict(src, StaleNode(id="c", label="TestClass", name="TestRag", **old)) == \
        VERDICT_SUPERSEDED
    assert _verdict(src, StaleNode(id="m", label="TestMethod", name="test_x",
                                   test_class="TestRag", **old)) == VERDICT_SUPERSEDED


def test_a_test_class_moved_to_another_file_is_superseded_by_its_live_twin(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("tests/test_new.py", "class TestThing(TransactionCase):\n    pass\n")
    repo.commit()
    src = repo.source()
    old = dict(id="tc", label="TestClass", name="TestThing", file_path=f"{MOD}/tests/test_old.py")

    assert _verdict(src, StaleNode(**old, twin=True)) == VERDICT_SUPERSEDED
    # No live twin: the class is still defined but the parse lost it -> kept.
    assert _verdict(src, StaleNode(**old)) == VERDICT_PRESENT
    gone = StaleNode(id="g", label="TestClass", name="TestDropped",
                     file_path=f"{MOD}/tests/test_old.py", twin=True)
    assert _verdict(src, gone) == VERDICT_GONE


def test_a_test_method_must_live_in_the_file_that_defines_its_class(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("tests/test_a.py", "class TestA(TransactionCase):\n    def test_kept(self):\n"
               "        pass\n")
    repo.write("tests/test_b.py", "class TestB(TransactionCase):\n    def test_moved(self):\n"
               "        pass\n")
    repo.commit()
    src = repo.source()

    def tm(name, cls):
        return StaleNode(id=name + cls, label="TestMethod", name=name, test_class=cls,
                         file_path=f"{MOD}/tests/test_a.py")

    assert _verdict(src, tm("test_kept", "TestA")) == VERDICT_PRESENT
    assert _verdict(src, tm("test_moved", "TestA")) == VERDICT_GONE
    assert _verdict(src, tm("test_kept", "TestGone")) == VERDICT_GONE


def test_a_js_patch_is_gone_once_its_patch_call_is_removed_even_if_the_target_stays(tmp_path):
    repo = Repo(tmp_path / "r")
    js = "static/src/js/extra.js"
    repo.write(js, "import { patch } from '@web/core/utils/patch';\n"
               "import { Panel } from '@x/panel';\n"
               "patch(Panel.prototype, {\n  setup() {},\n});\n")
    repo.commit()
    node = StaleNode(id="p", label="JSPatch", target="Panel", file_path=f"{MOD}/{js}")
    assert _verdict(repo.source(), node) == VERDICT_PRESENT

    repo.write(js, "import { patch } from '@web/core/utils/patch';\n"
               "import { Panel } from '@x/panel';\n"
               "export class Extra extends Panel {}\n")
    repo.commit()
    assert _verdict(repo.source(), node) == VERDICT_GONE


def test_a_lint_violation_follows_its_file_and_its_re_validated_view(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("views/v.xml", "<odoo/>")
    repo.commit()
    src = repo.source()
    base = dict(label="LintViolation", file_path=f"{MOD}/views/v.xml")

    assert _verdict(src, StaleNode(id="a", **base, parent_live=True)) == VERDICT_SUPERSEDED
    assert _verdict(src, StaleNode(id="b", **base)) == VERDICT_PRESENT
    gone = StaleNode(id="c", label="LintViolation", file_path=f"{MOD}/views/deleted.xml")
    assert _verdict(src, gone) == VERDICT_GONE
    # Its view's record was removed from a file that stays: gone with the view.
    of_removed_view = StaleNode(id="d", **base, xmlid=f"{MOD}.view_removed")
    assert _verdict(src, of_removed_view) == VERDICT_GONE


def test_files_of_a_previous_directory_head_does_not_have_are_gone(tmp_path):
    """Path drift: the module was once indexed from an untracked copy
    (``.odoo-ai/<mod>``); git proves HEAD has nothing there, so the children
    only that copy produced are gone - while a previous directory inside a
    submodule could not be proven absent and stays undecidable."""
    repo = Repo(tmp_path / "r")
    repo.commit()
    (repo.path / ".odoo-ai" / MOD / "views").mkdir(parents=True)
    src = repo.source(previous=[f".odoo-ai/{MOD}"])
    old = StaleNode(id="o", label="LintViolation",
                    file_path=f".odoo-ai/{MOD}/views/v.xml", xmlid=f"{MOD}.v")
    assert _verdict(src, old) == VERDICT_GONE

    sub = Repo(tmp_path / "sub")
    sub.commit()
    _git(repo.path, "submodule", "add", "-q", str(sub.path), "vendor")
    repo.commit("vendor")
    src = repo.source(previous=[f"vendor/{MOD}_old"])
    in_sub = StaleNode(id="s", label="Stylesheet", file_path=f"vendor/{MOD}_old/x.scss")
    assert _verdict(src, in_sub) == VERDICT_PRESENT


def test_a_relationship_into_a_node_the_prune_deletes_goes_with_it(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("views/v.xml", '<odoo><record id="view_kept" model="ir.ui.view"/></odoo>')
    repo.commit()
    lint = StaleNode(id="lv", label="LintViolation", file_path=f"{MOD}/views/gone.xml")
    rel = StaleRel(id="hv", type="HAS_VIOLATION", start_label="View", start_name="view_kept",
                   end_labels=("LintViolation",), end_id="lv")
    result = classify(repo.source(), [lint], [rel])
    assert result.nodes["lv"] == VERDICT_GONE
    assert result.rels["hv"] == VERDICT_SUPERSEDED and result.kept_rels == []


def test_an_unknown_label_is_never_proven_gone(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit()
    assert _verdict(repo.source(), StaleNode(id="x", label="FutureLabel", name="x")) == \
        VERDICT_PRESENT


# ---------------------------------------------------------------------------
# Unavailable source: nothing is proven, everything is kept
# ---------------------------------------------------------------------------

def test_a_gitlink_inside_the_module_makes_every_verdict_present(tmp_path):
    sub = Repo(tmp_path / "sub")
    sub.commit()
    repo = Repo(tmp_path / "r")
    _git(repo.path, "submodule", "add", "-q", str(sub.path), f"{MOD}/vendored")
    repo.commit("vendor")
    src = repo.source()

    assert not src.available and "submodule" in src.reason
    assert _verdict(src, field("anything_at_all")) == VERDICT_PRESENT


def test_a_module_inside_a_submodule_is_judged_by_the_submodule_head(tmp_path):
    sub = Repo(tmp_path / "sub")
    sub.write("models/t.py", "class T:\n    kept = fields.Char()\n")
    sub.commit()
    repo = Repo(tmp_path / "r")
    _git(repo.path, "submodule", "add", "-q", str(sub.path), "vendor")
    repo.commit("vendor the addon repo")
    src = load_module_source(repo.path, MOD, f"vendor/{MOD}")

    assert src.available, src.reason
    assert _verdict(src, field("kept")) == VERDICT_PRESENT
    assert _verdict(src, field("never_there")) == VERDICT_GONE
    assert f"vendor/{MOD}/models/t.py" in src.files


def test_a_module_directory_without_a_committed_manifest_is_unavailable(tmp_path):
    """Untracked module directory, or a tracked one whose manifest is only on
    disk: HEAD does not hold this module, so nothing about it can be proven."""
    repo = Repo(tmp_path / "r")
    repo.commit()
    (repo.path / "untracked_mod").mkdir()
    (repo.path / "untracked_mod" / "__manifest__.py").write_text("{}")
    untracked = load_module_source(repo.path, "untracked_mod", "untracked_mod")
    repo.write("models/x.py", "x = 1\n", module="half_mod")
    repo.commit("only the models of half_mod")
    (repo.path / "half_mod" / "__manifest__.py").write_text("{}")
    half = load_module_source(repo.path, "half_mod", "half_mod")

    for src in (untracked, half):
        assert not src.available
        assert _verdict(src, field("x")) == VERDICT_PRESENT
    assert "manifest" in half.reason


def test_a_directory_outside_git_is_unavailable(tmp_path):
    (tmp_path / "plain" / MOD).mkdir(parents=True)
    src = load_module_source(tmp_path / "plain", MOD, MOD)
    assert not src.available
    assert classify(src, [field("x")], [StaleRel(id="r", type="DEPENDS_ON",
                                                 start_label="Module",
                                                 end_labels=("Module",),
                                                 end_name="sale")]).delete_rel_ids == []


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------

def test_a_dependency_counts_only_in_the_manifest(tmp_path):
    """The parser reads ``depends`` from the manifest alone: a module name
    still imported in Python does not keep a dropped dependency."""
    repo = Repo(tmp_path / "r")
    repo.write("__manifest__.py", repr({"name": "D", "depends": ["base", "mail"]}))
    repo.write("models/t.py", "from odoo.addons.sale.models import x  # noqa\n")
    repo.commit()
    src = repo.source()

    def dep(name):
        return StaleRel(id=name, type="DEPENDS_ON", start_label="Module", start_name=MOD,
                        end_labels=("Module",), end_name=name)

    assert _rel_verdict(src, dep("mail")) == VERDICT_PRESENT
    assert _rel_verdict(src, dep("sale")) == VERDICT_GONE


def test_inheritance_and_delegation_relations_follow_the_python_source(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", """\
        class T(models.Model):
            _name = "demo.thing"
            _inherit = ["mail.thread"]
            partner_id = fields.Many2one("res.partner")
    """)
    repo.commit()
    src = repo.source()

    def rel(rtype, end):
        return StaleRel(id=rtype + end, type=rtype, start_label="Model",
                        start_name="demo.thing", end_labels=("Model",), end_name=end)

    assert _rel_verdict(src, rel("INHERITS", "mail.thread")) == VERDICT_PRESENT
    assert _rel_verdict(src, rel("INHERITS", "ai.mixin")) == VERDICT_GONE
    # res.partner is still referenced, but no _inherits exists any more.
    assert _rel_verdict(src, rel("DELEGATES_TO", "res.partner")) == VERDICT_GONE
    # Same-name extension edge: derived by the post-pass, which re-adds it.
    assert _rel_verdict(src, rel("INHERITS", "demo.thing")) == VERDICT_SUPERSEDED


def test_method_relations_are_judged_inside_the_method_and_its_decorators(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", """\
        class T(models.Model):
            _name = "demo.thing"
            name = fields.Char()
            assistant_id = fields.Many2one("res.users")

            @api.depends("name")
            def _compute_label(self):
                for rec in self:
                    rec.label = rec.name

            def action_other(self):
                return self.assistant_id.name_get()
    """)
    repo.commit()
    src = repo.source()

    def rel(rtype, end, qn=None):
        return StaleRel(id=rtype + end, type=rtype, start_label="Method",
                        start_name="_compute_label", end_labels=("Field",),
                        end_name=end, end_qualified_name=qn)

    assert _rel_verdict(src, rel("DEPENDS_ON_FIELD", "name")) == VERDICT_PRESENT
    # assistant_id is a live field and used by ANOTHER method: still gone here.
    assert _rel_verdict(src, rel("DEPENDS_ON_FIELD", "assistant_id")) == VERDICT_GONE
    assert _rel_verdict(src, rel("USES_FIELD", "assistant_id")) == VERDICT_GONE
    assert _rel_verdict(
        src, rel("USES_CORE_SYMBOL", "x", "odoo.models.BaseModel.name_get"),
    ) == VERDICT_GONE


def test_an_extension_naming_a_record_form_qweb_view_by_its_record_id_is_present(tmp_path):
    """The writer links ``inherit_id ref="website_forum.default_faq"`` to the
    QWebTmpl keyed ``website_forum.faq_accordion`` (its ``record_xmlid`` is the
    record id). The source names only the record id, so the oracle must judge
    the edge by it too - never 'gone' while the extension is still declared."""
    repo = Repo(tmp_path / "r")
    repo.write("views/t.xml", """\
        <odoo>
            <template id="faq_ext" inherit_id="website_forum.default_faq">
                <xpath expr="//div" position="inside"><span/></xpath>
            </template>
        </odoo>
    """)
    repo.commit()
    src = repo.source()

    def ext(record_xmlid):
        return StaleRel(id="e", type="EXTENDS_TMPL", start_label="QWebTmpl",
                        start_name=f"{MOD}.faq_ext", end_labels=("QWebTmpl",),
                        end_xmlid="website_forum.faq_accordion",
                        end_record_xmlid=record_xmlid)

    assert _rel_verdict(src, ext("website_forum.default_faq")) == VERDICT_PRESENT
    # Break proof: judged by the key alone (no record id read back), the
    # declared extension would be called gone.
    assert _rel_verdict(src, ext(None)) == VERDICT_GONE
    # The extension removed by a commit is gone by either identity.
    repo.write("views/t.xml", "<odoo/>\n")
    repo.commit("[REM] faq_ext")
    assert _rel_verdict(repo.source(), ext("website_forum.default_faq")) == VERDICT_GONE


def test_view_and_bundle_relations_match_whole_xmlids(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("views/v.xml", """\
        <odoo>
          <record id="ext" model="ir.ui.view">
            <field name="inherit_id" ref="base.view_partner_form_alt"/>
          </record>
          <template id="assets_x" inherit_id="web.assets_backend"/>
        </odoo>
    """)
    repo.commit()
    src = repo.source()

    def rel(rtype, xmlid=None, name=None, labels=("View",)):
        return StaleRel(id=rtype + str(xmlid or name), type=rtype, start_label="View",
                        start_name="ext", end_labels=labels, end_xmlid=xmlid, end_name=name)

    assert _rel_verdict(src, rel("INHERITS_VIEW", "base.view_partner_form")) == VERDICT_GONE
    assert _rel_verdict(src, rel("INHERITS_VIEW", "base.view_partner_form_alt")) == \
        VERDICT_PRESENT
    bundle = ("AssetBundle",)
    assert _rel_verdict(src, rel("CONTRIBUTES_TO", name="web.assets_backend",
                                 labels=bundle)) == VERDICT_PRESENT
    assert _rel_verdict(src, rel("CONTRIBUTES_TO", name="web.assets_frontend",
                                 labels=bundle)) == VERDICT_GONE


def test_a_stylesheet_import_is_judged_in_the_importing_file(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.write("static/src/scss/extra.scss", '@import "./_vars.scss";\n.o_x { padding: 0; }\n')
    repo.write("static/src/scss/other.scss", "// mentions colors here\n")
    repo.commit()
    src = repo.source()

    def imp(target):
        return StaleRel(id=target, type="IMPORTS", start_label="Stylesheet",
                        start_file=f"{MOD}/static/src/scss/extra.scss",
                        end_labels=("Stylesheet",), end_file=f"{MOD}/static/src/scss/{target}")

    assert _rel_verdict(src, imp("_vars.scss")) == VERDICT_PRESENT
    assert _rel_verdict(src, imp("_colors.scss")) == VERDICT_GONE


def test_an_unknown_relationship_end_is_never_proven_gone(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit()
    rel = StaleRel(id="x", type="HAS_VIOLATION", start_label="View", start_name="v",
                   end_labels=("LintViolation",))
    assert _rel_verdict(repo.source(), rel) == VERDICT_PRESENT


# ---------------------------------------------------------------------------
# Fingerprint (the re-evaluation key of a kept set)
# ---------------------------------------------------------------------------

def test_the_tree_fingerprint_moves_with_the_module_only(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit()
    before = pe.module_tree_fingerprint(repo.path, [MOD])
    repo.write("README.md", "outside the module\n", module="")
    repo.commit()
    assert pe.module_tree_fingerprint(repo.path, [MOD]) == before
    repo.write("models/t.py", "x = 1\n")
    repo.commit()
    assert pe.module_tree_fingerprint(repo.path, [MOD]) != before
    assert pe.module_tree_fingerprint(tmp_path / "nowhere", [MOD]) == f"{MOD}/=?"


def test_the_code_token_names_the_parsers_and_the_oracle():
    token = pe.prune_code_token()
    assert token.startswith("src-") and len(token) == 20
    assert "prune_evidence.py" in pe._PRUNE_CODE_SOURCES


# ---------------------------------------------------------------------------
# Property: everything the REAL parsers produce is "present"
# ---------------------------------------------------------------------------

_ODD_PY = '''\
# -*- coding: utf-8 -*-
from odoo import api, fields, models
from ..const import SERVICE_NAME

SECOND = "viin.demo.second"


class Base(models.AbstractModel):
    _name = 'viin.demo.base'
    _description = "Base"

    code = fields.Char()


class Thing(models.Model):
    _name = "viin.demo.thing"
    _inherit = ["mail.thread", 'viin.demo.base']
    _inherits = {"res.partner": "partner_id"}

    partner_id = fields.Many2one(
        "res.partner",
        required=True,
    )
    annotated: fields.Char = fields.Char()
    tags = fields.Many2many(comodel_name="res.partner.category")
    (paren) = fields.Integer()
    cont \\
        = fields.Float()

    @api.depends(
        "partner_id.name",
        "code",
    )
    def _compute_label(self):
        for rec in self:
            rec.label = self.code or self.partner_id.name

    @api.model
    def create(self, vals):
        return super().create(vals)

    def action_go(self):
        return self.env["viin.demo.base"].search([]).name_get()


class Extension(models.Model):
    _inherit = 'viin.demo.thing'

    extra = fields.Boolean()


class Local(Base):
    _name = SECOND
    _inherit = 'viin.demo.base'

    local_field = fields.Char()


class FromConstant(models.Model):
    _name = SERVICE_NAME
'''

_ODD_XML = '''\
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <record id="view_thing_form" model="ir.ui.view">
        <field name="name">thing.form</field>
        <field name="model">viin.demo.thing</field>
        <field name="arch" type="xml">
            <form><field name="code"/></form>
        </field>
    </record>
    <record id = "view_thing_form_ext" model="ir.ui.view">
        <field name="model">viin.demo.thing</field>
        <field name="inherit_id" ref="view_thing_form"/>
        <field name="arch" type="xml">
            <xpath expr="//field[@name='code']" position="after"><field name="extra"/></xpath>
        </field>
    </record>
    <record id="base.view_partner_form_override" model="ir.ui.view">
        <field name="model">res.partner</field>
        <field name="inherit_id" ref="base.view_partner_form"/>
        <field name="arch" type="xml"><form/></field>
    </record>
    <template id="report_thing_document">
        <div class="page"><span t-field="o.code"/></div>
    </template>
    <template id="report_thing_document_ext" inherit_id="viin_demo.report_thing_document">
        <xpath expr="//div" position="inside"><span>x</span></xpath>
    </template>
    <record id="web_key_tmpl" model="ir.ui.view">
        <field name="type">qweb</field>
        <field name="key">viin_demo.keyed_template</field>
        <field name="arch" type="xml"><t t-name="viin_demo.keyed_template"/></field>
    </record>
    <record id="action_report_thing" model="ir.actions.report">
        <field name="name">Thing</field>
        <field name="model">viin.demo.thing</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">viin_demo.report_thing_document</field>
    </record>
</odoo>
'''

_ODD_JS = '''\
/** @odoo-module */
import { patch } from "@web/core/utils/patch";
import { Component } from "@odoo/owl";
import { FormController } from "@web/views/form/form_controller";

export class ThingPanel extends Component {
    static template = "viin_demo.ThingPanel";
    setup() {
        this.orm.searchRead("viin.demo.thing", [], ["code"]);
    }
}

patch(FormController.prototype, {
    setup() {
        super.setup();
    },
});

patch(ThingPanel, "viin_demo.static", {});
'''

_ODD_TEST = '''\
from odoo.tests import TransactionCase, tagged


class ThingCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.thing = cls.env["viin.demo.thing"].create({"code": "x"})


@tagged("post_install", "-at_install")
class TestThing(ThingCommon):

    def test_label(self):
        self.assertTrue(self.thing.code)

    def helper(self):
        return 1
'''


def _odd_module(repo: Repo) -> None:
    repo.write("__manifest__.py", repr({
        "name": "Demo", "version": "17.0.1.0.0", "depends": ["base", "mail", "web"],
        "data": ["views/views.xml"], "license": "LGPL-3",
        "assets": {"web.assets_backend": [f"{MOD}/static/src/**/*"]},
    }))
    repo.write("__init__.py", "from . import models\n")
    repo.write("const.py", "SERVICE_NAME = 'viin.demo.service'\n")
    repo.write("models/__init__.py", "from . import thing\n")
    repo.write("models/thing.py", _ODD_PY)
    repo.write("views/views.xml", _ODD_XML)
    repo.write("static/src/js/thing_panel.js", _ODD_JS)
    repo.write("static/src/scss/_vars.scss", "$gap: 4px;\n")
    repo.write("static/src/scss/thing.scss", '@import "vars";\n.o_thing { padding: $gap; }\n')
    repo.write("static/src/css/plain.css", ".o_plain { color: red; }\n")
    repo.write("static/tests/thing_panel.test.js",
               'import { test } from "@odoo/hoot";\ntest("renders", async () => {});\n')
    repo.write("tests/__init__.py", "from . import test_thing\n")
    repo.write("tests/test_thing.py", _ODD_TEST)


def _info(repo: Repo, version: str, depends=("base", "mail", "web")):
    from src.indexer.models import ModuleInfo
    return ModuleInfo(
        name=MOD, odoo_version=version, repo=repo.path.name,
        path=str(repo.path / MOD), depends=list(depends), repo_root=repo.path,
    )


def _parsed_items(info, module: str = MOD):
    """Every node and relationship the REAL parsers produce for *info*, in the
    shape the prune reads them back (stale identities)."""
    from src.indexer import (
        parser_assets,
        parser_css,
        parser_js,
        parser_js_test,
        parser_python,
        parser_qweb,
        parser_scss,
        parser_test,
        parser_xml,
    )
    from src.indexer.models import to_repo_relative

    nodes: list[StaleNode] = []
    rels: list[StaleRel] = []

    def rel(rtype, start_label, start_name=None, **end):
        rels.append(StaleRel(id=f"r{len(rels)}", type=rtype, start_label=start_label,
                             start_name=start_name, **end))

    def node(label, **props):
        nodes.append(StaleNode(id=f"n{len(nodes)}", label=label, **props))

    for dep in info.depends:
        rel("DEPENDS_ON", "Module", module, end_labels=("Module",), end_name=dep)
    py = parser_python.parse_module(info)
    for m in py.models:
        node("Model", name=m.name)
        for parent in m.inherit:
            rel("INHERITS", "Model", m.name, end_labels=("Model",), end_name=parent)
        for parent in m.inherits:
            rel("DELEGATES_TO", "Model", m.name, end_labels=("Model",), end_name=parent)
        for f in m.fields:
            node("Field", name=f.name, model=m.name)
            rel("BELONGS_TO", "Field", f.name, end_labels=("Model",), end_name=m.name)
        for meth in m.methods:
            node("Method", name=meth.name, model=m.name)
            for dep in meth.depends:
                for seg in dep.split("."):
                    rel("DEPENDS_ON_FIELD", "Method", meth.name, end_labels=("Field",),
                        end_name=seg)
            for ref in meth.field_refs:
                rel("USES_FIELD", "Method", meth.name, end_labels=("Field",), end_name=ref)
            for qn in meth.core_symbol_refs:
                rel("USES_CORE_SYMBOL", "Method", meth.name, end_labels=("CoreSymbol",),
                    end_qualified_name=qn)
    views = parser_xml.parse_module(info)
    for v in views.views:
        if v.xmlid.split(".")[0] != module:
            continue  # foreign-namespace records are never this module's to prune
        node("View", xmlid=v.xmlid)
        if v.model:
            rel("TARGETS_MODEL", "View", v.xmlid, end_labels=("Model",), end_name=v.model)
        if v.inherit_xmlid:
            rel("INHERITS_VIEW", "View", v.xmlid, end_labels=("View",),
                end_xmlid=v.inherit_xmlid)
    for r in views.reports:
        node("Report", xmlid=r.xmlid)
        rel("REPORTS_ON", "Report", r.xmlid, end_labels=("Model",), end_name=r.model)
        if r.report_name:
            rel("USES_TEMPLATE", "Report", r.xmlid, end_labels=("QWebTmpl",),
                end_xmlid=r.report_name)
    for q in parser_qweb.parse_module(info).qweb:
        if q.xmlid.split(".")[0] != module:
            continue
        node("QWebTmpl", xmlid=q.xmlid)
        if q.inherit_xmlid:
            rel("EXTENDS_TMPL", "QWebTmpl", q.xmlid, end_labels=("QWebTmpl",),
                end_xmlid=q.inherit_xmlid)
    graph = parser_js.parse_module_graph(info)
    for p in graph.patches:
        node("JSPatch", target=p.target, file_path=p.file_path)
        rel("PATCHES", "JSPatch", p.patch_name, end_labels=("OWLComp",), end_name=p.target)
    for c in graph.components:
        node("OWLComp", name=c.name, file_path=c.file_path)
    sheets = parser_css.parse_module(info)[1] + parser_scss.parse_module(info)[1]
    for s in sheets:
        fp = to_repo_relative(s.file_path, info.repo_root)
        node("Stylesheet", file_path=fp)
        for target in s.imports:
            rel("IMPORTS", "Stylesheet", start_file=fp, end_labels=("Stylesheet",),
                end_file=to_repo_relative(target, info.repo_root))
    for suite in parser_js_test.parse_module_js_tests(info):
        node("JsTestSuite", file_path=suite.file_path)
    for tc in parser_test.parse_module(info).test_classes:
        node("TestClass", name=tc.name, file_path=tc.file_path)
        node("TestHelper", name=tc.name)
        for tm in tc.methods:
            node("TestMethod", name=tm.name, test_class=tc.name, file_path=tm.file_path)
            rel("BELONGS_TO_TEST", "TestMethod", tm.name, end_labels=("TestClass",),
                end_name=tc.name)
    for contribution in parser_assets.parse_assets(info).contributions:
        rel("CONTRIBUTES_TO", "Module", module, end_labels=("AssetBundle",),
            end_name=contribution.bundle_name)
    return nodes, rels


def _assert_nothing_produced_is_gone(src, nodes, rels):
    result = classify(src, nodes, rels)
    gone_nodes = [n.describe() for n in nodes if result.nodes[n.id] == VERDICT_GONE]
    gone_rels = [r.describe() for r in rels if result.rels[r.id] == VERDICT_GONE]
    assert gone_nodes == [] and gone_rels == [], (
        "the oracle called entities the parser still produces 'gone' - the pattern "
        f"for them is too narrow: nodes={gone_nodes} rels={gone_rels}"
    )


def test_everything_the_real_parsers_produce_is_present(tmp_path):
    repo = Repo(tmp_path / "r")
    _odd_module(repo)
    repo.commit()
    info = _info(repo, "17.0")
    nodes, rels = _parsed_items(info)
    labels = {n.label for n in nodes}
    rel_types = {r.type for r in rels}

    # Positive control: the fixture really exercises every family, and the
    # parser reads the constant identities (same-file SECOND, same-addon
    # SERVICE_NAME) the oracle has to evaluate too.
    assert {"viin.demo.second", "viin.demo.service"} <= {
        n.name for n in nodes if n.label == "Model"
    }
    assert {"Model", "Field", "Method", "View", "Report", "QWebTmpl", "JSPatch",
            "OWLComp", "Stylesheet", "JsTestSuite", "TestClass", "TestMethod"} <= labels, labels
    assert {"DEPENDS_ON", "INHERITS", "DELEGATES_TO", "DEPENDS_ON_FIELD", "USES_FIELD",
            "INHERITS_VIEW", "TARGETS_MODEL", "REPORTS_ON", "USES_TEMPLATE",
            "EXTENDS_TMPL", "PATCHES", "IMPORTS", "CONTRIBUTES_TO"} <= rel_types, rel_types
    _assert_nothing_produced_is_gone(repo.source(), nodes, rels)


def test_the_property_check_catches_a_too_narrow_pattern(tmp_path, monkeypatch):
    """Break-proof of the property test: with a Field rule that only accepts
    ``name = fields.`` at the start of a line, the annotated / parenthesized /
    continued declarations of the fixture are called gone - and the property
    assertion fails."""
    repo = Repo(tmp_path / "r")
    _odd_module(repo)
    repo.commit()
    nodes, rels = _parsed_items(_info(repo, "17.0"))
    import re
    monkeypatch.setattr(pe, "_field_pattern",
                        lambda n: re.compile(rf"^[ \t]*{re.escape(n)} = fields\.", re.M))
    with pytest.raises(AssertionError, match="too narrow"):
        _assert_nothing_produced_is_gone(repo.source(), nodes, rels)


_ERA1_PY = '''\
# -*- coding: utf-8 -*-
from openerp.osv import fields, osv


class legacy_thing(osv.osv):
    _name = 'viin.legacy.thing'
    _inherit = ['mail.thread']
    _columns = {
        'name': fields.char('Name', size=64),
        'partner_id'
            : fields.many2one('res.partner', 'Partner'),
        'amount': fields.function(lambda s, *a: {}, type='float', string='A'),
    }

    def action_print(self, cr, uid, ids, context=None):
        print "py2 only"
        return True

legacy_thing()


class legacy_ext(osv.osv):
    _inherit = 'viin.legacy.thing'
    _columns = {}
    _columns.update({
        'extra': fields.boolean('Extra'),
    })
'''


def test_everything_the_real_era1_parser_produces_is_present(tmp_path):
    """v8 Python 2 source (text-regex fallback path of the parser)."""
    repo = Repo(tmp_path / "r")
    repo.write("__openerp__.py", repr({"name": "Legacy", "depends": ["mail"]}))
    repo.rm("__manifest__.py")
    repo.write("models/legacy.py", _ERA1_PY)
    repo.commit()
    info = _info(repo, "8.0", depends=["mail"])
    nodes, rels = _parsed_items(info)
    assert {("Field", "partner_id"), ("Field", "extra"), ("Method", "action_print")} <= {
        (n.label, n.name) for n in nodes
    }, [(n.label, n.name) for n in nodes]
    _assert_nothing_produced_is_gone(repo.source(), nodes, rels)


def test_the_parsed_fixture_minus_a_commit_is_gone_exactly_where_git_removed_it(tmp_path):
    """The other direction on the same fixture: after a commit removes one
    field, one method, one view and the test file, exactly those are gone."""
    repo = Repo(tmp_path / "r")
    _odd_module(repo)
    repo.commit()
    nodes, _rels = _parsed_items(_info(repo, "17.0"))
    repo.write("models/thing.py", _ODD_PY.replace("    extra = fields.Boolean()\n", "")
               .replace("    def action_go(self):\n", "    def action_went(self):\n"))
    repo.write("views/views.xml", _ODD_XML.replace('id = "view_thing_form_ext"',
                                                   'id = "view_renamed_ext"'))
    repo.rm("tests/test_thing.py")
    repo.commit("[REM] some entities")
    result = classify(repo.source(), nodes)
    gone = sorted(
        (n.label, n.name or n.xmlid) for n in nodes if result.nodes[n.id] == VERDICT_GONE
    )
    assert gone == sorted([
        ("Field", "extra"), ("Method", "action_go"),
        ("View", f"{MOD}.view_thing_form_ext"),
        ("TestClass", "TestThing"), ("TestClass", "ThingCommon"),
        ("TestHelper", "TestThing"), ("TestHelper", "ThingCommon"),
        ("TestMethod", "helper"), ("TestMethod", "setUpClass"), ("TestMethod", "test_label"),
    ]), gone


# ---------------------------------------------------------------------------
# Root modules, twins, re-pointed edges, scope, blobs
# ---------------------------------------------------------------------------

def test_a_module_at_the_repository_root_is_judged_like_any_other(tmp_path):
    """A single-module repo keeps its manifest at the root: the module's
    directory is the repo root (``""`` / ``"."``). Its deleted entities must be
    provable too, not kept for ever as an 'unavailable' parser gap."""
    root = tmp_path / "rootmod"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "__manifest__.py").write_text(repr({"name": "Root", "depends": ["base"]}))
    (root / "models.py").write_text(
        "class T(models.Model):\n    _name = 'root.thing'\n    kept = fields.Char()\n"
        "    dropped = fields.Char()\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    (root / "models.py").write_text(
        "class T(models.Model):\n    _name = 'root.thing'\n    kept = fields.Char()\n")
    _git(root, "commit", "-q", "-am", "[REM] dropped")

    for module_dir in ("", "."):
        src = load_module_source(root, "rootmod", module_dir)
        assert src.available, (module_dir, src.reason)
        assert _verdict(src, field("dropped", "root.thing")) == VERDICT_GONE
        assert _verdict(src, field("kept", "root.thing")) == VERDICT_PRESENT


def _two_same_name_test_classes(repo: Repo) -> None:
    for rel in ("tests/test_a.py", "tests/test_b.py"):
        repo.write(rel, "class TestCommon(TransactionCase):\n"
                        "    def test_x(self):\n        pass\n")


def test_a_same_name_test_class_still_in_its_own_file_is_kept_beside_its_twin(tmp_path):
    """sale v17 defines two TestSaleCommon: one module may hold two classes of
    one name in two files. A parser gap that loses the one in test_b.py while
    test_a.py's is re-written must not delete test_b.py's node - it is still
    defined in its own file. Once test_b.py no longer defines it, the twin in
    test_a.py replaces it."""
    repo = Repo(tmp_path / "r")
    _two_same_name_test_classes(repo)
    repo.commit()
    src = repo.source()
    tc = StaleNode(id="tc", label="TestClass", name="TestCommon",
                   file_path=f"{MOD}/tests/test_b.py", twin=True)
    tm = StaleNode(id="tm", label="TestMethod", name="test_x", test_class="TestCommon",
                   file_path=f"{MOD}/tests/test_b.py", twin=True)
    assert _verdict(src, tc) == VERDICT_PRESENT
    assert _verdict(src, tm) == VERDICT_PRESENT

    repo.write("tests/test_b.py", "class TestOther(TransactionCase):\n    pass\n")
    repo.commit("[MOV] TestCommon lives in test_a.py only")
    src = repo.source()
    assert _verdict(src, tc) == VERDICT_SUPERSEDED
    assert _verdict(src, tm) == VERDICT_SUPERSEDED


def test_a_re_pointed_relationship_is_superseded_by_its_fresh_twin(tmp_path):
    """The writers pick ONE of several same-identity end nodes by ranking
    (INHERITS / DELEGATES_TO / REPORTS_ON: one Model node per module of a name;
    PATCHES: one OWLComp; USES_CORE_SYMBOL: a CoreSymbol). When the winner
    changes, the run writes E -> winner2 and E -> winner1 goes stale although
    the source still names the target: the old edge is superseded, never kept
    as a parser gap (two parents for one ``_inherit`` would inflate chains)."""
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", """\
        class T(models.Model):
            _name = "demo.thing"
            _inherit = ["some.mixin"]
            _inherits = {"res.partner": "partner_id"}
            partner_id = fields.Many2one("res.partner")

            def action(self):
                return self.env["x"].old_api()
    """)
    repo.write("static/src/patch.js",
               "patch(FormController.prototype, { setup() {} });\n")
    repo.write("report/r.xml", '<record id="report_x" model="ir.actions.report">'
               '<field name="model">demo.thing</field></record>\n')
    repo.commit()
    src = repo.source()

    rels = {
        "INHERITS": dict(start_label="Model", start_name="demo.thing",
                         end_labels=("Model",), end_name="some.mixin"),
        "DELEGATES_TO": dict(start_label="Model", start_name="demo.thing",
                             end_labels=("Model",), end_name="res.partner"),
        "REPORTS_ON": dict(start_label="Report", start_name=None,
                           end_labels=("Model",), end_name="demo.thing"),
        "PATCHES": dict(start_label="JSPatch", start_name="FormController",
                        start_file=f"{MOD}/static/src/patch.js",
                        end_labels=("OWLComp",), end_name="FormController"),
        "USES_CORE_SYMBOL": dict(start_label="Method", start_name="action",
                                 end_labels=("CoreSymbol",),
                                 end_qualified_name="odoo.models.old_api",
                                 end_status="deprecated"),
    }
    for rtype, kw in rels.items():
        stale = StaleRel(id=rtype, type=rtype, **kw)
        assert _rel_verdict(src, stale) == VERDICT_PRESENT, (
            f"{rtype}: without a fresh twin the source still names it - kept")
        twin = StaleRel(id=rtype, type=rtype, twin=True, **kw)
        assert _rel_verdict(src, twin) == VERDICT_SUPERSEDED, rtype


def test_a_core_symbol_link_to_a_symbol_no_longer_deprecated_is_superseded(tmp_path):
    """USES_CORE_SYMBOL is written only towards a ``deprecated`` CoreSymbol: a
    curated status change can never be re-linked, whatever the method says."""
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", "class T(models.Model):\n    _name = 'demo.thing'\n"
               "    def action(self):\n        return self.old_api()\n")
    repo.commit()
    src = repo.source()

    def link(status):
        return StaleRel(id=str(status), type="USES_CORE_SYMBOL", start_label="Method",
                        start_name="action", end_labels=("CoreSymbol",),
                        end_qualified_name="odoo.models.old_api", end_status=status)

    assert _rel_verdict(src, link("active")) == VERDICT_SUPERSEDED
    assert _rel_verdict(src, link("deprecated")) == VERDICT_PRESENT
    assert _rel_verdict(src, link(None)) == VERDICT_PRESENT, "an unknown status proves nothing"


def test_a_class_body_name_also_bound_outside_counts_both_values(tmp_path):
    """``X = 'b.model'`` then ``class A: _name = X; X = 'a.model'``: Python (and
    the parser) give model b.model - a class body reads the outer X until it
    binds its own. The oracle must not narrow it to {'a.model'} and call
    Field foo@b.model gone."""
    repo = Repo(tmp_path / "r")
    repo.write("models/t.py", """\
        X = "b.model"

        class A(models.Model):
            _name = X
            X = "a.model"
            foo = fields.Char()
    """)
    repo.commit()
    src = repo.source()
    assert _verdict(src, field("foo", "b.model")) == VERDICT_PRESENT
    assert _verdict(src, field("foo", "a.model")) == VERDICT_PRESENT
    assert _verdict(src, field("foo", "c.model")) == VERDICT_GONE, "still class-scoped"


def test_binary_formats_are_never_loaded_and_a_huge_text_blob_proves_nothing(
    tmp_path, monkeypatch,
):
    """A PDF without a NUL in its first bytes used to be read as text (its words
    kept ghosts alive); binary formats are skipped by suffix now. A text blob
    above the cap is not loaded, and since skipping its text could turn what
    it defines into a wrong 'gone', the source is unavailable instead."""
    repo = Repo(tmp_path / "r")
    repo.write_bytes("static/doc.pdf", b"%PDF-1.4 ghost_f = fields.Char()\n")
    repo.write("models/t.py", "class T(models.Model):\n    _name = 'demo.thing'\n"
               "    kept = fields.Char()\n")
    repo.commit()
    src = repo.source()
    assert src.available and f"{MOD}/static/doc.pdf" not in src.files
    assert _verdict(src, field("ghost_f")) == VERDICT_GONE

    monkeypatch.setattr(pe, "_MAX_BLOB_BYTES", 40)
    big = repo.source()
    assert not big.available and "larger than" in (big.reason or "")
    assert _verdict(big, field("ghost_f")) == VERDICT_PRESENT


def test_the_code_token_covers_every_file_that_changes_the_parse_or_the_verdicts(
    monkeypatch, tmp_path,
):
    """A fix shipped only in the version dispatch, the xmlid qualification, the
    curated framework bases or the curated CoreSymbol data must re-open the
    recorded parser gaps once, like a parser fix."""
    for name in ("version_registry.py", "_xmlid.py", "framework_bases.py"):
        assert name in pe._PRUNE_CODE_SOURCES, name
    here = Path(pe.__file__).resolve().parent
    assert any(here.glob("spec_data/tools_symbols_*.json")), "precondition: curated data"
    fake = tmp_path / "indexer"
    (fake / "spec_data").mkdir(parents=True)
    for p in here.glob("*.py"):
        (fake / p.name).write_bytes(p.read_bytes())
    for pattern in ("tools_symbols_*.json", "lint_rules_*.json"):
        for p in here.glob(f"spec_data/{pattern}"):
            (fake / "spec_data" / p.name).write_bytes(p.read_bytes())

    def token_of(directory: Path) -> str:
        monkeypatch.setattr(pe, "_CODE_TOKEN", None)
        monkeypatch.setattr(pe, "__file__", str(directory / "prune_evidence.py"))
        return pe.prune_code_token()

    base = token_of(fake)
    assert token_of(fake) == base, "same files, same token"
    data = sorted((fake / "spec_data").glob("tools_symbols_*.json"))[0]
    original = data.read_bytes()
    data.write_bytes(original + b"\n")
    assert token_of(fake) != base, "a curated CoreSymbol data change re-opens the gaps"
    data.write_bytes(original)
    assert token_of(fake) == base
    dispatch = fake / "version_registry.py"
    dispatch.write_bytes(dispatch.read_bytes() + b"\n# changed\n")
    assert token_of(fake) != base, "a version-dispatch change re-opens the gaps"
