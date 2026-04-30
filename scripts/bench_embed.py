"""Embedding self-retrieval benchmark on an extracted corpus.

Given a JSONL corpus produced by ``scripts/bench_corpus.py`` (records with
``docstring`` + ``body`` + ``id``), this script:

1. Loads a ``sentence-transformers`` model on CUDA (or CPU with ``--device cpu``).
2. Embeds every ``body`` (corpus) with ``batch_size``, measuring encode
   throughput and GPU peak VRAM.
3. Embeds every ``docstring`` as a query and performs cosine top-k lookup
   against the corpus.
4. Reports Recall@{1,5,10}, MRR, per-query latency (P50/P95, warm), corpus
   encode throughput, VRAM peak, and model disk size.

Usage::

    # On a host with CUDA + torch + sentence-transformers in a venv:
    source /path/to/embed-venv/bin/activate
    python scripts/bench_embed.py \\
        --corpus /tmp/embed-spike/corpus.jsonl \\
        --model BAAI/bge-code-v1 \\
        --out /tmp/embed-spike/results/bge-code-v1.json

Dry-run mode (no model load — lets you verify CLI + corpus on a machine
without a GPU)::

    python scripts/bench_embed.py --corpus /tmp/embed-spike/corpus.jsonl \\
        --dry-run

Deps when NOT dry-run: ``sentence-transformers``, ``torch``. Install in a
separate venv; not added to ``pyproject.toml`` because this is a one-off
spike, not a runtime dependency.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Record:
    id: int
    docstring: str
    body: str
    module: str
    qualname: str


@dataclass
class BenchResult:
    model: str
    device: str
    corpus_size: int
    embedding_dim: int
    # Quality
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    mrr: float
    # Performance
    corpus_encode_seconds: float
    corpus_encode_throughput: float  # items/sec
    query_latency_p50_ms: float
    query_latency_p95_ms: float
    query_latency_mean_ms: float
    # Resources
    vram_peak_mb: float
    model_disk_size_mb: float
    # Context
    max_seq_length: int
    truncated_bodies: int
    truncated_queries: int
    batch_size: int
    warmup_iters: int
    measure_iters: int
    extras: dict[str, Any] = field(default_factory=dict)


def load_corpus(path: Path) -> list[Record]:
    out: list[Record] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out.append(
                Record(
                    id=r["id"],
                    docstring=r["docstring"],
                    body=r["body"],
                    module=r["module"],
                    qualname=r["qualname"],
                )
            )
    return out


def _approximate_tokens(text: str) -> int:
    """Rough token count proxy (1 token ≈ 4 chars).

    Only used to flag truncation risk — real tokenizer call is owned by the
    model during encode.
    """
    return max(1, len(text) // 4)


def _model_disk_size_mb(model_name: str) -> float:
    """Size of cached model on disk, in MB. Returns 0 if not yet cached."""
    from huggingface_hub import scan_cache_dir  # type: ignore[import-untyped]

    try:
        info = scan_cache_dir()
    except Exception:
        return 0.0
    total = 0
    for repo in info.repos:
        if repo.repo_id == model_name:
            total += repo.size_on_disk
    return total / (1024 * 1024)


def run_bench(
    corpus: list[Record],
    model_name: str,
    device: str,
    batch_size: int,
    warmup_iters: int,
    measure_iters: int,
    max_seq_length: int | None,
) -> BenchResult:
    import torch  # type: ignore[import-not-found]
    from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]

    if device == "cuda":
        if not torch.cuda.is_available():
            raise SystemExit("error: --device cuda but torch.cuda.is_available() is False")
        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()

    print(f"loading {model_name} on {device} ...", flush=True)
    load_start = time.perf_counter()
    model = SentenceTransformer(model_name, device=device, trust_remote_code=True)
    load_seconds = time.perf_counter() - load_start
    native_max_seq = int(getattr(model, "max_seq_length", 0) or 0)
    if max_seq_length is not None:
        model.max_seq_length = max_seq_length
    max_seq = int(getattr(model, "max_seq_length", 0) or 0)
    print(
        f"  loaded in {load_seconds:.1f}s  "
        f"max_seq_length={max_seq} (native={native_max_seq})",
        flush=True,
    )

    bodies = [r.body for r in corpus]
    queries = [r.docstring for r in corpus]

    truncated_bodies = sum(1 for b in bodies if _approximate_tokens(b) > max_seq)
    truncated_queries = sum(1 for q in queries if _approximate_tokens(q) > max_seq)

    # --- Corpus encode + throughput ---
    print(f"encoding {len(bodies)} bodies (batch={batch_size}) ...", flush=True)
    if device == "cuda":
        torch.cuda.synchronize()
    enc_start = time.perf_counter()
    body_emb = model.encode(
        bodies,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    if device == "cuda":
        torch.cuda.synchronize()
    enc_seconds = time.perf_counter() - enc_start
    throughput = len(bodies) / enc_seconds if enc_seconds > 0 else 0.0
    embedding_dim = int(body_emb.shape[1])
    print(
        f"  corpus encoded in {enc_seconds:.2f}s "
        f"({throughput:.1f} items/s, dim={embedding_dim})",
        flush=True,
    )

    # --- Query encode (batched, for recall evaluation) ---
    print(f"encoding {len(queries)} queries ...", flush=True)
    if device == "cuda":
        torch.cuda.synchronize()
    q_start = time.perf_counter()
    query_emb = model.encode(
        queries,
        batch_size=batch_size,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    if device == "cuda":
        torch.cuda.synchronize()
    q_enc_seconds = time.perf_counter() - q_start
    print(f"  queries encoded in {q_enc_seconds:.2f}s", flush=True)

    # --- Recall / MRR ---
    # cos sim = dot product because normalize_embeddings=True
    # shape: (N_queries, N_bodies)
    import numpy as np  # type: ignore[import-not-found]

    sims = query_emb @ body_emb.T
    topk_idx = np.argsort(-sims, axis=1)[:, :10]

    hits_at_1 = 0
    hits_at_5 = 0
    hits_at_10 = 0
    reciprocal_ranks: list[float] = []
    for q_idx, _record in enumerate(corpus):
        gold = q_idx  # self-retrieval: body at same index is the gold hit
        topk = topk_idx[q_idx]
        rank = None
        for position, body_idx in enumerate(topk, start=1):
            if body_idx == gold:
                rank = position
                break
        if rank == 1:
            hits_at_1 += 1
        if rank is not None and rank <= 5:
            hits_at_5 += 1
        if rank is not None and rank <= 10:
            hits_at_10 += 1
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

    n = len(corpus)
    recall_at_1 = hits_at_1 / n
    recall_at_5 = hits_at_5 / n
    recall_at_10 = hits_at_10 / n
    mrr = sum(reciprocal_ranks) / n

    # --- Per-query latency (warm) ---
    print(
        f"measuring per-query latency (warmup={warmup_iters}, measure={measure_iters}) ...",
        flush=True,
    )
    sample_queries = queries[: min(50, len(queries))]
    # Warmup
    for i in range(warmup_iters):
        model.encode(
            [sample_queries[i % len(sample_queries)]],
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
    if device == "cuda":
        torch.cuda.synchronize()
    # Measure
    latencies_ms: list[float] = []
    for i in range(measure_iters):
        q = sample_queries[i % len(sample_queries)]
        if device == "cuda":
            torch.cuda.synchronize()
        t0 = time.perf_counter()
        model.encode(
            [q],
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        if device == "cuda":
            torch.cuda.synchronize()
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_sorted = sorted(latencies_ms)
    p50 = statistics.median(latencies_sorted)
    p95 = latencies_sorted[int(0.95 * len(latencies_sorted))]
    mean = statistics.mean(latencies_sorted)

    # --- VRAM ---
    vram_peak_mb = 0.0
    if device == "cuda":
        vram_peak_mb = torch.cuda.max_memory_allocated() / (1024 * 1024)

    disk_mb = _model_disk_size_mb(model_name)

    return BenchResult(
        model=model_name,
        device=device,
        corpus_size=n,
        embedding_dim=embedding_dim,
        recall_at_1=recall_at_1,
        recall_at_5=recall_at_5,
        recall_at_10=recall_at_10,
        mrr=mrr,
        corpus_encode_seconds=enc_seconds,
        corpus_encode_throughput=throughput,
        query_latency_p50_ms=p50,
        query_latency_p95_ms=p95,
        query_latency_mean_ms=mean,
        vram_peak_mb=vram_peak_mb,
        model_disk_size_mb=disk_mb,
        max_seq_length=max_seq,
        truncated_bodies=truncated_bodies,
        truncated_queries=truncated_queries,
        batch_size=batch_size,
        warmup_iters=warmup_iters,
        measure_iters=measure_iters,
        extras={
            "load_seconds": load_seconds,
            "query_encode_seconds": q_enc_seconds,
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        required=True,
        help="Path to corpus.jsonl (output of bench_corpus.py).",
    )
    parser.add_argument(
        "--model",
        help="HuggingFace model id (e.g. BAAI/bge-code-v1). Required unless --dry-run.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output JSON path. Required unless --dry-run.",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        choices=("cuda", "cpu"),
        help="Device (default: cuda).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="Encode batch size (default: 8).",
    )
    parser.add_argument(
        "--max-seq-length",
        type=int,
        default=2048,
        help=(
            "Cap model.max_seq_length (default: 2048). "
            "Applied uniformly across models for fair comparison. "
            "Pass 0 to keep the model's native max."
        ),
    )
    parser.add_argument(
        "--warmup-iters",
        type=int,
        default=20,
        help="Per-query latency warmup iterations (default: 20).",
    )
    parser.add_argument(
        "--measure-iters",
        type=int,
        default=100,
        help="Per-query latency measurement iterations (default: 100).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load corpus only, print stats, skip model.",
    )
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    if not corpus:
        raise SystemExit(f"error: empty corpus {args.corpus}")
    print(f"loaded {len(corpus)} records from {args.corpus}", flush=True)

    if args.dry_run:
        avg_body = sum(len(r.body) for r in corpus) / len(corpus)
        avg_doc = sum(len(r.docstring) for r in corpus) / len(corpus)
        print(f"avg body chars = {avg_body:.0f}, avg docstring chars = {avg_doc:.0f}")
        print("dry-run: no model loaded.")
        return 0

    if not args.model or not args.out:
        raise SystemExit("error: --model and --out are required unless --dry-run")

    result = run_bench(
        corpus=corpus,
        model_name=args.model,
        device=args.device,
        batch_size=args.batch_size,
        warmup_iters=args.warmup_iters,
        measure_iters=args.measure_iters,
        max_seq_length=args.max_seq_length if args.max_seq_length > 0 else None,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")

    print()
    print(f"=== {result.model} ({result.device}) ===")
    print(f"  recall@1  = {result.recall_at_1 * 100:.1f}%")
    print(f"  recall@5  = {result.recall_at_5 * 100:.1f}%")
    print(f"  recall@10 = {result.recall_at_10 * 100:.1f}%")
    print(f"  MRR       = {result.mrr:.3f}")
    print(f"  VRAM peak = {result.vram_peak_mb:.0f} MB")
    print(f"  disk      = {result.model_disk_size_mb:.0f} MB")
    print(f"  dim       = {result.embedding_dim}")
    print(f"  max_seq   = {result.max_seq_length}")
    print(
        f"  truncated bodies/queries = "
        f"{result.truncated_bodies}/{result.truncated_queries}  (approx)"
    )
    print(
        f"  corpus encode: {result.corpus_encode_seconds:.2f}s "
        f"({result.corpus_encode_throughput:.1f} items/s)"
    )
    print(
        f"  per-query latency: P50={result.query_latency_p50_ms:.1f}ms "
        f"P95={result.query_latency_p95_ms:.1f}ms "
        f"mean={result.query_latency_mean_ms:.1f}ms"
    )
    print(f"  saved → {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
