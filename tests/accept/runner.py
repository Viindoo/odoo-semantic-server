"""Model-graph benchmark harness.

Drives the 10 questions in `questions.md` against live handlers, measures:

- Correctness: handler response present without exception (404 expected for Q10).
- Token reduction vs raw-source baseline: tiktoken `cl100k_base` on both
  the handler JSON and the concatenated baseline files.
- Latency: per-question P50 + P99 across N iterations (default 100).

Writes a Markdown table + raw JSON under `reports/`.

Designed to run against a throwaway tenant schema seeded with the
committed fixture corpus. Requires `DATABASE_URL`.

Invocation:
    DATABASE_URL=postgresql:///osm_wp6_test?user=soncrits \\
        uv run python -m tests.accept.runner
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import tiktoken

from osm.indexer.driver import index as run_index
from osm.server.errors import NotFoundError
from osm.server.handlers.resolve_field import resolve_field
from osm.server.handlers.resolve_method import resolve_method
from osm.server.handlers.resolve_model import resolve_model
from osm.server.tenancy import TenantContext, validate_tenant
from scripts.create_tenant import main as create_tenant_main
from scripts.migrate import main as migrate_main

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "tests" / "fixtures"
REPORTS = REPO / "reports"

_ENCODER = tiktoken.get_encoding("cl100k_base")


# ---------------------------------------------------------------------------
# Question registry — mirrors tests/accept/questions.md
# ---------------------------------------------------------------------------


@dataclass
class Question:
    qid: str
    prompt: str
    tool: str
    args: tuple[str, ...]
    baseline_files: tuple[str, ...]
    expects_404: bool = False


QUESTIONS: list[Question] = [
    Question(
        qid="Q1",
        prompt="What modules contribute to account.move?",
        tool="resolve_model",
        args=("account.move",),
        baseline_files=("odoo_ce_subset/account/models/account_move.py",),
    ),
    Question(
        qid="Q2",
        prompt="Which modules have touched res.partner, in what order?",
        tool="resolve_model",
        args=("res.partner",),
        baseline_files=(
            "odoo_ce_subset/base/models/res_partner.py",
            "custom_addons/viin_fixture_order_override/models/res_partner.py",
        ),
    ),
    Question(
        qid="Q3",
        prompt="sale.order after sale_management + 7 viin_* fixtures: final chain?",
        tool="resolve_model",
        args=("sale.order",),
        baseline_files=(
            "odoo_ce_subset/sale/models/sale_order.py",
            "odoo_ce_subset/sale_management/models/sale_order.py",
            "custom_addons/viin_fixture_conditional_optional_dep/models/sale_order.py",
            "custom_addons/viin_fixture_depends_added/models/sale_order.py",
            "custom_addons/viin_fixture_field_override_compute/models/sale_order.py",
            "custom_addons/viin_fixture_field_override_no_compute/models/sale_order.py",
            "custom_addons/viin_fixture_method_override_break_super/models/sale_order.py",
            "custom_addons/viin_fixture_method_override_super/models/sale_order.py",
            "custom_addons/viin_fixture_multi_inherit/models/sale_order.py",
        ),
    ),
    Question(
        qid="Q4",
        prompt="Final definition of sale.order.partner_id after overrides?",
        tool="resolve_field",
        args=("sale.order", "partner_id"),
        baseline_files=(
            "odoo_ce_subset/sale/models/sale_order.py",
            "custom_addons/viin_fixture_field_override_no_compute/models/sale_order.py",
        ),
    ),
    Question(
        qid="Q5",
        prompt="Which compute runs for sale.order.amount_total, and what depends on it?",
        tool="resolve_field",
        args=("sale.order", "amount_total"),
        baseline_files=(
            "odoo_ce_subset/sale/models/sale_order.py",
            "custom_addons/viin_fixture_depends_added/models/sale_order.py",
            "custom_addons/viin_fixture_field_override_compute/models/sale_order.py",
        ),
    ),
    Question(
        qid="Q6",
        prompt="Walk me through sale.order._amount_all across modules.",
        tool="resolve_method",
        args=("sale.order", "_amount_all"),
        baseline_files=(
            "odoo_ce_subset/sale/models/sale_order.py",
            "custom_addons/viin_fixture_depends_added/models/sale_order.py",
        ),
    ),
    Question(
        qid="Q7",
        prompt="Does sale.order.action_confirm call super all the way down?",
        tool="resolve_method",
        args=("sale.order", "action_confirm"),
        baseline_files=(
            "odoo_ce_subset/sale/models/sale_order.py",
            "odoo_ce_subset/sale_management/models/sale_order.py",
            "custom_addons/viin_fixture_method_override_super/models/sale_order.py",
            "custom_addons/viin_fixture_method_override_break_super/models/sale_order.py",
        ),
    ),
    Question(
        qid="Q8",
        prompt="What does mail.thread look like? Is it abstract?",
        tool="resolve_model",
        args=("mail.thread",),
        baseline_files=("odoo_ce_subset/mail/models/mail_thread.py",),
    ),
    Question(
        qid="Q9",
        prompt="res.users delegates to what, via which FK?",
        tool="resolve_model",
        args=("res.users",),
        baseline_files=(
            "odoo_ce_subset/base/models/res_users.py",
            "odoo_ce_subset/contacts/models/res_users.py",
        ),
    ),
    Question(
        qid="Q10",
        prompt="Is sale.fancyMadeUpModel a real model here?",
        tool="resolve_model",
        args=("sale.fancyMadeUpModel",),
        baseline_files=(),
        expects_404=True,
    ),
]


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))


def _baseline_tokens(files: tuple[str, ...]) -> int:
    """Sum tiktoken tokens across the listed fixture files."""
    total = 0
    for rel in files:
        path = FIXTURES / rel
        if not path.is_file():
            raise FileNotFoundError(f"baseline file missing: {path}")
        total += _count_tokens(path.read_text(encoding="utf-8"))
    return total


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "resolve_model": resolve_model,
    "resolve_field": resolve_field,
    "resolve_method": resolve_method,
}


@dataclass
class QuestionResult:
    qid: str
    tool: str
    prompt: str
    status: str  # "ok" | "expected_404" | "error"
    response_tokens: int
    baseline_tokens: int
    token_reduction_pct: float | None  # None when baseline is zero (e.g. 404)
    p50_ms: float
    p99_ms: float
    iterations: int
    notes: list[str] = field(default_factory=list)


def _run_question(
    cur: Any,
    ctx: Any,
    q: Question,
    iterations: int,
) -> QuestionResult:
    handler = _HANDLERS[q.tool]
    notes: list[str] = []
    status = "ok"
    response_tokens = 0
    latencies_ms: list[float] = []

    # First call (correctness + token count)
    try:
        env = handler(cur, ctx, *q.args)
        payload = json.dumps(env, sort_keys=True)
        response_tokens = _count_tokens(payload)
    except NotFoundError as exc:
        if q.expects_404:
            status = "expected_404"
            notes.append(f"NotFoundError raised as expected: {exc}")
        else:
            status = "error"
            notes.append(f"unexpected NotFoundError: {exc}")
    except Exception as exc:  # pragma: no cover — defensive
        status = "error"
        notes.append(f"{type(exc).__name__}: {exc}")
        return QuestionResult(
            qid=q.qid, tool=q.tool, prompt=q.prompt, status=status,
            response_tokens=0, baseline_tokens=0,
            token_reduction_pct=None, p50_ms=0.0, p99_ms=0.0,
            iterations=0, notes=notes,
        )

    # Latency loop (run handler multiple times; swallow expected 404s)
    for _ in range(iterations):
        t0 = time.perf_counter()
        try:
            handler(cur, ctx, *q.args)
        except NotFoundError:
            pass
        latencies_ms.append((time.perf_counter() - t0) * 1000)

    p50 = statistics.median(latencies_ms) if latencies_ms else 0.0
    p99 = statistics.quantiles(latencies_ms, n=100)[-1] if len(latencies_ms) >= 100 else max(
        latencies_ms or [0.0]
    )

    baseline_tokens = _baseline_tokens(q.baseline_files) if q.baseline_files else 0
    reduction = None
    if baseline_tokens > 0 and status == "ok":
        reduction = (1.0 - response_tokens / baseline_tokens) * 100

    return QuestionResult(
        qid=q.qid, tool=q.tool, prompt=q.prompt, status=status,
        response_tokens=response_tokens, baseline_tokens=baseline_tokens,
        token_reduction_pct=reduction, p50_ms=p50, p99_ms=p99,
        iterations=len(latencies_ms), notes=notes,
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_markdown(results: list[QuestionResult], tenant: str, iterations: int) -> str:
    lines: list[str] = [
        "# Model-graph benchmark results",
        "",
        f"Runner iterations per question: **{iterations}** (latency loop).",
        f"Live tenant schema: `{tenant}` (throwaway, dropped on teardown).",
        "Token counter: `tiktoken` encoding `cl100k_base` (GPT-4 family).",
        "",
        "## Per-question results",
        "",
        ("| QID | Tool | Status | Resp toks | Baseline toks | Reduction | "
         "P50 (ms) | P99 (ms) |"),
        "|-----|------|--------|-----------|---------------|-----------|---------|---------|",
    ]
    for r in results:
        reduction = f"{r.token_reduction_pct:.1f}%" if r.token_reduction_pct is not None else "—"
        lines.append(
            f"| {r.qid} | {r.tool} | {r.status} | {r.response_tokens} | "
            f"{r.baseline_tokens} | {reduction} | {r.p50_ms:.2f} | {r.p99_ms:.2f} |"
        )

    lines.extend(["", "## Aggregate — token reduction by tool", ""])
    tool_buckets: dict[str, list[float]] = {}
    for r in results:
        if r.token_reduction_pct is not None:
            tool_buckets.setdefault(r.tool, []).append(r.token_reduction_pct)
    lines.append("| Tool | Questions | Mean reduction | Min reduction |")
    lines.append("|------|-----------|----------------|---------------|")
    for tool, pcts in sorted(tool_buckets.items()):
        mean = sum(pcts) / len(pcts)
        lines.append(
            f"| {tool} | {len(pcts)} | {mean:.1f}% | {min(pcts):.1f}% |"
        )

    lines.extend(["", "## Aggregate — latency", ""])
    p50_all = [r.p50_ms for r in results if r.status == "ok"]
    p99_all = [r.p99_ms for r in results if r.status == "ok"]
    if p50_all:
        median_p50 = statistics.median(p50_all)
        max_p99 = max(p99_all)
        lines.append(f"- Median P50 across successful questions: **{median_p50:.2f} ms**")
        lines.append(f"- Max P99 across successful questions:   **{max_p99:.2f} ms**")

    lines.extend(["", "## Notes", ""])
    for r in results:
        for n in r.notes:
            lines.append(f"- {r.qid}: {n}")
    if not any(r.notes for r in results):
        lines.append("- (no per-question notes)")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(iterations: int = 100) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 2

    # Seed fixture corpus + index into a throwaway tenant.
    tmp_root = Path(tempfile.mkdtemp(prefix="accept_"))
    shutil.copytree(FIXTURES / "odoo_ce_subset", tmp_root / "odoo_ce_subset")
    shutil.copytree(FIXTURES / "custom_addons", tmp_root / "custom_addons")

    migrate_main(["--schema", "public", "--database-url", db_url])
    tenant = f"osm_accept_{uuid.uuid4().hex[:8]}"
    create_tenant_main([tenant, "--database-url", db_url])
    try:
        with psycopg.connect(db_url) as conn:
            run_index(
                addon_roots=[tmp_root / "odoo_ce_subset", tmp_root / "custom_addons"],
                conn=conn,
                tenant=tenant,
                git_sha="accept-fixture",
            )
            conn.commit()

        # Tenant-only context (no public-schema fallback). The normal
        # context_from_tenant() factory unions public + tenant so customer
        # overlays can extend a shared CE catalog; that does not fit this
        # harness. When the shared public schema is populated with a real
        # CE index its indexed_at_sha does not match the fixture's
        # "accept-fixture" sha, so effective_indexed_at_sha() collapses to
        # None across the cross-schema UNION and every handler raises
        # StaleIndexError. The accept suite is self-contained against the
        # fixture corpus indexed into the throwaway tenant, so the public
        # schema must be excluded from the query scope.
        ctx = TenantContext(tenant=validate_tenant(tenant), schemas=(tenant,))
        results: list[QuestionResult] = []
        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            for q in QUESTIONS:
                print(f"[accept] running {q.qid} ({q.tool}) ...", flush=True)
                results.append(_run_question(cur, ctx, q, iterations))

    finally:
        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{tenant}" CASCADE')
            conn.commit()
        shutil.rmtree(tmp_root, ignore_errors=True)

    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "phase-01-accept-raw.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2, sort_keys=True) + "\n"
    )
    (REPORTS / "phase-01-accept.md").write_text(
        _render_markdown(results, tenant, iterations)
    )
    print(f"wrote {REPORTS / 'phase-01-accept.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
