# Smoke Test Automation — Approach A + C

> **Status:** ✓ DONE — Smoke automation shipped 2026-05-08

**Created**: 2026-05-08
**Goal**: Tự động hoá 3 manual smoke test (B1 indexer wiring, B2 lifecycle, Phase 0 v8/v9) bằng 2-tier strategy:
- **A** — Per-PR fast smoke với fixture nhỏ (~30s).
- **C** — Nightly real-source smoke với clone Odoo upstream + auto-issue on fail.

**Branch**: tạo branch mới `feat/smoke-automation-a-c`, mở PR khi xong.

**Triết lý**: Boil the Lake. Setup 1 lần, vĩnh viễn không phải hỏi "B1/B2 còn fix không?" sau N PR.

---

## Background — 8 file allow-list từ parser_odoo_core.py

```python
_CORE_FILES = (
    "odoo/tools/safe_eval.py",   # function
    "odoo/tools/query.py",        # query helper
    "odoo/tools/sql.py",          # query helper
    "odoo/fields.py",             # field_class
    "odoo/models.py",             # orm_class
    "odoo/api.py",                # function (decorators)
    "odoo/sql_db.py",             # query helper
    "odoo/exceptions.py",         # exception class
)
```

CoreSymbol kind enum cần fixture cover: `function`, `class_method`, `field_class`, `exception`, `orm_class`, `query_helper` (verify list trong `parser_odoo_core.py`).

---

## Approach A — Per-PR fixture smoke

### A1: Tạo fixture `tests/fixtures/odoo_core_min/` (8 file mini)

**Quyết định kỹ thuật**: viết MINI từ scratch theo Odoo idioms thay vì cherry-pick code GPL. Lý do:
1. Tránh drag GPL/LGPL vào test fixture (Viindoo repo có thể MIT-style).
2. Không bị rotting khi Odoo upstream refactor.
3. Đủ minimal để parser exercise, không cần production logic.

**Cấu trúc fixture**:
```
tests/fixtures/odoo_core_min/
└── odoo/
    ├── __init__.py
    ├── api.py              # ~60 LOC — @api.depends, @api.constrains, @api.model decorators
    ├── exceptions.py       # ~40 LOC — UserError, ValidationError, AccessError classes
    ├── fields.py           # ~80 LOC — Field base + Char, Integer, Many2one
    ├── models.py           # ~120 LOC — BaseModel, Model, TransientModel, AbstractModel
    ├── sql_db.py           # ~60 LOC — Cursor, ConnectionPool helpers
    └── tools/
        ├── __init__.py
        ├── safe_eval.py    # ~50 LOC — safe_eval, _BUILTINS, expr_eval
        ├── query.py        # ~40 LOC — Query, Select helpers
        └── sql.py          # ~30 LOC — pg_varchar, drop_view_if_exists
```

**Mỗi file PHẢI có**:
- ≥1 top-level class HOẶC function với docstring.
- ≥1 method (cho class).
- Decorators thật (`@api.depends`, `@property`, etc.) cho parser test.
- KHÔNG import/dep vào nhau cross-file (chỉ-stub) → parser chạy file-độc-lập được.

**Threshold expected** (verify local trước khi commit):
- `parse_odoo_core(fixture_root, '99.0')` → CoreSymbol ≥ 25 (8 file × ~3-5 symbol/file).
- Cover cả 6 kind enum.

### A2: Smoke test `tests/test_smoke_index_core.py`

