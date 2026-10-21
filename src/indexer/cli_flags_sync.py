# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-sync the curated GLOBAL CLI flags in ``spec_data/cli_flags_<version>.json``
from the real ``tools/config.py`` of each Odoo version (issue #380 item 6, ADR-0055).

Why this exists
---------------
The curated flag files are hand-curated data. ADR-0055 requires such data to have an
oracle: here the oracle is the production parser (``parser_cli._parse_options_calls``)
run over the real ``<pkg>/tools/config.py``. Before this module the comparison existed
only as a dev-box test (``tests/test_cli_flags_content_parity.py``, ``odoo_source``
marker): when upstream changed a flag on a stable branch the test went red on the one
machine that had the checkouts, and the fix was an ad-hoc script that never reached the
repo. ``scripts/sync_cli_flags.py`` (``--check`` / ``--write``) drives this module and a
weekly GitHub Actions job runs it against fresh shallow checkouts.

Scope - GLOBAL flags only, by design
------------------------------------
``config.py`` is the only file the oracle reads. Per-command flags (``command_name``
not in ``{None, "server"}``: ``odoo-bin db``, ``i18n``, ``module``, ``scaffold``,
``deploy``, ``cloc``, ``upgrade_code`` ...) live in ``cli/*.py`` argparse code the oracle
never opens, so they have NO oracle at any version. ADR-0055 allows curated data without
an oracle only when the reason is written down - this is that statement: those entries are
left byte-for-byte untouched by every operation here, and the sync report states their
count per version so the gap stays visible.

What a sync changes (and what it never does)
--------------------------------------------
* a curated global flag the oracle also finds: ``status`` / ``default`` / ``type`` /
  ``help`` (``COMPARED_FIELDS``) are set to the oracle's values;
* a flag the oracle finds that has no curated global entry: appended after the last
  global entry with the oracle's values. ``replacement_flag_name`` / ``env_name`` /
  ``posix_only`` start at their defaults - the oracle never populates them
  (see ``tests/test_cli_flags_content_parity.py`` ``EXCLUDED_FIELDS``) so a reviewer must
  curate them by hand; the report lists every added flag for exactly that reason;
* a curated global flag the oracle does NOT find is only reported. It may be genuinely
  removed upstream or an oracle blind spot (``PosixOnlyOption(...)`` wrappers, multi-alias
  calls), and telling those apart is a human judgement the generator must not guess;
* the file's ``_global_flags_provenance`` block (repo, branch, commit, source file, oracle,
  date) is rewritten whenever the global data changed or the block is missing. An unchanged
  file is not re-stamped, so the recorded commit is "the source state this data was last
  brought in line with", not a churning "last looked at" timestamp.
"""
from __future__ import annotations

import copy
import json
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.indexer.models import CLIFlagInfo
from src.indexer.parser_cli import _parse_options_calls, _pkg_prefix

# Fields the oracle derives from the real add_option/add_argument call on every version
# (``parser_cli._build_flag_info``). SSOT for the content-parity test and for the sync.
COMPARED_FIELDS: tuple[str, ...] = ("status", "default", "type", "help")

# Key order of a curated flag entry (mirrors ``cli_flag.schema.json``).
_ENTRY_KEYS: tuple[str, ...] = (
    "flag_name", "command_name", "status", "default", "type", "help",
    "replacement_flag_name", "env_name", "posix_only",
)

GLOBAL_COMMANDS = (None, "server")

PROVENANCE_KEY = "_global_flags_provenance"
ORACLE_ID = "src.indexer.parser_cli._parse_options_calls"

SPEC_DATA_DIR = Path(__file__).parent / "spec_data"


# ---------------------------------------------------------------------------
# Diff (moved here from tests/test_cli_flags_content_parity.py - one definition)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldMismatch:
    flag_name: str
    field_name: str
    curated: object
    oracle: object


@dataclass(frozen=True)
class DiffResult:
    matched: frozenset[str]
    field_mismatches: tuple[FieldMismatch, ...]
    # Present in the curated global set, absent from the oracle output. NOT drift: it can
    # mean a genuinely removed flag OR a real oracle blind spot (the geoip-db multi-alias
    # call, v19 ``PosixOnlyOption(...)``); conflating the two would be a false alarm.
    curated_only: frozenset[str]
    # Present in the oracle output (demonstrably real, current source) but absent from the
    # curated global set. Always drift: the curated file lacks a flag that exists.
    oracle_only: frozenset[str]

    def field_mismatch_counts(self) -> dict[str, int]:
        counts = {f: 0 for f in COMPARED_FIELDS}
        for m in self.field_mismatches:
            counts[m.field_name] += 1
        return counts

    @property
    def drifted(self) -> bool:
        return bool(self.field_mismatches or self.oracle_only)


def curated_value(entry: dict, field_name: str) -> object:
    """The value a curated entry carries for *field_name* (``status`` defaults to stable)."""
    return entry.get("status", "stable") if field_name == "status" else entry.get(field_name)


def diff_global_flags(curated: dict[str, dict], oracle: dict[str, CLIFlagInfo]) -> DiffResult:
    """Field-by-field diff over ``COMPARED_FIELDS`` for flags present on both sides."""
    curated_names = frozenset(curated)
    oracle_names = frozenset(oracle)
    matched = curated_names & oracle_names
    mismatches: list[FieldMismatch] = []
    for name in sorted(matched):
        for field_name in COMPARED_FIELDS:
            c_val = curated_value(curated[name], field_name)
            o_val = getattr(oracle[name], field_name)
            if c_val != o_val:
                mismatches.append(FieldMismatch(name, field_name, c_val, o_val))
    return DiffResult(
        matched=matched,
        field_mismatches=tuple(mismatches),
        curated_only=curated_names - oracle_names,
        oracle_only=oracle_names - curated_names,
    )


# ---------------------------------------------------------------------------
# Curated data access
# ---------------------------------------------------------------------------

def spec_path(version: str, spec_dir: Path = SPEC_DATA_DIR) -> Path:
    return spec_dir / f"cli_flags_{version}.json"


def curated_majors(spec_dir: Path = SPEC_DATA_DIR) -> list[int]:
    """Majors that have a curated file ``cli_flags_<major>.0.json`` (the smoke placeholder
    ``99.0`` is not a real Odoo version and is excluded)."""
    majors = []
    for p in spec_dir.glob("cli_flags_*.0.json"):
        stem = p.stem.removeprefix("cli_flags_").removesuffix(".0")
        if stem.isdigit() and int(stem) < 90:
            majors.append(int(stem))
    return sorted(majors)


def is_global(entry: dict) -> bool:
    return entry.get("command_name") in GLOBAL_COMMANDS


def global_flags(data: dict) -> dict[str, dict]:
    """Curated GLOBAL entries keyed by flag_name. A duplicated global name is a data
    error (the diff would silently compare only one of them)."""
    out: dict[str, dict] = {}
    for entry in data.get("flags", []):
        if not is_global(entry):
            continue
        name = entry["flag_name"]
        if name in out:
            raise ValueError(f"duplicate global flag {name!r} in curated data")
        out[name] = entry
    return out


def out_of_scope_count(data: dict) -> int:
    """Per-command entries: no oracle exists, never touched (module docstring)."""
    return sum(1 for e in data.get("flags", []) if not is_global(e))


def load_data(version: str, spec_dir: Path = SPEC_DATA_DIR) -> dict:
    return json.loads(spec_path(version, spec_dir).read_text(encoding="utf-8"))


def dump_data(data: dict) -> str:
    """Serialise exactly as the curated files are stored (indent 2, UTF-8, final newline)."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Oracle + provenance (the real source)
# ---------------------------------------------------------------------------

def config_path(checkout: Path, major: int) -> Path:
    return checkout / _pkg_prefix(f"{major}.0") / "tools" / "config.py"


def oracle_global_flags(
    source: str, version: str, file_path: str | None = None,
) -> dict[str, CLIFlagInfo]:
    flags = _parse_options_calls(source, version, command_name="server", file_path=file_path)
    return {f.flag_name: f for f in flags}


def _git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def read_provenance(checkout: Path, major: int, *, today: date | None = None) -> dict:
    """Where the oracle's input came from: repo, branch, commit, file (ADR-0055).

    Raises ``RuntimeError`` when the checkout cannot vouch for itself: not a git work
    tree, on a branch other than ``<major>.0`` (a wrong directory would silently stamp
    the wrong version's data), or ``config.py`` modified relative to HEAD (the commit
    would not describe the bytes that were parsed).
    """
    cfg = config_path(checkout, major)
    rel = cfg.relative_to(checkout).as_posix()
    try:
        commit = _git(checkout, "rev-parse", "HEAD")
        branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
        repo = _git(checkout, "remote", "get-url", "origin")
        dirty = _git(checkout, "status", "--porcelain", "--", rel)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RuntimeError(f"{checkout}: cannot read git provenance ({exc})") from exc
    if branch != f"{major}.0":
        raise RuntimeError(
            f"{checkout} is on branch {branch!r}, expected '{major}.0' - refusing to "
            "stamp this checkout as the source of that version's data"
        )
    if dirty:
        raise RuntimeError(f"{checkout}: {rel} differs from HEAD ({dirty!r})")
    return {
        "source_repo": repo,
        "source_branch": branch,
        "source_commit": commit,
        "source_file": rel,
        "oracle": ORACLE_ID,
        "synced_at": (today or date.today()).isoformat(),
    }


# ---------------------------------------------------------------------------
# Plan / apply
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncPlan:
    version: str
    diff: DiffResult
    out_of_scope: int
    provenance_missing: bool
    new_data: dict
    added_flags: tuple[str, ...]

    @property
    def drifted(self) -> bool:
        return self.diff.drifted or self.provenance_missing


def _with_provenance(data: dict, provenance: dict) -> dict:
    """*data* with the provenance block set, kept with the other ``_`` metadata keys
    (before ``commands`` / ``flags``) rather than appended after the flag list."""
    out: dict = {}
    placed = False
    for key, value in data.items():
        if key == PROVENANCE_KEY:
            out[key] = provenance
            placed = True
            continue
        if not placed and not key.startswith("_"):
            out[PROVENANCE_KEY] = provenance
            placed = True
        out[key] = value
    if not placed:
        out[PROVENANCE_KEY] = provenance
    return out


def plan_sync(
    version: str, data: dict, oracle: dict[str, CLIFlagInfo], provenance: dict,
) -> SyncPlan:
    """Diff *data* against *oracle* and build the data a ``--write`` would store.

    ``data`` is never mutated. Per-command entries are carried over unchanged.
    """
    curated = global_flags(data)
    diff = diff_global_flags(curated, oracle)
    provenance_missing = PROVENANCE_KEY not in data

    new_data = copy.deepcopy(data)
    new_globals = global_flags(new_data)
    for m in diff.field_mismatches:
        # A status that differs from the schema default is stored explicitly, so a
        # deprecated flag is never read back as "stable".
        new_globals[m.flag_name][m.field_name] = m.oracle

    added: list[str] = []
    flags = new_data.setdefault("flags", [])
    insert_at = max((i for i, e in enumerate(flags) if is_global(e)), default=-1) + 1
    for name in sorted(diff.oracle_only):
        o = oracle[name]
        entry = {
            "flag_name": name, "command_name": None, "status": o.status,
            "default": o.default, "type": o.type, "help": o.help,
            "replacement_flag_name": None, "env_name": None, "posix_only": False,
        }
        assert tuple(entry) == _ENTRY_KEYS
        flags.insert(insert_at, entry)
        insert_at += 1
        added.append(name)

    if diff.drifted or provenance_missing:
        new_data = _with_provenance(new_data, provenance)
    return SyncPlan(
        version=version, diff=diff, out_of_scope=out_of_scope_count(data),
        provenance_missing=provenance_missing, new_data=new_data,
        added_flags=tuple(added),
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_plan(plan: SyncPlan) -> str:
    d = plan.diff
    state = "DRIFT" if plan.drifted else "in sync"
    lines = [
        f"v{plan.version}: {state} - {len(d.matched)} global flag(s) compared, "
        f"{len(d.field_mismatches)} field mismatch(es) {d.field_mismatch_counts()}, "
        f"{len(d.oracle_only)} flag(s) missing from curated data, "
        f"{len(d.curated_only)} curated-only (not touched), "
        f"{plan.out_of_scope} per-command flag(s) out of scope (no oracle).",
    ]
    if plan.provenance_missing:
        lines.append(f"  provenance block {PROVENANCE_KEY!r} missing")
    for m in sorted(d.field_mismatches, key=lambda x: (x.flag_name, x.field_name)):
        lines.append(
            f"  {m.flag_name} {m.field_name}: curated={m.curated!r} -> source={m.oracle!r}"
        )
    for name in sorted(d.oracle_only):
        lines.append(
            f"  {name}: in source, not curated -> added; hand-curate "
            "replacement_flag_name / env_name / posix_only"
        )
    if d.curated_only:
        lines.append(
            f"  curated-only (removed upstream or oracle blind spot - review, never "
            f"auto-removed): {sorted(d.curated_only)}"
        )
    return "\n".join(lines)
