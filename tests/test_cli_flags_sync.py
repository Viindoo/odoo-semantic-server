# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI-flags re-sync generator (issue #380 item 6, ADR-0055).

Fast lane, no Odoo checkout: the oracle runs over small ``config.py`` snippets and the
provenance reader over throwaway git repos built in ``tmp_path``. The dev-box test
``test_cli_flags_content_parity.py`` is what proves the real files agree with real
source; this file proves the TOOL that re-syncs them behaves.

Business rules under test:
  S1  ``--check`` is green only when every curated global flag equals what the oracle
      parses from the source AND the provenance block exists; any compared-field change
      upstream, a flag added upstream, or a missing provenance is drift (exit 1).
  S2  ``--write`` brings exactly the drifted file in line, touches no other file, and a
      following ``--check`` is green (round trip).
  S3  Per-command flags (no oracle) are never modified - not even when a per-command
      entry shares its name with a global flag that IS updated.
  S4  A curated global flag the oracle does not find is reported, never removed and never
      counted as drift (removed upstream vs oracle blind spot is a human call).
  S5  A flag added from source lands with the schema defaults for the fields the oracle
      cannot populate, after the last global entry.
  S6  Provenance (repo, branch, commit, file) is the checkout's real git state, and a
      checkout on the wrong branch / with a modified config.py / without config.py or
      flags is an error (exit 2), never a silent stamp.
  S7  The writer reproduces the stored files byte-for-byte, so a ``--write`` diff only
      ever contains real changes.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from src.indexer import cli_flags_sync as sync

REPO_ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 20)

CONFIG_V17 = '''
class configmanager:
    def __init__(self):
        group.add_option("-d", "--database", dest="db_name", my_default=False,
                         help="specify the database name")
        group.add_option("--workers", dest="workers", type="int", my_default=0,
                         help="Specify the number of workers, 0 disable prefork mode.")
        group.add_option("--old-thing", dest="old", my_default=1,
                         help="deprecated: use --new-thing")
'''


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "sync_cli_flags_script", REPO_ROOT / "scripts" / "sync_cli_flags.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


script = _load_script()


def _entry(name, *, command=None, status="stable", default=None, type_=None, help_=None):
    return {
        "flag_name": name, "command_name": command, "status": status,
        "default": default, "type": type_, "help": help_,
        "replacement_flag_name": None, "env_name": None, "posix_only": False,
    }


def _curated_in_sync() -> dict:
    """Curated data equal to CONFIG_V17 plus one per-command entry sharing a global name."""
    return {
        "_curate_status": "complete",
        "_note": "fixture",
        "commands": [{"name": "server"}],
        "flags": [
            _entry("--database", default="False", help_="specify the database name"),
            _entry("--workers", default="0", type_="int",
                   help_="Specify the number of workers, 0 disable prefork mode."),
            _entry("--old-thing", status="deprecated", default="1",
                   help_="deprecated: use --new-thing"),
            _entry("--database", command="db", help_="per-command, hand curated"),
        ],
    }


PROV = {
    "source_repo": "https://example.invalid/odoo.git", "source_branch": "17.0",
    "source_commit": "a" * 40, "source_file": "odoo/tools/config.py",
    "oracle": sync.ORACLE_ID, "synced_at": "2026-10-20",
}


def _oracle(source: str = CONFIG_V17):
    return sync.oracle_global_flags(source, "17.0")