```python
"""Per-PR smoke: index-core against mini fixture → assert counts."""
import json
from pathlib import Path

import pytest

from src.indexer.parser_odoo_core import parse_odoo_core
from src.indexer.parser_lint_rules import parse_lint_rules_for_version
from src.indexer.parser_cli import parse_cli_commands, parse_cli_flags
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = [pytest.mark.neo4j, pytest.mark.smoke]

SMOKE_VERSION = "99.0"  # isolated test version
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "odoo_core_min"


@pytest.fixture
def smoke_writer(neo4j_driver, clean_neo4j):
    """Fresh writer with cleaned DB."""
    return Neo4jWriter(neo4j_driver)


def test_smoke_parse_core_extracts_minimum_symbols():
    """8-file fixture → ≥25 CoreSymbols, all 6 kinds present."""
    symbols = parse_odoo_core(FIXTURE_ROOT, SMOKE_VERSION)
    assert len(symbols) >= 25, f"Expected ≥25 symbols, got {len(symbols)}"
    kinds = {s.kind for s in symbols}
    expected_kinds = {"function", "class_method", "field_class",
                       "exception", "orm_class", "query_helper"}
    assert expected_kinds.issubset(kinds), f"Missing kinds: {expected_kinds - kinds}"


def test_smoke_index_core_writes_to_neo4j(smoke_writer):
    """Full pipeline: parse → write → assert Neo4j counts."""
    # Core symbols
    symbols = parse_odoo_core(FIXTURE_ROOT, SMOKE_VERSION)
    smoke_writer.write_core_symbols(symbols, SMOKE_VERSION)

    # Lint rules from spec_data
    curate_status, rules = parse_lint_rules_for_version(SMOKE_VERSION)
    smoke_writer.write_lint_rules(rules, SMOKE_VERSION)

    # CLI from spec_data
    commands = parse_cli_commands(SMOKE_VERSION)
    smoke_writer.write_cli_commands(commands, SMOKE_VERSION)
    flags = parse_cli_flags(SMOKE_VERSION)
    smoke_writer.write_cli_flags(flags, SMOKE_VERSION)

    # Verify counts
    with smoke_writer.driver.session() as s:
        result = s.run("""
            MATCH (cs:CoreSymbol {odoo_version: $v}) WITH count(cs) AS cs
            MATCH (lr:LintRule {odoo_version: $v}) WITH cs, count(lr) AS lr
            MATCH (cc:CLICommand {odoo_version: $v}) WITH cs, lr, count(cc) AS cc
            MATCH (cf:CLIFlag {odoo_version: $v})
            RETURN cs, lr, cc, count(cf) AS cf
        """, v=SMOKE_VERSION).single()

    assert result["cs"] >= 25, f"CoreSymbol count {result['cs']} < 25"
    # spec_data 99.0 cần placeholder JSON với ≥1 entry mỗi loại (tạo trong A1).
    assert result["lr"] >= 1, f"LintRule count {result['lr']} = 0"
    assert result["cc"] >= 1, f"CLICommand count {result['cc']} = 0"
    assert result["cf"] >= 1, f"CLIFlag count {result['cf']} = 0"


def test_smoke_lifecycle_properties_set_on_diff(smoke_writer):
    """Index v17 fixture variant → v18 fixture variant → lifecycle props populated."""
    # Setup: 2 versions với 1 symbol khác nhau.
    # Use small inline fixtures (not full odoo_core_min) for diff test.
    # Refer to test_diff_engine_lifecycle.py shape — extend if needed.
    # ... (implementation: index v17, then v18 with one removed symbol,
    #      assert cs.removed_in = '18.0' on the v17 node)
    pass  # Stub — agent fills in based on existing test_diff_engine_lifecycle pattern
```

**Add `smoke` marker vào `pyproject.toml`**:
```toml
[tool.pytest.ini_options]
markers = [
    "neo4j: requires Neo4j connection",
    "postgres: requires PostgreSQL connection",
    "smoke: end-to-end smoke tests (per-PR fast tier)",
]
```

### A3: CI workflow update — add `smoke-tests` job

`.github/workflows/ci.yml` thêm job mới (parallel với integration-tests, dùng cùng Neo4j service pattern):

```yaml
  smoke-tests:
    runs-on: ubuntu-latest
    services:
      neo4j:
        image: neo4j:5.26.25
        env:
          NEO4J_AUTH: neo4j/password
        ports:
          - 7687:7687
        options: >-
          --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 12
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest tests/test_smoke_index_core.py -v -m "smoke" --tb=short
        env:
          NEO4J_TEST_URI: bolt://localhost:7687
          NEO4J_TEST_USER: neo4j
          NEO4J_TEST_PASSWORD: password
```

**Lý do tách job riêng (không merge vào integration-tests)**:
- Smoke fail = B1/B2 regression — cần signal độc lập, không lẫn với integration noise.
- Job tên `smoke-tests` đứng riêng trong PR check list — David nhìn nhanh.

### A4: Verify A xong
- Push branch + open PR.
- Confirm 4 CI checks: lint, unit-tests, integration-tests, **smoke-tests** — đều green.
- `make test-integration -k smoke` local cũng phải green.

**Commit chunks**:
1. `[ADD] tests/fixtures/odoo_core_min: minimal Odoo source for parser smoke (smoke A1)`
2. `[ADD] tests/test_smoke_index_core.py: per-PR smoke with fixture (smoke A2)`
3. `[ADD] ci: smoke-tests job + smoke pytest marker (smoke A3)`

---

## Approach C — Nightly real-source smoke

### C1: Workflow `.github/workflows/nightly-smoke.yml`

