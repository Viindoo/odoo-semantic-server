"""Extract (docstring, method body) pairs from an addon fixture for embedding benchmarks.

Walks one or more addon roots, parses every ``*.py`` file with stdlib ``ast``,
and emits a JSONL record per function/method that has a docstring of at least
``--min-docstring-chars`` characters and a body of at least ``--min-body-lines``
non-trivial lines. Non-trivial means anything other than bare ``pass``,
``...``, or the docstring itself.

Output record::

    {
        "id": "<sequential int>",
        "module": "product",
        "file": "product/models/product_template.py",
        "qualname": "ProductTemplate._compute_display_name",
        "line": 421,
        "docstring": "...",
        "body": "def _compute_display_name(self):\\n    ..."
    }

Usage::

    python scripts/bench_corpus.py \\
        --addons tests/fixtures/odoo_ce_subset \\
        --out /tmp/embed-spike/corpus.jsonl

Pure stdlib — safe to run without ``uv sync``.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_TRIVIAL_BODY_NODES = (ast.Pass, ast.Expr)


@dataclass(frozen=True)
class MethodRecord:
    id: int
    module: str
    file: str
    qualname: str
    line: int
    docstring: str
    body: str


def _count_meaningful_body_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Count body statements that are not the docstring, not ``pass``, not ``...``."""
    count = 0
    for i, stmt in enumerate(node.body):
        # Skip docstring at position 0
        if (
            i == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        # Skip `...` (Ellipsis) and bare pass
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            continue
        count += 1
    return count


def _iter_functions(
    tree: ast.Module,
    parent_qual: str = "",
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Yield (qualname, node) for every function/method at any depth."""
    for node in tree.body:
        yield from _walk_node(node, parent_qual)


def _walk_node(
    node: ast.AST,
    parent_qual: str,
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        qual = f"{parent_qual}.{node.name}" if parent_qual else node.name
        yield qual, node
        for child in node.body:
            yield from _walk_node(child, qual)
    elif isinstance(node, ast.ClassDef):
        qual = f"{parent_qual}.{node.name}" if parent_qual else node.name
        for child in node.body:
            yield from _walk_node(child, qual)


def _module_name_for(file_path: Path, addon_root: Path) -> str:
    rel = file_path.relative_to(addon_root)
    # First path component under the addon root is the module name.
    return rel.parts[0]


def extract_from_file(
    file_path: Path,
    addon_root: Path,
    source: str,
    start_id: int,
    min_doc_chars: int,
    min_body_lines: int,
) -> tuple[list[MethodRecord], int]:
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return [], start_id

    module = _module_name_for(file_path, addon_root)
    rel_file = str(file_path.relative_to(addon_root.parent))
    source_lines = source.splitlines()

    out: list[MethodRecord] = []
    next_id = start_id
    for qualname, func in _iter_functions(tree):
        docstring = ast.get_docstring(func)
        if not docstring or len(docstring) < min_doc_chars:
            continue
        if _count_meaningful_body_lines(func) < min_body_lines:
            continue

        # Extract source lines covering the whole def incl. decorators.
        start = (
            min((d.lineno for d in func.decorator_list), default=func.lineno) - 1
        )
        end = func.end_lineno or func.lineno
        body_src = "\n".join(source_lines[start:end])

        out.append(
            MethodRecord(
                id=next_id,
                module=module,
                file=rel_file,
                qualname=qualname,
                line=func.lineno,
                docstring=docstring.strip(),
                body=body_src,
            )
        )
        next_id += 1
    return out, next_id


def extract(
    addon_roots: list[Path],
    min_doc_chars: int,
    min_body_lines: int,
) -> list[MethodRecord]:
    records: list[MethodRecord] = []
    next_id = 0
    for root in addon_roots:
        if not root.is_dir():
            raise SystemExit(f"error: addon root {root} not a directory")
        for py in sorted(root.rglob("*.py")):
            try:
                source = py.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            new, next_id = extract_from_file(
                py, root, source, next_id, min_doc_chars, min_body_lines
            )
            records.extend(new)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addons",
        action="append",
        required=True,
        help="Addon root directory; repeat flag for multiple roots.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output JSONL path.",
    )
    parser.add_argument(
        "--min-docstring-chars",
        type=int,
        default=20,
        help="Minimum docstring length (default: 20).",
    )
    parser.add_argument(
        "--min-body-lines",
        type=int,
        default=3,
        help="Minimum non-trivial body statements (default: 3).",
    )
    args = parser.parse_args(argv)

    roots = [Path(a).resolve() for a in args.addons]
    records = extract(roots, args.min_docstring_chars, args.min_body_lines)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(
                json.dumps(
                    {
                        "id": r.id,
                        "module": r.module,
                        "file": r.file,
                        "qualname": r.qualname,
                        "line": r.line,
                        "docstring": r.docstring,
                        "body": r.body,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    by_mod: dict[str, int] = {}
    for r in records:
        by_mod[r.module] = by_mod.get(r.module, 0) + 1
    print(f"wrote {len(records)} records to {args.out}")
    for mod, n in sorted(by_mod.items(), key=lambda kv: -kv[1]):
        print(f"  {mod:<20} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
