"""Calibration harness for find_examples rerank coefficients (Item A) and
_compute_risk thresholds (Item B).

Item A — test_rerank_coefficient_grid:
  Marker: ollama (skips without Ollama + qwen3-embedding-q5km + indexed data).
  Re-uses the 100-query Vi+En eval dataset from test_find_examples_recall.py.
  Grid-sweeps log_coeff x chain_boost, finds the best recall@5 combo, and
  asserts the best combo >= baseline recall.

Item B — test_risk_threshold_validation:
  No marker — pure-Python, runs in unit tests.
  Loads 25 synthetic incident cases from tests/eval/impact_analysis_incidents.json.
  Calls _compute_risk() directly with synthetic_counts; builds confusion matrix.
  Sweeps alternative HIGH/MED threshold pairs; computes macro-F1.
  Asserts current_F1 >= 0.70 (sanity floor) and best_F1 >= current_F1.

Re-tune protocol:
  Item A: pytest tests/test_calibration_eval.py::test_rerank_coefficient_grid -m ollama -v
  Item B: pytest tests/test_calibration_eval.py::test_risk_threshold_validation -v
"""
import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_EVAL_DIR = Path(__file__).parent / "eval"
_INCIDENTS_PATH = _EVAL_DIR / "impact_analysis_incidents.json"