```yaml
name: Nightly Real-Source Smoke

on:
  schedule:
    # 19:00 UTC = 02:00 VN. Run sau khi GitHub least-busy.
    - cron: "0 19 * * *"
  workflow_dispatch: {}  # cho phép manual trigger để debug

jobs:
  smoke-real-odoo-17:
    runs-on: ubuntu-latest
    services:
      neo4j:
        image: neo4j:5.26.25
        env:
          NEO4J_AUTH: neo4j/password
        ports:
          - 7687:7687
        options: >-
          --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 12
      postgres:
        image: pgvector/pgvector:0.8.2-pg16
        env:
          POSTGRES_DB: odoo_semantic
          POSTGRES_USER: odoo_semantic
          POSTGRES_PASSWORD: password
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - run: pip install -e ".[dev]"

      - name: Bootstrap PG schema
        run: python -m src.db.migrate
        env:
          PG_DSN: postgresql://odoo_semantic:password@localhost:5432/odoo_semantic

      - name: Clone Odoo 17 (shallow)
        run: |
          git clone --depth=1 -b 17.0 https://github.com/odoo/odoo /tmp/odoo_17.0
          du -sh /tmp/odoo_17.0

      - name: Smoke v17 — index-core
        id: smoke_v17_core
        run: |
          python -m src.indexer index-core \
            --source /tmp/odoo_17.0 \
            --version 17.0 \
            2>&1 | tee /tmp/index-core-17.log
        env:
          NEO4J_TEST_URI: bolt://localhost:7687
          NEO4J_TEST_USER: neo4j
          NEO4J_TEST_PASSWORD: password

      - name: Smoke v17 — verify Neo4j counts
        run: |
          python -c "
          from neo4j import GraphDatabase
          d = GraphDatabase.driver('bolt://localhost:7687', auth=('neo4j','password'))
          with d.session() as s:
              cs = s.run(\"MATCH (cs:CoreSymbol {odoo_version:'17.0'}) RETURN count(cs) AS c\").single()['c']
              lr = s.run(\"MATCH (lr:LintRule {odoo_version:'17.0'}) RETURN count(lr) AS c\").single()['c']
              cc = s.run(\"MATCH (cc:CLICommand {odoo_version:'17.0'}) RETURN count(cc) AS c\").single()['c']
              cf = s.run(\"MATCH (cf:CLIFlag {odoo_version:'17.0'}) RETURN count(cf) AS c\").single()['c']
          print(f'v17 counts: CoreSymbol={cs} LintRule={lr} CLICommand={cc} CLIFlag={cf}')
          assert cs >= 400, f'CoreSymbol {cs} < 400 (B1 regression?)'
          assert lr >= 10, f'LintRule {lr} < 10'
          assert cc >= 10, f'CLICommand {cc} < 10'
          assert cf >= 50, f'CLIFlag {cf} < 50'
          print('v17 smoke PASS')
          "

  smoke-real-odoo-8:
    runs-on: ubuntu-latest
    services:
      neo4j:
        image: neo4j:5.26.25
        env:
          NEO4J_AUTH: neo4j/password
        ports:
          - 7687:7687
        options: >-
          --health-cmd "wget -q --spider http://localhost:7474 || exit 1"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 12
      postgres:
        image: pgvector/pgvector:0.8.2-pg16
        env:
          POSTGRES_DB: odoo_semantic
          POSTGRES_USER: odoo_semantic
          POSTGRES_PASSWORD: password
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5

    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: python -m src.db.migrate
        env:
          PG_DSN: postgresql://odoo_semantic:password@localhost:5432/odoo_semantic

      - name: Clone Odoo 8 (shallow)
        run: git clone --depth=1 -b 8.0 https://github.com/odoo/odoo /tmp/odoo_8.0

      - name: Register profile + repo
        run: |
          python -m src.manager add-profile odoo8 --version 8.0
          python -m src.manager add-repo --profile odoo8 \
            --url file:///tmp/odoo_8.0 --branch 8.0 --local-path /tmp/odoo_8.0
        env:
          PG_DSN: postgresql://odoo_semantic:password@localhost:5432/odoo_semantic

      - name: Smoke v8 — index-repo (Phase 0 unblock)
        run: python -m src.indexer index-repo --profile odoo8 --no-embed 2>&1 | tee /tmp/index-v8.log
        env:
          NEO4J_TEST_URI: bolt://localhost:7687
          NEO4J_TEST_USER: neo4j
          NEO4J_TEST_PASSWORD: password
          PG_DSN: postgresql://odoo_semantic:password@localhost:5432/odoo_semantic

      - name: Smoke v8 — verify Module + Method counts (Phase 0 + WI-F5)
        run: |
          python -c "
          from neo4j import GraphDatabase
          d = GraphDatabase.driver('bolt://localhost:7687', auth=('neo4j','password'))
          with d.session() as s:
              modules = s.run(\"MATCH (m:Module {odoo_version:'8.0'}) RETURN count(m) AS c\").single()['c']
              models = s.run(\"MATCH (m:Model {odoo_version:'8.0'}) RETURN count(m) AS c\").single()['c']
              methods = s.run(\"MATCH (m:Method {odoo_version:'8.0'}) RETURN count(m) AS c\").single()['c']
          print(f'v8 counts: Module={modules} Model={models} Method={methods}')
          assert modules >= 100, f'Module {modules} < 100 (Phase 0 ManifestFinder regression?)'
          assert models > 0, f'Model {models} = 0 (era1 _columns extract regression?)'
          assert methods > 0, f'Method {methods} = 0 (WI-F5 era1 method extract regression?)'
          print('v8 smoke PASS')
          "
```

