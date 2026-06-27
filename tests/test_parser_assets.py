# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-D — version-aware asset-bundle parser (parser_assets, ADR-0052).

Behaviour contract (no DB): the aggregate dispatcher parse_assets() resolves the
Odoo major to era A (v8-v14, XML <template> bundles — owned by parser_qweb, so
NO contributions emitted here) vs era B (v15-v19, __manifest__.py 'assets' dict),
and the era-B parser must understand every manifest entry form (str/include/
remove/prepend/before/after/replace) using real snippets from the surveys.
"""
import textwrap

from src.indexer.models import ModuleInfo
from src.indexer.parser_assets import parse_assets


def _mod(version: str, path: str = "/nonexistent") -> ModuleInfo:
    return ModuleInfo(
        name="web", odoo_version=version, repo=f"odoo_{version}",
        path=path, depends=[], version_raw=f"{version}.1.0.0",
    )


# --- Version dispatch (the ADR-0052 boundary) --------------------------------

def test_v14_dispatches_to_era_a_no_contributions():
    """v14 is the last XML-template era: parse_assets emits NO contributions
    (parser_qweb owns the legacy <template> bundles). Manifest assets are absent
    in v14 anyway — passing one must still yield empty for era A."""
    res = parse_assets(_mod("14.0"), manifest={"assets": {"web.assets_backend": ["a/b.js"]}})
    assert res.contributions == []


def test_v15_dispatches_to_era_b():
    """v15 is the first manifest-dict era: the same manifest now produces a bundle."""
    res = parse_assets(_mod("15.0"), manifest={"assets": {"web.assets_backend": ["a/b.js"]}})
    assert [c.bundle_name for c in res.contributions] == ["web.assets_backend"]


def test_v19_dispatches_to_era_b():
    res = parse_assets(_mod("19.0"), manifest={"assets": {"web.assets_web": ["a/b.js"]}})
    assert [c.bundle_name for c in res.contributions] == ["web.assets_web"]


def test_unparseable_or_pre_era_version_yields_empty():
    assert parse_assets(_mod("7.0"), manifest={"assets": {"x.y": ["z"]}}).contributions == []
    assert parse_assets(_mod("garbage"), manifest={"assets": {"x.y": ["z"]}}).contributions == []


# --- Era B: every manifest operation form (survey eraBC §1) ------------------

def test_era_b_plain_string_path():
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {"web.assets_backend": [
            "account/static/src/scss/account_journal_dashboard.scss",
        ]},
    })
    c = res.contributions[0]
    assert c.entries == ["account/static/src/scss/account_journal_dashboard.scss"]
    assert c.includes == []


def test_era_b_absolute_path_is_normalized():
    """Leading '/' (Odoo-root-relative) stored identically to module-relative form."""
    res = parse_assets(_mod("15.0"), manifest={
        "assets": {"web.report_assets_common": [
            "/web/static/src/legacy/scss/asset_styles_company_report.scss",
        ]},
    })
    assert res.contributions[0].entries == [
        "web/static/src/legacy/scss/asset_styles_company_report.scss",
    ]


def test_era_b_include_op_collects_include_target():
    res = parse_assets(_mod("15.0"), manifest={
        "assets": {"web.assets_common": [
            ("include", "web._assets_helpers"),
        ]},
    })
    c = res.contributions[0]
    assert c.entries == [["include", "web._assets_helpers"]]
    assert c.includes == ["web._assets_helpers"]


def test_era_b_remove_op():
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {"web.assets_backend": [
            ("remove", "web/static/src/webclient/clickbot/clickbot.js"),
        ]},
    })
    c = res.contributions[0]
    assert c.entries == [["remove", "web/static/src/webclient/clickbot/clickbot.js"]]
    assert c.includes == []


def test_era_b_prepend_op():
    res = parse_assets(_mod("15.0"), manifest={
        "assets": {"web._assets_secondary_variables": [
            ("prepend", "website/static/src/scss/secondary_variables.scss"),
        ]},
    })
    assert res.contributions[0].entries == [
        ["prepend", "website/static/src/scss/secondary_variables.scss"],
    ]


def test_era_b_replace_op_3tuple():
    res = parse_assets(_mod("15.0"), manifest={
        "assets": {"web.assets_frontend": [
            ("replace",
             "web/static/src/legacy/js/public/public_root_instance.js",
             "website/static/src/js/content/website_root_instance.js"),
        ]},
    })
    assert res.contributions[0].entries == [[
        "replace",
        "web/static/src/legacy/js/public/public_root_instance.js",
        "website/static/src/js/content/website_root_instance.js",
    ]]


def test_era_b_before_op_3tuple():
    res = parse_assets(_mod("16.0"), manifest={
        "assets": {"web.dark_mode_variables": [
            ("before",
             "base/static/src/scss/onboarding.variables.scss",
             "base/static/src/scss/onboarding.variables.dark.scss"),
        ]},
    })
    assert res.contributions[0].entries == [[
        "before",
        "base/static/src/scss/onboarding.variables.scss",
        "base/static/src/scss/onboarding.variables.dark.scss",
    ]]


def test_era_b_after_op_3tuple():
    res = parse_assets(_mod("15.0"), manifest={
        "assets": {"web.qunit_suite_tests": [
            ("after",
             "web/static/tests/legacy/views/kanban_tests.js",
             "account/static/tests/account_payment_field_tests.js"),
        ]},
    })
    assert res.contributions[0].entries[0][0] == "after"


def test_era_b_list_form_operations_accepted():
    """Manifest literals allow list as well as tuple for ops."""
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {"web.assets_backend": [
            ["include", "web._assets_core"],
            ["remove", "web/static/src/foo.js"],
        ]},
    })
    c = res.contributions[0]
    assert c.includes == ["web._assets_core"]
    assert len(c.entries) == 2


def test_era_b_double_underscore_bundle_not_skipped():
    """survey eraBC §3: web.__assets_tests_call__ is a functional internal wrapper,
    NOT to be skipped."""
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {"web.__assets_tests_call__": [
            "web/static/tests/ignore_missing_deps_start.js",
            ("include", "web.assets_tests"),
        ]},
    })
    names = [c.bundle_name for c in res.contributions]
    assert "web.__assets_tests_call__" in names


def test_era_b_is_private_signal_via_underscore():
    """Private sub-bundles use the '._' convention; the writer reads this off the
    name, but the parser preserves the exact name so the signal survives."""
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {
            "web._assets_helpers": ["web/static/src/scss/helpers.scss"],
            "web.assets_backend": ["web/static/src/backend.js"],
        },
    })
    names = {c.bundle_name for c in res.contributions}
    assert "web._assets_helpers" in names
    assert "web.assets_backend" in names


def test_era_b_malformed_entry_skipped_not_crash():
    """A stray unknown entry form must be skipped, not crash a full re-index."""
    res = parse_assets(_mod("17.0"), manifest={
        "assets": {"web.assets_backend": [
            "ok/path.js",
            ("bogus_op", "x"),          # unknown op -> skipped
            ("include",),                # wrong arity -> skipped
            42,                          # non-str/non-seq -> skipped
        ]},
    })
    assert res.contributions[0].entries == ["ok/path.js"]


def test_era_b_no_assets_key_yields_empty():
    res = parse_assets(_mod("17.0"), manifest={"name": "web", "depends": ["base"]})
    assert res.contributions == []


def test_era_b_assets_not_a_dict_yields_empty():
    res = parse_assets(_mod("17.0"), manifest={"assets": ["not", "a", "dict"]})
    assert res.contributions == []


def test_era_b_empty_bundle_still_emitted():
    """A bundle DECLARED with no usable entries is still a real base node that
    legacy <template inherit_id> extenders must resolve against (the WI-D point)."""
    res = parse_assets(_mod("17.0"), manifest={"assets": {"web.assets_tests": []}})
    assert [c.bundle_name for c in res.contributions] == ["web.assets_tests"]
    assert res.contributions[0].entries == []


# --- Era B: real manifest read from disk (Option 2, no manifest passed) ------

def test_era_b_reads_manifest_from_disk_when_not_passed(tmp_path):
    manifest_src = textwrap.dedent("""
        {
            'name': 'web',
            'version': '17.0.1.0.0',
            'depends': ['base'],
            'assets': {
                'web.assets_backend': [
                    'web/static/src/core/app.js',
                    ('include', 'web._assets_core'),
                ],
            },
        }
    """).strip()
    (tmp_path / "__manifest__.py").write_text(manifest_src)
    res = parse_assets(_mod("17.0", path=str(tmp_path)))  # no manifest arg
    c = res.contributions[0]
    assert c.bundle_name == "web.assets_backend"
    assert c.includes == ["web._assets_core"]


def test_era_a_does_not_read_manifest_from_disk(tmp_path):
    """Era A never needs the manifest — even a manifest with an assets dict on disk
    yields no contributions (proves the era-A handler short-circuits)."""
    (tmp_path / "__manifest__.py").write_text(
        "{'assets': {'web.assets_backend': ['a/b.js']}}"
    )
    res = parse_assets(_mod("14.0", path=str(tmp_path)))
    assert res.contributions == []


def test_module_and_version_carried_on_contribution():
    m = ModuleInfo(name="crm", odoo_version="17.0", repo="odoo_17.0",
                   path="/x", depends=[], version_raw="17.0.1.0.0")
    res = parse_assets(m, manifest={"assets": {"crm.assets": ["crm/static/x.js"]}})
    c = res.contributions[0]
    assert c.module == "crm"
    assert c.odoo_version == "17.0"
