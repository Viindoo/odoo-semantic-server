"""Accept-test harness for Phase 2 — ``resolve_view`` top-50 benchmark.

Same transport-bypass pattern as ``runner.py`` (WP-9): invokes the
handler in-process via a psycopg cursor, measures correctness vs the
live-Odoo golden dump committed under
``tests/fixtures/golden/resolve_view_live/``, records token reduction +
per-view latency, and writes a Markdown + JSON report.

Per-view pipeline:

1. Canonicalize handler's ``final_xml`` with ``lxml.etree.canonicalize(strip_text=True)``.
2. Canonicalize golden file the same way (they are already canonical from
   the dump script, but re-canonicalizing is cheap and defends against
   whitespace drift introduced by file-system round-trips).
3. Compute ``diff%`` via ``difflib.unified_diff`` line count divided
   by ``max(golden_lines, handler_lines) * 100``.
4. Tiktoken ``cl100k_base`` counts for (a) handler's final XML and
   (b) raw-source baseline = concatenation of every file_path in the
   handler's chain (file bytes, UTF-8).
5. ``reduction% = (1 - handler_tokens / raw_tokens) * 100``.
6. Latency: 100 iterations, record times, compute ``P50`` + ``P99``.

Exit criteria (from ``roadmap.md`` P2 + ``tasks/phase-02-plan.md`` §WP-17):

- Mean diff% across views with golden: ``< 5%``
- Overall token reduction: ``≥ 70%``
- Overall P50 latency: ``< 100ms``
- Coverage: at least ``--coverage-threshold`` (default 40) of the 50
  views must have a golden file available; otherwise the run fails with
  exit code 1 regardless of per-view results.

Invocation (on osm-dev after dump_live_odoo_views.py has populated the
fixture directory):

    DATABASE_URL=postgresql:///osm_live?user=osm \\
        OSM_TENANT=public \\
        uv run python -m tests.accept.runner_p2 \\
        --coverage-threshold 40
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import tiktoken
from lxml import etree

from osm.server.errors import NotFoundError
from osm.server.handlers.resolve_view import resolve_view
from osm.server.tenancy import context_from_tenant

REPO = Path(__file__).resolve().parent.parent.parent
GOLDEN_DIR = REPO / "tests" / "fixtures" / "golden" / "resolve_view_live"
TOP50_JSON = REPO / "tests" / "accept" / "top50_views.json"
REPORTS = REPO / "reports"

_ENCODER = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class ViewResult:
    xmlid: str
    status: str  # "ok" | "no_golden" | "404" | "error"
    diff_pct: float | None
    handler_tokens: int
    raw_tokens: int
    reduction_pct: float | None
    p50_ms: float
    p99_ms: float
    iterations: int
    chain_length: int
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_xmlid(xmlid: str) -> str:
    return xmlid.replace(".", "__")


def _load_view_list() -> list[str]:
    blob = json.loads(TOP50_JSON.read_text(encoding="utf-8"))
    views = blob.get("views") or []
    return [v["xmlid"] for v in views if isinstance(v, dict) and v.get("xmlid")]


def _canonicalize(xml_text: str) -> str:
    """Return lxml-canonical XML with insignificant whitespace stripped."""
    out: str = etree.canonicalize(xml_text, strip_text=True)
    return out


def _count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


def _diff_pct(golden_canonical: str, handler_canonical: str) -> float:
    golden_lines = golden_canonical.splitlines()
    handler_lines = handler_canonical.splitlines()
    diff = list(
        difflib.unified_diff(
            golden_lines, handler_lines,
            lineterm="", n=0,
        )
    )
    # Strip the ``---`` / ``+++`` / ``@@`` metadata lines to avoid noise.
    diff_body = [ln for ln in diff if ln[:2] not in ("--", "++", "@@")]
    denom = max(len(golden_lines), len(handler_lines), 1)
    return len(diff_body) / denom * 100


def _raw_tokens_from_chain(
    cur: Any,
    schema: str,
    handler_result: dict[str, Any],
) -> tuple[int, int]:
    """Sum tiktoken tokens across every ``file_path`` contributing to the
    chain. Returns ``(raw_tokens, chain_length)``.

    Reads the ``file_path`` column from ``views`` for every ``xmlid`` in the
    resolved chain. Schema scoping matches the handler's tenancy overlay.
    """
    chain_meta = handler_result.get("result", {}).get("chain", [])
    if not chain_meta:
        return 0, 0
    xmlids = [row["xmlid"] for row in chain_meta]
    # file_path is the source XML file — concatenate their raw content.
    from psycopg import sql
    q = sql.SQL(
        "SELECT DISTINCT file_path FROM {schema}.views "
        "WHERE xmlid = ANY(%s)"
    ).format(schema=sql.Identifier(schema))
    cur.execute(q, (xmlids,))
    total = 0
    seen_paths: set[str] = set()
    for (file_path,) in cur.fetchall():
        if not file_path or file_path in seen_paths:
            continue
        seen_paths.add(file_path)
        try:
            total += _count_tokens(Path(file_path).read_text(encoding="utf-8"))
        except OSError:
            # File moved / not mounted — skip but log via notes upstream.
            continue
    return total, len(chain_meta)


# ---------------------------------------------------------------------------
# Per-view run
# ---------------------------------------------------------------------------


def _run_one(
    cur: Any,
    ctx: Any,
    schema: str,
    xmlid: str,
    iterations: int,
) -> ViewResult:
    notes: list[str] = []
    golden_path = GOLDEN_DIR / f"{_escape_xmlid(xmlid)}.xml"
    has_golden = golden_path.is_file()
    if not has_golden:
        notes.append(f"golden missing at {golden_path}")

    # Correctness + token measurement — single call first.
    try:
        env = resolve_view(cur, ctx, xmlid)
    except NotFoundError as exc:
        return ViewResult(
            xmlid=xmlid, status="404",
            diff_pct=None, handler_tokens=0, raw_tokens=0,
            reduction_pct=None, p50_ms=0.0, p99_ms=0.0,
            iterations=0, chain_length=0,
            notes=[f"NotFoundError: {exc}"],
        )
    except Exception as exc:  # pragma: no cover — defensive
        return ViewResult(
            xmlid=xmlid, status="error",
            diff_pct=None, handler_tokens=0, raw_tokens=0,
            reduction_pct=None, p50_ms=0.0, p99_ms=0.0,
            iterations=0, chain_length=0,
            notes=[f"{type(exc).__name__}: {exc}"],
        )

    handler_xml = env["result"].get("final_xml", "")
    handler_canonical = _canonicalize(handler_xml) if handler_xml else ""
    handler_tokens = _count_tokens(handler_canonical)

    diff_pct: float | None = None
    if has_golden:
        golden_canonical = _canonicalize(
            golden_path.read_text(encoding="utf-8")
        )
        diff_pct = _diff_pct(golden_canonical, handler_canonical)

    raw_tokens, chain_length = _raw_tokens_from_chain(cur, schema, env)
    reduction = (
        (1.0 - handler_tokens / raw_tokens) * 100
        if raw_tokens > 0 else None
    )

    # Latency loop — re-invoke the handler; swallow 404s defensively.
    latencies_ms: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            resolve_view(cur, ctx, xmlid)
        except NotFoundError:
            pass
        latencies_ms.append((time.perf_counter() - t0) * 1000)

    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p99 = (
        statistics.quantiles(latencies_ms, n=100)[-1]
        if len(latencies_ms) >= 100 else max(latencies_ms or [0.0])
    )

    status = "ok" if has_golden else "no_golden"
    return ViewResult(
        xmlid=xmlid, status=status,
        diff_pct=diff_pct, handler_tokens=handler_tokens,
        raw_tokens=raw_tokens, reduction_pct=reduction,
        p50_ms=p50, p99_ms=p99,
        iterations=len(latencies_ms), chain_length=chain_length,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_markdown(
    results: list[ViewResult],
    iterations: int,
    tenant: str,
    coverage_threshold: int,
) -> str:
    ok_results = [r for r in results if r.status == "ok"]
    coverage = len(ok_results)
    mean_diff = (
        statistics.mean([r.diff_pct for r in ok_results if r.diff_pct is not None])
        if ok_results else float("nan")
    )
    total_handler = sum(r.handler_tokens for r in ok_results)
    total_raw = sum(r.raw_tokens for r in ok_results)
    overall_reduction = (
        (1.0 - total_handler / total_raw) * 100 if total_raw > 0 else float("nan")
    )
    overall_p50 = (
        statistics.median([r.p50_ms for r in ok_results]) if ok_results else 0.0
    )
    overall_p99 = max((r.p99_ms for r in ok_results), default=0.0)

    lines: list[str] = [
        "---",
        "status: draft",
        "scope: reports/phase-02-accept",
        "phase: P2",
        "reads-with:",
        "  - ../tests/accept/questions.md",
        "  - ../tests/accept/top50_views.json",
        "---",
        "",
        "# Phase 2 accept-test results — top-50 views",
        "",
        f"Iterations per view (latency loop): **{iterations}**",
        f"Tenant schema: `{tenant}`",
        f"Coverage (views with live-Odoo golden): **{coverage}/{len(results)}** "
        f"(threshold: {coverage_threshold})",
        "",
        "Diff formula: ``len(unified_diff_lines) / max(len(golden_lines), "
        "len(handler_lines)) * 100`` — ``--`` / ``++`` / ``@@`` header "
        "lines excluded.",
        "",
        "## Per-view results",
        "",
        "| xmlid | status | chain | diff% | handler tok | raw tok | reduction | P50 ms | P99 ms |",
        "|-------|--------|-------|-------|-------------|---------|-----------|--------|--------|",
    ]
    for r in results:
        diff = f"{r.diff_pct:.2f}%" if r.diff_pct is not None else "—"
        red = f"{r.reduction_pct:.1f}%" if r.reduction_pct is not None else "—"
        lines.append(
            f"| {r.xmlid} | {r.status} | {r.chain_length} | {diff} | "
            f"{r.handler_tokens} | {r.raw_tokens} | {red} | "
            f"{r.p50_ms:.2f} | {r.p99_ms:.2f} |"
        )

    lines.extend([
        "",
        "## Aggregate",
        "",
        f"- Mean diff%: **{mean_diff:.2f}%** (target <5%)",
        f"- Overall token reduction: **{overall_reduction:.1f}%** "
        f"(target ≥70%)",
        f"- Median P50: **{overall_p50:.2f} ms** (target <100ms)",
        f"- Max P99: **{overall_p99:.2f} ms**",
        "",
        "## Notes",
        "",
    ])
    any_notes = False
    for r in results:
        for note in r.notes:
            any_notes = True
            lines.append(f"- {r.xmlid}: {note}")
    if not any_notes:
        lines.append("- (no per-view notes)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--iterations", type=int, default=100,
        help="Latency-loop iteration count per view",
    )
    ap.add_argument(
        "--coverage-threshold", type=int, default=40,
        help="Minimum number of views with golden that must be measured",
    )
    ap.add_argument(
        "--diff-threshold", type=float, default=5.0,
        help="Mean diff%% threshold (pass if mean <= this)",
    )
    ap.add_argument(
        "--reduction-threshold", type=float, default=70.0,
        help="Overall token reduction%% threshold (pass if >= this)",
    )
    ap.add_argument(
        "--p50-threshold-ms", type=float, default=100.0,
        help="P50 latency threshold in milliseconds (pass if <= this)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 2
    tenant = os.environ.get("OSM_TENANT", "public")
    ctx = context_from_tenant(tenant)

    xmlids = _load_view_list()
    if not xmlids:
        print("error: top50_views.json has no entries", file=sys.stderr)
        return 2

    results: list[ViewResult] = []
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        for idx, xmlid in enumerate(xmlids, 1):
            print(f"[accept-p2] {idx}/{len(xmlids)} {xmlid}", flush=True)
            results.append(_run_one(cur, ctx, tenant, xmlid, args.iterations))

    ok_results = [r for r in results if r.status == "ok"]
    coverage = len(ok_results)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "phase-02-accept-raw.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (REPORTS / "phase-02-accept.md").write_text(
        _render_markdown(results, args.iterations, tenant, args.coverage_threshold),
        encoding="utf-8",
    )
    print(f"wrote {REPORTS / 'phase-02-accept.md'}")

    # Exit criteria evaluation.
    if coverage < args.coverage_threshold:
        print(
            f"FAIL: coverage {coverage} < {args.coverage_threshold} views with golden",
            file=sys.stderr,
        )
        return 1
    if not ok_results:
        print("FAIL: no ok results to evaluate", file=sys.stderr)
        return 1

    mean_diff = statistics.mean(
        [r.diff_pct for r in ok_results if r.diff_pct is not None]
    )
    total_handler = sum(r.handler_tokens for r in ok_results)
    total_raw = sum(r.raw_tokens for r in ok_results)
    overall_reduction = (
        (1.0 - total_handler / total_raw) * 100 if total_raw > 0 else 0.0
    )
    overall_p50 = statistics.median([r.p50_ms for r in ok_results])

    failed: list[str] = []
    if mean_diff > args.diff_threshold:
        failed.append(f"mean diff {mean_diff:.2f}% > {args.diff_threshold}%")
    if overall_reduction < args.reduction_threshold:
        failed.append(
            f"reduction {overall_reduction:.1f}% < {args.reduction_threshold}%"
        )
    if overall_p50 > args.p50_threshold_ms:
        failed.append(f"P50 {overall_p50:.2f}ms > {args.p50_threshold_ms}ms")
    if failed:
        print("FAIL: " + "; ".join(failed), file=sys.stderr)
        return 1
    print(
        f"PASS: mean_diff={mean_diff:.2f}% "
        f"reduction={overall_reduction:.1f}% "
        f"P50={overall_p50:.2f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
