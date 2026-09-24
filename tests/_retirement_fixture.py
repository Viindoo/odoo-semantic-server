# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared on-disk fixture for the module-retirement cascade tests (ADR-0056 B6).

Real case: ``viin_ai_rag`` was merged into ``viin_ai`` and then deleted, so the
index must retire ``viin_ai_rag`` and everything it wrote while ``viin_ai``
survives. The fixture builds BOTH modules on disk, with ``viin_ai_rag`` shipping
one artifact of every kind the indexer writes for a module (Python models /
fields / methods, form + list + inherited views, a RelaxNG-invalid list view
that yields a LintViolation, a QWeb template, a report action, a JS patch, an
OWL component, an SCSS stylesheet, an ``assets`` bundle contribution, a Hoot JS
test suite, Python tests with an addon ``tests/common.py`` helper base).

It is driven through the REAL ``_index_repo`` + ``reconcile_test_surface`` so the
set of labels attached to the module is whatever the production writers
actually produce - never a copy of ``MODULE_CHILD_LABELS``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from tests.conftest import TEST_VERSION

RNG_DIR = Path(__file__).parent / "fixtures" / "rng"

RETIRED = "viin_ai_rag"
SURVIVOR = "viin_ai"

_MAJOR = TEST_VERSION.split(".")[0]


def _manifest(name: str, depends: list[str], assets: dict | None = None) -> str:
    body = {
        "name": name,
        "version": f"{_MAJOR}.1.0",
        "depends": depends,
        "installable": True,
        "license": "LGPL-3",
    }
    if assets:
        body["assets"] = assets
    return repr(body) + "\n"


_VIIN_AI_MODELS = '''\
from odoo import fields, models


class AiAssistant(models.Model):
    _name = "ai.assistant"
    _description = "AI Assistant"

    name = fields.Char()

    def action_ask(self):
        return True
'''

_VIIN_AI_VIEWS = """\
<?xml version="1.0"?>
<odoo>
    <record id="ai_assistant_view_form" model="ir.ui.view">
        <field name="name">ai.assistant.form</field>
        <field name="model">ai.assistant</field>
        <field name="arch" type="xml">
            <form><sheet><field name="name"/></sheet></form>
        </field>
    </record>
</odoo>
"""

_RAG_MODELS = '''\
from odoo import api, fields, models


class AiRagSource(models.Model):
    _name = "ai.rag.source"
    _description = "RAG Source"

    name = fields.Char()
    assistant_id = fields.Many2one("ai.assistant")

    def action_reindex(self):
        return True


class AiAssistant(models.Model):
    _inherit = "ai.assistant"

    rag_source_ids = fields.One2many("ai.rag.source", "assistant_id")
    rag_count = fields.Integer(compute="_compute_rag_count")

    @api.depends("rag_source_ids")
    def _compute_rag_count(self):
        for rec in self:
            rec.rag_count = len(rec.rag_source_ids)
'''

_RAG_VIEWS = f"""\
<?xml version="1.0"?>
<odoo>
    <record id="ai_rag_source_view_form" model="ir.ui.view">
        <field name="name">ai.rag.source.form</field>
        <field name="model">ai.rag.source</field>
        <field name="arch" type="xml">
            <form><sheet><field name="name"/></sheet></form>
        </field>
    </record>
    <record id="ai_rag_source_view_list" model="ir.ui.view">
        <field name="name">ai.rag.source.list</field>
        <field name="model">ai.rag.source</field>
        <field name="arch" type="xml">
            <list>
                <badtag foo="bar"/>
            </list>
        </field>
    </record>
    <record id="ai_assistant_view_form_rag" model="ir.ui.view">
        <field name="name">ai.assistant.form.rag</field>
        <field name="model">ai.assistant</field>
        <field name="inherit_id" ref="{SURVIVOR}.ai_assistant_view_form"/>
        <field name="arch" type="xml">
            <xpath expr="//field[@name='name']" position="after">
                <field name="rag_source_ids"/>
            </xpath>
        </field>
    </record>
</odoo>
"""

_RAG_REPORT = """\
<?xml version="1.0"?>
<odoo>
    <template id="report_rag_source_document">
        <t t-call="web.html_container">
            <div class="page"><span t-field="o.name"/></div>
        </t>
    </template>
    <record id="action_report_rag_source" model="ir.actions.report">
        <field name="name">RAG Source</field>
        <field name="model">ai.rag.source</field>
        <field name="report_type">qweb-pdf</field>
        <field name="report_name">viin_ai_rag.report_rag_source_document</field>
        <field name="report_file">viin_ai_rag.report_rag_source_document</field>
    </record>
</odoo>
"""

_RAG_PATCH_JS = """\
/** @odoo-module */
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
patch(FormController.prototype, {
    setup() {
        super.setup();
    },
});
"""

_RAG_OWL_JS = """\
/** @odoo-module */
import { Component } from "@odoo/owl";

export class RagSourcePanel extends Component {
    static template = "viin_ai_rag.RagSourcePanel";
}
"""

_RAG_SCSS = """\
$rag-panel-gap: 8px;
.o_rag_source_panel {
    padding: $rag-panel-gap;
}
"""

_RAG_HOOT_TEST = """\
import { describe, test, expect } from "@odoo/hoot";

describe("RagSourcePanel", () => {
    test("renders the source list", async () => {
        expect(true).toBe(true);
    });
});
"""

_RAG_TEST_COMMON = '''\
from odoo.tests.common import TransactionCase


class RagCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = cls.env["ai.rag.source"].create({"name": "Handbook"})
'''