### C2: Auto-issue on failure

Thêm job `report-failure` chạy `if: failure()` từ 2 job trên:

```yaml
  report-failure:
    needs: [smoke-real-odoo-17, smoke-real-odoo-8]
    if: failure()
    runs-on: ubuntu-latest
    permissions:
      issues: write
    steps:
      - name: Create issue on smoke failure
        uses: actions/github-script@v8
        with:
          script: |
            const today = new Date().toISOString().slice(0, 10);
            const runUrl = `https://github.com/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}`;
            github.rest.issues.create({
              owner: context.repo.owner,
              repo: context.repo.repo,
              title: `Nightly smoke fail ${today}`,
              body: `Nightly smoke against real Odoo 17 + Odoo 8 sources failed.\n\n` +
                    `**Run**: ${runUrl}\n\n` +
                    `Check the workflow logs to identify which assertion failed.\n\n` +
                    `Possible regressions:\n` +
                    `- B1 indexer wiring (CoreSymbol/LintRule/CLI counts < threshold)\n` +
                    `- Phase 0 ManifestFinder (v8 Module count = 0)\n` +
                    `- WI-F5 era1 method extract (v8 Method count = 0)\n` +
                    `- Upstream Odoo schema drift (file rename, allow-list outdated)`,
              labels: ['nightly-smoke', 'bug', 'priority:high']
            });
```

### C3: Manual trigger for testing

`workflow_dispatch: {}` cho phép David trigger thủ công qua `gh workflow run nightly-smoke.yml` để verify trước khi đợi cron đầu tiên.

### C4: Verify C xong
- `gh workflow run nightly-smoke.yml` từ branch.
- Confirm cả 2 job (v17 + v8) green trong run đầu.
- Test failure path bằng cách tạm break threshold (vd `cs >= 99999`) → verify issue được tạo. Sau đó revert.

**Commit chunks**:
4. `[ADD] ci: nightly-smoke workflow against real Odoo v17 + v8 (smoke C1+C2+C3)`

---

## Sequence

A1 → A2 → A3 → A4 → C1 → C2 → C3 → C4 → push → open PR → CI all green.

A và C có thể parallel sau A1 (fixture cần xong trước A2 + A3). Sonnet có thể implement tuần tự để đơn giản.

---

## Verification gate cuối

| Check | Expected |
|---|---|
| `make test` (unit) | Pass — không touch unit |
| `make test-integration` | Pass — không touch existing integration |
| `make test-integration -k smoke` | NEW — pass với fixture |
| `make lint` | clean |
| CI `smoke-tests` job | green trên PR |
| `gh workflow run nightly-smoke.yml` | green cả 2 job |
| Failure-path test (tạm break threshold rồi run) | issue được tạo → confirm permission đúng |

---

## Quy tắc thực thi

1. Tạo branch mới `feat/smoke-automation-a-c` từ `master` (đã sync sau merge PR #11).
2. Test-first: viết smoke test trước khi viết fixture content (dùng skip + xfail trong process).
3. Commit từng chunk như spec ở mỗi phase.
4. Format commit `[ADD|FIX|IMP] <scope>: <summary> (smoke <Ax|Cx>)`.
5. KHÔNG `Co-Authored-By: Claude` trailer.
6. KHÔNG suppress test/lint.
7. KHÔNG drag GPL Odoo source vào fixture — viết mini từ scratch.
8. Sau A4: commit + push lên branch, để C dùng cùng branch (1 PR thôi).
9. Sau C4: push final, mở PR, request David review.

---

## Output handoff cho David

Sau khi push xong + PR mở:
- Link PR.
- 4 CI checks status (lint, unit, integration, smoke-tests).
- 1 nightly-smoke manual run status (qua `gh run view`).
- Failure-path test result (đã verify issue creation work).
- Số commit, số test mới, số LOC delta.