def _setup_server_env() -> None:
    """Set test Neo4j env vars as defaults before importing server module."""
    os.environ.setdefault("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    os.environ.setdefault("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    os.environ.setdefault("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    sys.modules.pop("src.mcp.server", None)


# ---------------------------------------------------------------------------
# Item A — find_examples rerank coefficient grid sweep
# ---------------------------------------------------------------------------

def _extract_entities(result_text: str) -> list[str]:
    """Parse entity names from _find_examples output lines (same helper as recall test)."""
    return [
        line.split("·")[-1].strip()
        for line in result_text.splitlines()
        if line.startswith("#") and "·" in line
    ]


def _recall_at_k(result_entity_names: list[str], expected: str, k: int = 5) -> bool:
    return any(expected in name for name in result_entity_names[:k])


@pytest.mark.ollama
def test_rerank_coefficient_grid(live_connections):
    """Grid sweep of log_coeff x chain_boost over the 100-query Vi+En eval dataset.

    Markers: ollama — requires Ollama + qwen3-embedding-q5km + indexed Viindoo 17 data.

    The sweep measures recall@5 for each (log_coeff, chain_boost) combo.
    Because _find_examples inlines the scoring math, this test measures total
    end-to-end recall (cosine similarity + rerank) for each combo by swapping
    the module-level constants _RERANK_LOG_COEFF and _RERANK_CHAIN_BOOST
    (extracted from the inline literals at M7 refactor time — see server.py ~line 515).
    If those constants don't exist yet (pre-refactor), the test falls back to
    measuring baseline performance only and reports it for reference.

    Asserts: best recall@5 across all combos >= baseline recall (0.02, 0.20).
    Prints best combo + recall for orchestrator inspection.

    Re-tune: pytest tests/test_calibration_eval.py::test_rerank_coefficient_grid -m ollama -v
    """
    import src.mcp.server as srv
    from tests.test_find_examples_recall import (
        _EN_EVAL,  # noqa: PLC0415
        _VN_EVAL,  # noqa: PLC0415
    )

    driver, pg, embedder = live_connections

    LOG_COEFFS = [0.01, 0.02, 0.05, 0.10]
    CHAIN_BOOSTS = [0.10, 0.20, 0.30, 0.40]
    BASELINE = (0.02, 0.20)

    has_constants = hasattr(srv, "_RERANK_LOG_COEFF") and hasattr(srv, "_RERANK_CHAIN_BOOST")

    results: dict[tuple[float, float], float] = {}

    all_queries = list(_VN_EVAL) + list(_EN_EVAL)
    total = len(all_queries)

    def measure_recall(log_c: float, chain_b: float) -> float:
        """Measure recall@5 for the given coefficients.

        Uses try/finally to guarantee constant restoration even if
        srv._find_examples raises mid-sweep — otherwise tuned coefficients
        leak into subsequent test runs in the same module session.
        """
        orig_log = orig_chain = None
        if has_constants:
            orig_log = srv._RERANK_LOG_COEFF  # type: ignore[attr-defined]
            orig_chain = srv._RERANK_CHAIN_BOOST  # type: ignore[attr-defined]
            srv._RERANK_LOG_COEFF = log_c  # type: ignore[attr-defined]
            srv._RERANK_CHAIN_BOOST = chain_b  # type: ignore[attr-defined]
        try:
            hits: list[bool] = []
            for query, expected_entity, _ in all_queries:
                result = srv._find_examples(
                    query,
                    odoo_version="auto",
                    limit=5,
                    _driver=driver,
                    _pg_conn=pg,
                    _embedder=embedder,
                )
                entity_names = _extract_entities(result)
                hits.append(_recall_at_k(entity_names, expected_entity))
            return sum(hits) / total if total else 0.0
        finally:
            if has_constants:
                srv._RERANK_LOG_COEFF = orig_log  # type: ignore[attr-defined]
                srv._RERANK_CHAIN_BOOST = orig_chain  # type: ignore[attr-defined]

    # Measure baseline first
    baseline_recall = measure_recall(*BASELINE)
    results[BASELINE] = baseline_recall
    print(
        f"\nBaseline (log_coeff={BASELINE[0]}, chain_boost={BASELINE[1]}): "
        f"recall@5 = {baseline_recall:.4f}"
    )

    if has_constants:
        # Full grid sweep
        for log_c in LOG_COEFFS:
            for chain_b in CHAIN_BOOSTS:
                combo = (log_c, chain_b)
                if combo == BASELINE:
                    continue
                recall = measure_recall(log_c, chain_b)
                results[combo] = recall
                print(
                    f"  log_coeff={log_c:.2f}, chain_boost={chain_b:.2f}: "
                    f"recall@5 = {recall:.4f}"
                )

        best_combo = max(results, key=lambda k: results[k])
        best_recall = results[best_combo]
        print(
            f"\nBest combo: log_coeff={best_combo[0]}, chain_boost={best_combo[1]}"
            f" -> recall@5 = {best_recall:.4f}"
        )
        print(
            f"Baseline:   log_coeff={BASELINE[0]}, chain_boost={BASELINE[1]}"
            f" -> recall@5 = {baseline_recall:.4f}"
        )

        assert best_recall >= baseline_recall, (
            f"Best combo {best_combo} (recall@5={best_recall:.4f}) < "
            f"baseline {BASELINE} (recall@5={baseline_recall:.4f}). "
            "Check that all combos include baseline — grid may be broken."
        )
    else:
        # Constants not yet extracted — measure baseline only.
        # This is expected pre-refactor. Report findings for the orchestrator.
        print(
            "\nNote: src.mcp.server does not expose _RERANK_LOG_COEFF / "
            "_RERANK_CHAIN_BOOST as module-level constants. Grid sweep skipped "
            "— only baseline measured. Extract inline literals to module-level "
            "constants in server.py to enable full grid."
        )
        print(
            f"Baseline recall@5 = {baseline_recall:.4f} "
            f"(log_coeff={BASELINE[0]}, chain_boost={BASELINE[1]})"
        )
        best_combo = BASELINE
        best_recall = baseline_recall

    print(f"\nA best (log_coeff, chain_boost): {best_combo}")
    print(f"A recall@5: {best_recall:.4f}")


@pytest.fixture(scope="module")
def live_connections():
    """Open real Neo4j + PostgreSQL + Ollama connections (mirrors test_find_examples_recall)."""
    import psycopg2
    from neo4j import GraphDatabase
    from pgvector.psycopg2 import register_vector

    from src.indexer.embedder import Qwen3Embedder

    neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user = os.getenv("NEO4J_USER", "neo4j")
    neo4j_pass = os.getenv("NEO4J_PASSWORD", "password")
    pg_dsn = os.getenv(
        "PG_DSN",
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "qwen3-embedding-q5km")

    try:
        driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_pass))
        driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not reachable: {e}")

    try:
        conn = psycopg2.connect(pg_dsn)
        register_vector(conn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable: {e}")

    embedder = Qwen3Embedder(url=ollama_url, model=ollama_model, dim=1024, retries=1)
    try:
        embedder.embed(["ping"])
    except Exception as e:
        pytest.skip(f"Ollama not reachable or model not loaded: {e}")

    yield driver, conn, embedder

    driver.close()
    conn.close()


# ---------------------------------------------------------------------------
# Item B — risk threshold validation
# ---------------------------------------------------------------------------

def _load_incidents() -> list[dict]:
    """Load the curated incident dataset from tests/eval/impact_analysis_incidents.json."""
    with _INCIDENTS_PATH.open() as fh:
        return json.load(fh)


def _compute_risk_with_thresholds(
    view_count: int,
    method_count: int,
    js_count: int,
    high_threshold: int,
    med_threshold: int,
) -> str:
    """Replica of _compute_risk logic with configurable thresholds (for sweep)."""
    total = view_count + method_count + js_count
    if total >= high_threshold:
        return "HIGH"
    if total >= med_threshold:
        return "MEDIUM"
    return "LOW"


def _macro_f1(cases: list[dict], high_t: int, med_t: int) -> float:
    """Compute macro-averaged F1 over HIGH/MEDIUM/LOW classes."""
    classes = ["HIGH", "MEDIUM", "LOW"]
    tp: dict[str, int] = {c: 0 for c in classes}
    fp: dict[str, int] = {c: 0 for c in classes}
    fn: dict[str, int] = {c: 0 for c in classes}

    for case in cases:
        counts = case["synthetic_counts"]
        predicted = _compute_risk_with_thresholds(
            counts["view_count"],
            counts["method_count"],
            counts["js_count"],
            high_t,
            med_t,
        )
        expected = case["expected_severity"]
        if predicted == expected:
            tp[predicted] += 1
        else:
            fp[predicted] += 1
            fn[expected] += 1

    per_class_f1: list[float] = []
    for cls in classes:
        precision = tp[cls] / (tp[cls] + fp[cls]) if (tp[cls] + fp[cls]) > 0 else 0.0
        recall = tp[cls] / (tp[cls] + fn[cls]) if (tp[cls] + fn[cls]) > 0 else 0.0
        if precision + recall > 0:
            f1 = 2 * precision * recall / (precision + recall)
        else:
            f1 = 0.0
        per_class_f1.append(f1)

    return sum(per_class_f1) / len(per_class_f1)


def _confusion_matrix_str(cases: list[dict], high_t: int, med_t: int) -> str:
    """Return a human-readable confusion matrix string."""
    classes = ["HIGH", "MEDIUM", "LOW"]
    matrix: dict[str, dict[str, int]] = {c: {d: 0 for d in classes} for c in classes}
    for case in cases:
        counts = case["synthetic_counts"]
        predicted = _compute_risk_with_thresholds(
            counts["view_count"],
            counts["method_count"],
            counts["js_count"],
            high_t,
            med_t,
        )
        expected = case["expected_severity"]
        matrix[expected][predicted] += 1

    header = "        " + " ".join(f"{c:>8}" for c in classes) + "  (predicted)"
    rows = [header]
    for actual in classes:
        row_vals = " ".join(f"{matrix[actual][pred]:>8}" for pred in classes)
        rows.append(f"{actual:>8}: {row_vals}")
    rows.append("(actual)")
    return "\n".join(rows)


def test_risk_threshold_validation() -> None:
    """Validate _compute_risk() thresholds against 25 curated synthetic incidents.

    Pure-Python — no Neo4j, no Ollama, no markers needed.

    Protocol:
      1. Load tests/eval/impact_analysis_incidents.json (25 cases).
      2. Call _compute_risk(**synthetic_counts) per case; build confusion matrix.
      3. Sweep HIGH in {7, 10, 12, 15} x MED in {3, 4, 5, 6} — compute macro-F1.
      4. Assert current_F1 >= 0.70 (sanity floor).
      5. Assert best_F1 >= current_F1 (best combo is at least as good).
      6. Print recommendation if best != current (10, 4).

    Re-tune: pytest tests/test_calibration_eval.py::test_risk_threshold_validation -v
    """
    _setup_server_env()
    from src.mcp.server import _compute_risk  # noqa: PLC0415

    cases = _load_incidents()
    assert len(cases) >= 20, f"Expected >= 20 incident cases, got {len(cases)}"

    CURRENT_HIGH = 10
    CURRENT_MED = 4
    HIGH_CANDIDATES = [7, 10, 12, 15]
    MED_CANDIDATES = [3, 4, 5, 6]

    # Verify current _compute_risk against each case directly
    direct_misclassified = []
    for case in cases:
        counts = case["synthetic_counts"]
        predicted = _compute_risk(
            counts["view_count"],
            counts["method_count"],
            counts["js_count"],
        )
        if predicted != case["expected_severity"]:
            total = counts["view_count"] + counts["method_count"] + counts["js_count"]
            direct_misclassified.append(
                f"  {case['entity_name']}: expected={case['expected_severity']}, "
                f"predicted={predicted} (total={total})"
            )

    # Measure current thresholds
    current_f1 = _macro_f1(cases, CURRENT_HIGH, CURRENT_MED)
    current_cm = _confusion_matrix_str(cases, CURRENT_HIGH, CURRENT_MED)

    print(f"\nCurrent thresholds (HIGH>={CURRENT_HIGH}, MED>={CURRENT_MED}):")
    print(f"  macro-F1 = {current_f1:.4f}")
    print(f"Confusion matrix:\n{current_cm}")

    if direct_misclassified:
        print(f"Misclassified ({len(direct_misclassified)}/{len(cases)}):")
        print("\n".join(direct_misclassified))
    else:
        print(f"All {len(cases)} cases correctly classified.")

    # Grid sweep
    sweep_results: dict[tuple[int, int], float] = {}
    for h in HIGH_CANDIDATES:
        for m in MED_CANDIDATES:
            if m >= h:
                # Invalid combo (MED threshold must be < HIGH)
                continue
            sweep_results[(h, m)] = _macro_f1(cases, h, m)

    best_combo = max(sweep_results, key=lambda k: sweep_results[k])
    best_f1 = sweep_results[best_combo]

    print("\nThreshold sweep results (HIGH_threshold x MED_threshold -> macro-F1):")
    for (h, m), f1 in sorted(sweep_results.items(), key=lambda x: -x[1]):
        marker = " <- best" if (h, m) == best_combo else ""
        current_marker = " <- current" if (h, m) == (CURRENT_HIGH, CURRENT_MED) else ""
        print(f"  HIGH>={h:2d}, MED>={m}: macro-F1 = {f1:.4f}{marker}{current_marker}")

    print(f"\nB best (HIGH, MED) thresholds: {best_combo}")
    print(f"B macro-F1 (best): {best_f1:.4f}")
    print(
        f"B macro-F1 (current HIGH={CURRENT_HIGH}, MED={CURRENT_MED}): {current_f1:.4f}"
    )

    if best_combo != (CURRENT_HIGH, CURRENT_MED):
        best_cm = _confusion_matrix_str(cases, best_combo[0], best_combo[1])
        print(
            f"\nRECOMMENDATION: update _compute_risk thresholds to "
            f"HIGH>={best_combo[0]}, MED>={best_combo[1]} "
            f"(macro-F1 {best_f1:.4f} vs current {current_f1:.4f})"
        )
        print(f"Best confusion matrix:\n{best_cm}")
    else:
        print(
            f"\nCurrent thresholds (HIGH>={CURRENT_HIGH}, MED>={CURRENT_MED}) are optimal "
            f"vs candidates {HIGH_CANDIDATES} x {MED_CANDIDATES}."
        )

    # Sanity floor: current thresholds must achieve at least 0.70 macro-F1
    assert current_f1 >= 0.70, (
        f"Current thresholds (HIGH>={CURRENT_HIGH}, MED>={CURRENT_MED}) "
        f"macro-F1 = {current_f1:.4f} < 0.70. "
        "Dataset entries may be miscalibrated — revise synthetic_counts to "
        "match expected_severity, not the other way around. "
        "Misclassified cases above are the starting point."
    )

    # Best must be at least as good as current
    assert best_f1 >= current_f1, (
        f"best combo {best_combo} macro-F1={best_f1:.4f} < "
        f"current macro-F1={current_f1:.4f}. "
        "This should not happen since current combo is in the sweep. "
        "Check sweep logic."
    )