def _plan(data=None, source: str = CONFIG_V17, provenance=None):
    data = data if data is not None else {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    return sync.plan_sync("17.0", data, _oracle(source), provenance or PROV)


# ---------------------------------------------------------------------------
# plan_sync (library)
# ---------------------------------------------------------------------------

def test_data_equal_to_source_with_provenance_is_not_drift_and_is_returned_unchanged():
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    # A newer source commit with identical flags must not re-stamp the file: the recorded
    # commit is the state the data was last brought in line with, not "last looked at".
    plan = _plan(data, provenance={**PROV, "source_commit": "c" * 40})
    assert not plan.drifted
    assert plan.new_data == data


def test_missing_provenance_alone_is_drift_and_is_stamped():
    plan = _plan(_curated_in_sync(), provenance={**PROV, "source_commit": "b" * 40})
    assert plan.drifted and plan.provenance_missing
    assert plan.new_data[sync.PROVENANCE_KEY]["source_commit"] == "b" * 40
    # metadata stays grouped before commands/flags
    assert list(plan.new_data)[:4] == [
        "_curate_status", "_note", sync.PROVENANCE_KEY, "commands",
    ]


@pytest.mark.parametrize("field,old,new_source_fragment,expected", [
    ("help", "specify the database name", 'help="select the database"', "select the database"),
    ("default", "0", "my_default=4", "4"),
    ("type", "int", 'type="float"', "float"),
])
def test_upstream_change_of_a_compared_field_is_drift_and_updates_only_that_field(
    field, old, new_source_fragment, expected,
):
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    if field == "help":
        source = CONFIG_V17.replace('help="specify the database name"', new_source_fragment)
        flag = "--database"
    elif field == "default":
        source = CONFIG_V17.replace("my_default=0", new_source_fragment)
        flag = "--workers"
    else:
        source = CONFIG_V17.replace('type="int"', new_source_fragment)
        flag = "--workers"
    plan = _plan(data, source)
    assert plan.drifted
    assert [(m.flag_name, m.field_name) for m in plan.diff.field_mismatches] == [(flag, field)]
    new = next(e for e in plan.new_data["flags"] if e["flag_name"] == flag and is_global(e))
    old_entry = next(e for e in data["flags"] if e["flag_name"] == flag and is_global(e))
    assert new[field] == expected
    assert {k: v for k, v in new.items() if k != field} == \
        {k: v for k, v in old_entry.items() if k != field}


def is_global(entry):
    return sync.is_global(entry)


def test_status_change_is_written_explicitly():
    source = CONFIG_V17.replace('help="specify the database name"',
                                'help="DEPRECATED, use --db"')
    plan = _plan(source=source)
    new = next(e for e in plan.new_data["flags"]
               if e["flag_name"] == "--database" and is_global(e))
    assert new["status"] == "deprecated"


def test_per_command_entries_are_never_modified_even_when_a_global_of_the_same_name_is():
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    source = CONFIG_V17.replace('help="specify the database name"', 'help="changed upstream"')
    plan = _plan(data, source)
    per_cmd_before = [e for e in data["flags"] if not is_global(e)]
    per_cmd_after = [e for e in plan.new_data["flags"] if not is_global(e)]
    assert per_cmd_before == per_cmd_after == [
        _entry("--database", command="db", help_="per-command, hand curated"),
    ]
    assert plan.out_of_scope == 1


def test_curated_global_flag_missing_from_source_is_reported_but_not_removed_or_drift():
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    data["flags"].insert(1, _entry("--vanished", help_="maybe PosixOnlyOption blind spot"))
    plan = _plan(data)
    assert plan.diff.curated_only == {"--vanished"}
    assert not plan.drifted
    assert any(e["flag_name"] == "--vanished" for e in plan.new_data["flags"])
    assert "--vanished" in sync.format_plan(plan)


def test_flag_added_in_source_is_appended_after_the_last_global_with_schema_defaults():
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    source = CONFIG_V17 + (
        '\n        group.add_option("--log-config", dest="log_config", my_default="",\n'
        '                         help="JSON logging configuration file")\n'
    )
    plan = _plan(data, source)
    assert plan.drifted and plan.added_flags == ("--log-config",)
    names = [(e["flag_name"], e["command_name"]) for e in plan.new_data["flags"]]
    assert names == [("--database", None), ("--workers", None), ("--old-thing", None),
                     ("--log-config", None), ("--database", "db")]
    added = plan.new_data["flags"][3]
    assert added == _entry("--log-config", default="", help_="JSON logging configuration file")
    assert "hand-curate" in sync.format_plan(plan)


def test_plan_never_mutates_its_input():
    data = {**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}
    snapshot = json.dumps(data)
    _plan(data, CONFIG_V17.replace('help="specify the database name"', 'help="x"'))
    assert json.dumps(data) == snapshot


def test_duplicate_global_flag_in_curated_data_is_an_error():
    data = _curated_in_sync()
    data["flags"].append(_entry("--workers"))
    with pytest.raises(ValueError, match="duplicate global flag"):
        sync.global_flags(data)


def test_writer_keeps_non_ascii_text_readable():
    data = {"flags": [_entry("--x", help_="caf\u00e9 \u2014 d\u00e9faut")]}
    text = sync.dump_data(data)
    assert "caf\u00e9" in text and "\\u00e9" not in text
    assert json.loads(text) == data


def test_curated_majors_lists_real_versions_and_skips_the_smoke_placeholder():
    assert sync.curated_majors() == list(range(8, 20))


@pytest.mark.parametrize("major", range(8, 20))
def test_writer_reproduces_every_stored_file_byte_for_byte(major):
    """S7: a --write diff must contain only real changes, never reformatting."""
    path = sync.spec_path(f"{major}.0")
    assert sync.dump_data(sync.load_data(f"{major}.0")) == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("major", range(8, 20))
def test_every_stored_file_records_provenance_from_the_official_branch(major):
    """ADR-0055: the global flags carry repo + branch + commit + file + oracle."""
    prov = sync.load_data(f"{major}.0").get(sync.PROVENANCE_KEY)
    assert prov, f"cli_flags_{major}.0.json has no {sync.PROVENANCE_KEY}"
    assert prov["source_branch"] == f"{major}.0"
    assert prov["source_repo"].rstrip("/").removesuffix(".git").endswith("odoo/odoo")
    assert len(prov["source_commit"]) == 40
    pkg = "openerp" if major <= 9 else "odoo"
    assert prov["source_file"] == f"{pkg}/tools/config.py"
    assert prov["oracle"] == sync.ORACLE_ID


# ---------------------------------------------------------------------------
# scripts/sync_cli_flags.py against throwaway git checkouts
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
             "HOME": str(cwd), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    ).stdout.strip()


