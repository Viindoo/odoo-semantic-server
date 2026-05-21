# SPDX-License-Identifier: AGPL-3.0-or-later
"""Script chạy indexer với ~/git, version 17.0, để test E2E."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src.indexer.scanner import scan_repos
from src.indexer.registry import build_registry
from src.indexer.resolver import topological_sort
from src.indexer.parser_python import parse_module
from src.indexer.writer_neo4j import Neo4jWriter

BASE_DIRS = [os.path.expanduser("~/git")]
TARGET_VERSION = "17.0"

print("1. Scanning repos...")
repo_pairs = scan_repos(BASE_DIRS)
versioned = [(p, v) for p, v in repo_pairs if v == TARGET_VERSION]
print(f"   Found {len(versioned)} repos for version {TARGET_VERSION}")

print("2. Building registry...")
registry = build_registry(versioned)
modules_17 = registry.get(TARGET_VERSION, {})
print(f"   Found {len(modules_17)} modules")

print("3. Topological sort...")
order = topological_sort(modules_17)
print(f"   Sort order: {len(order)} modules")

print("4. Parsing Python files...")
results = []
for i, module_name in enumerate(order):
    module_info = modules_17[module_name]
    result = parse_module(module_info)
    results.append(result)
    if i % 50 == 0:
        print(f"   Parsed {i}/{len(order)} modules...")

print(f"5. Writing to Neo4j ({len(results)} modules)...")
writer = Neo4jWriter(
    uri=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    user=os.getenv("NEO4J_USER", "neo4j"),
    password=os.getenv("NEO4J_PASSWORD", "password"),
)
writer.setup_indexes()
writer.write_results(results)
writer.close()
print("Done! Neo4j populated.")