_RAG_TEST = '''\
from odoo.tests.common import tagged

from .common import RagCommon


@tagged("post_install", "-at_install")
class TestRagSource(RagCommon):

    def test_reindex_returns_true(self):
        self.assertTrue(self.source.action_reindex())
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_viin_ai(root: Path) -> Path:
    mod = root / SURVIVOR
    _write(mod / "__init__.py", "from . import models\n")
    _write(mod / "__manifest__.py", _manifest(SURVIVOR, ["base"]))
    _write(mod / "models" / "__init__.py", "from . import ai_assistant\n")
    _write(mod / "models" / "ai_assistant.py", _VIIN_AI_MODELS)
    _write(mod / "views" / "ai_assistant_views.xml", _VIIN_AI_VIEWS)
    return mod


def write_viin_ai_rag(root: Path) -> Path:
    mod = root / RETIRED
    _write(mod / "__init__.py", "from . import models\n")
    _write(
        mod / "__manifest__.py",
        _manifest(
            RETIRED, [SURVIVOR, "web"],
            assets={"web.assets_backend": [f"{RETIRED}/static/src/**/*"]},
        ),
    )
    _write(mod / "models" / "__init__.py", "from . import ai_rag_source\n")
    _write(mod / "models" / "ai_rag_source.py", _RAG_MODELS)
    _write(mod / "views" / "ai_rag_source_views.xml", _RAG_VIEWS)
    _write(mod / "report" / "rag_source_report.xml", _RAG_REPORT)
    _write(mod / "static" / "src" / "js" / "form_patch.js", _RAG_PATCH_JS)
    _write(mod / "static" / "src" / "components" / "rag_source_panel.js", _RAG_OWL_JS)
    _write(mod / "static" / "src" / "scss" / "rag.scss", _RAG_SCSS)
    _write(mod / "static" / "tests" / "rag_source_panel.test.js", _RAG_HOOT_TEST)
    _write(mod / "tests" / "__init__.py", "from . import test_rag_source\n")
    _write(mod / "tests" / "common.py", _RAG_TEST_COMMON)
    _write(mod / "tests" / "test_rag_source.py", _RAG_TEST)
    return mod


def git_init_commit(repo_dir: Path) -> None:
    def _git(*args):
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True)

    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    _git("checkout", "-b", TEST_VERSION)
    _git("add", "-A")
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")


class _StubRepoStore:
    """_index_repo reads/advances repos.head_sha through repo_store(); no PG here."""

    def get_repo_head_sha(self, _id):
        return None

    def update_repo_head_sha(self, _id, _sha):
        return None

    def get_repo_ids_by_local_path_basenames(self, *_a, **_k):
        return []

    def reset_head_sha(self, *_a, **_k):
        return 0


def index_repo_dir(writer, monkeypatch, repo_dir: Path, *, profile: str, repo_id: int,
                   gc: bool = False) -> None:
    """Run the REAL indexer write path over *repo_dir* under *profile*."""
    from src.indexer.pipeline import _index_repo, reconcile_test_surface

    monkeypatch.setattr("src.indexer.pipeline.repo_store", lambda: _StubRepoStore())
    repo = {
        "id": repo_id,
        "local_path": str(repo_dir),
        "odoo_version": TEST_VERSION,
        "url": f"file://{repo_dir.name}",
    }
    _index_repo(
        repo, writer, full_reindex=True, profile_name=profile,
        core_rng_root=RNG_DIR, refresh=False, gc=gc,
    )
    reconcile_test_surface(writer, [TEST_VERSION], framework_profiles=[profile])


def build_and_index(tmp_path: Path, writer, monkeypatch, *, repo_name: str = "viindoo_addons",
                    profile: str = "viindoo_99", repo_id: int = 7801,
                    with_rag: bool = True) -> Path:
    repo_dir = tmp_path / repo_name
    repo_dir.mkdir()
    write_viin_ai(repo_dir)
    if with_rag:
        write_viin_ai_rag(repo_dir)
    git_init_commit(repo_dir)
    index_repo_dir(writer, monkeypatch, repo_dir, profile=profile, repo_id=repo_id)
    return repo_dir


def labels_with_module(driver, module: str) -> dict[str, int]:
    """{label: count} of every node at TEST_VERSION carrying ``module = <module>``."""
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (n) WHERE n.odoo_version = $v AND n.module = $m AND NOT n:Module
            UNWIND labels(n) AS lbl
            RETURN lbl, count(*) AS n ORDER BY lbl
            """,
            v=TEST_VERSION, m=module,
        ).data()
    return {r["lbl"]: r["n"] for r in rows}


def lint_violations_of(driver, module: str) -> int:
    """LintViolations reachable from *module*: HAS_VIOLATION edge from one of its
    Views, a view_xmlid naming one of its Views, or a ``<module>.`` xmlid prefix."""
    with driver.session() as s:
        return s.run(
            """
            MATCH (lv:LintViolation {odoo_version: $v})
            WHERE EXISTS { MATCH (:View {module: $m, odoo_version: $v})-[:HAS_VIOLATION]->(lv) }
               OR EXISTS { MATCH (vw:View {module: $m, odoo_version: $v})
                           WHERE vw.xmlid = lv.view_xmlid }
               OR lv.view_xmlid STARTS WITH ($m + '.')
            RETURN count(lv) AS n
            """,
            v=TEST_VERSION, m=module,
        ).single()["n"]