def _make_checkout(root: Path, source: str = CONFIG_V17, branch: str = "17.0") -> Path:
    co = root / "odoo17"
    cfg = co / "odoo" / "tools" / "config.py"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(source, encoding="utf-8")
    _git(co, "init", "-q", "-b", branch)
    _git(co, "remote", "add", "origin", "https://github.com/odoo/odoo")
    _git(co, "add", "-A")
    _git(co, "commit", "-q", "-m", "fixture")
    return co


@pytest.fixture
def world(tmp_path):
    checkouts = tmp_path / "checkouts"
    checkouts.mkdir()
    spec = tmp_path / "spec"
    spec.mkdir()
    co = _make_checkout(checkouts)
    (spec / "cli_flags_17.0.json").write_text(
        sync.dump_data({**_curated_in_sync(), sync.PROVENANCE_KEY: PROV}), encoding="utf-8",
    )
    return checkouts, spec, co


def _run(world, mode, *, report=None, majors=(17,)):
    checkouts, spec, _ = world
    import io
    out = io.StringIO()
    code = script.run(mode=mode, majors=list(majors), checkouts=checkouts, spec_dir=spec,
                      report_file=report, today=TODAY, out=out)
    return code, out.getvalue()


def _stored(world) -> dict:
    return json.loads((world[1] / "cli_flags_17.0.json").read_text(encoding="utf-8"))


def test_check_is_green_when_curated_data_equals_source(world):
    code, out = _run(world, "check")
    assert code == script.EXIT_OK, out


def test_check_fails_and_leaves_files_alone_when_upstream_changed_a_flag(world, tmp_path):
    cfg = world[2] / "odoo" / "tools" / "config.py"
    cfg.write_text(CONFIG_V17.replace('"specify the database name"', '"new upstream help"'))
    _git(world[2], "commit", "-q", "-am", "upstream change")
    before = (world[1] / "cli_flags_17.0.json").read_text(encoding="utf-8")
    code, out = _run(world, "check")
    assert code == script.EXIT_DRIFT, out
    assert "--database help" in out and "new upstream help" in out
    assert (world[1] / "cli_flags_17.0.json").read_text(encoding="utf-8") == before


def test_write_fixes_the_drift_records_the_real_commit_and_check_is_then_green(world):
    cfg = world[2] / "odoo" / "tools" / "config.py"
    cfg.write_text(CONFIG_V17.replace('"specify the database name"', '"new upstream help"'))
    _git(world[2], "commit", "-q", "-am", "upstream change")
    head = _git(world[2], "rev-parse", "HEAD")

    code, _ = _run(world, "write")
    assert code == script.EXIT_OK
    stored = _stored(world)
    db = next(e for e in stored["flags"] if e["flag_name"] == "--database" and is_global(e))
    assert db["help"] == "new upstream help"
    assert stored[sync.PROVENANCE_KEY] == {
        "source_repo": "https://github.com/odoo/odoo", "source_branch": "17.0",
        "source_commit": head, "source_file": "odoo/tools/config.py",
        "oracle": sync.ORACLE_ID, "synced_at": "2026-10-20",
    }
    code, out = _run(world, "check")
    assert code == script.EXIT_OK, out


def test_write_does_not_rewrite_a_file_that_is_already_in_sync(world):
    path = world[1] / "cli_flags_17.0.json"
    # Stored with a different indent: any rewrite would normalise it, so unchanged bytes
    # prove the file was not touched (and the old provenance date was not re-stamped).
    path.write_text(
        json.dumps({**_stored(world), sync.PROVENANCE_KEY: {**PROV, "synced_at": "2026-01-01"}},
                   indent=4),
        encoding="utf-8",
    )
    before = path.read_text(encoding="utf-8")
    code, out = _run(world, "write")
    assert code == script.EXIT_OK and "wrote 0 file(s)" in out
    assert path.read_text(encoding="utf-8") == before


def test_write_adds_a_flag_that_exists_only_in_source_and_reports_it(world, tmp_path):
    cfg = world[2] / "odoo" / "tools" / "config.py"
    cfg.write_text(CONFIG_V17 + '\n        group.add_option("--brand-new", help="new")\n')
    _git(world[2], "commit", "-q", "-am", "add flag")
    report = tmp_path / "report.md"
    code, _ = _run(world, "write", report=report)
    assert code == script.EXIT_OK
    assert [e["flag_name"] for e in _stored(world)["flags"] if is_global(e)][-1] == "--brand-new"
    text = report.read_text(encoding="utf-8")
    assert "--brand-new" in text and "Per-command flags have no oracle" in text


def test_checkout_on_the_wrong_branch_is_an_error_not_a_stamp(world, tmp_path):
    _git(world[2], "checkout", "-q", "-b", "master")
    code, _ = _run(world, "write")
    assert code == script.EXIT_ERROR


def test_modified_config_py_is_an_error_because_the_commit_would_not_describe_it(world):
    (world[2] / "odoo" / "tools" / "config.py").write_text(CONFIG_V17 + "\n# local edit\n")
    code, _ = _run(world, "check")
    assert code == script.EXIT_ERROR


def test_missing_checkout_is_an_error_not_a_skip(world):
    code, _ = _run(world, "check", majors=(17, 18))  # no odoo18 checkout, no cli_flags_18
    assert code == script.EXIT_ERROR


def test_source_without_any_flag_is_an_error_not_mass_drift(world):
    cfg = world[2] / "odoo" / "tools" / "config.py"
    cfg.write_text("x = 1\n")
    _git(world[2], "commit", "-q", "-am", "empty")
    code, _ = _run(world, "check")
    assert code == script.EXIT_ERROR


def test_print_majors_lists_the_curated_versions(capsys):
    assert script.main(["--print-majors"]) == script.EXIT_OK
    assert capsys.readouterr().out.split() == [str(m) for m in range(8, 20)]
