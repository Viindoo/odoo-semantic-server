# Milestone 1 — "First Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Nguyên tắc bắt buộc khi implement:**
> - **Boil the Lake:** Không hardcode version, không giả định single-repo, không bỏ qua edge case. Làm đúng ngay từ đầu.
> - **Ship Wow Product:** Output của MCP tools phải dễ đọc, có cấu trúc cây rõ ràng. AI client đọc được ngay, không cần parse thêm.

**Goal:** `resolve_model("account.move", "17.0")` trả về đúng full cross-repo inheritance chain, kết nối được từ VS Code / Claude Code.

**Architecture:** Scanner quét `~/git` → Registry Builder build module map per version → Dep Resolver topo-sort → Python AST Parser → Neo4j Writer → MCP Server expose 3 tools qua HTTP FastMCP.

**Tech Stack:** Python 3.12+, fastmcp, neo4j (Python driver), psycopg2-binary, python-dotenv, pytest.

---

## Cấu Trúc File

```
odoo-semantic-mcp/
├── docker-compose.yml          -- Neo4j + PostgreSQL/pgvector
├── .env.example                -- biến môi trường cần thiết
├── pyproject.toml              -- dependencies
├── src/
│   ├── __init__.py
│   ├── indexer/
│   │   ├── __init__.py
│   │   ├── models.py           -- dataclasses dùng chung (ModuleInfo, ModelInfo, ...)
│   │   ├── scanner.py          -- quét git repos, phát hiện odoo_version từ branch
│   │   ├── registry.py         -- đọc manifest, build {version: {module: ModuleInfo}}
│   │   ├── resolver.py         -- topological sort dependency DAG
│   │   ├── parser_python.py    -- Python AST parser: models/fields/methods
│   │   └── writer_neo4j.py     -- ghi nodes + edges vào Neo4j
│   └── mcp/
│       ├── __init__.py
│       └── server.py           -- FastMCP server: resolve_model/field/method
└── tests/
    ├── conftest.py             -- fixtures: neo4j_driver, clean_neo4j, git repo helpers
    ├── test_scanner.py
    ├── test_registry.py
    ├── test_resolver.py
    ├── test_parser_python.py
    ├── test_writer_neo4j.py
    └── test_mcp_server.py
```

---

## Task 1: Infrastructure

**Files:**
- Tạo: `docker-compose.yml`
- Tạo: `.env.example`
- Tạo: `pyproject.toml`
- Tạo: `src/__init__.py`, `src/indexer/__init__.py`, `src/mcp/__init__.py`
- Tạo: `tests/conftest.py`

- [ ] **Bước 1: Tạo docker-compose.yml**

```yaml
# docker-compose.yml — DB TIER ONLY
# Chạy file này trên DB server (hoặc cùng server với app khi dev).
# App tier kết nối qua NEO4J_URI và PG_DSN trong .env — không cần sửa code.
services:
  neo4j:
    image: neo4j:5
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-password}
      # Khi Neo4j chạy trên server riêng, phải set advertised address
      # để bolt client redirect đúng host (không bị redirect về container hostname):
      NEO4J_server_bolt_advertised__address: ${NEO4J_ADVERTISED_HOST:-localhost}:7687
    ports:
      - "127.0.0.1:7474:7474"  # Browser UI — localhost only, không expose ra ngoài
      - "7687:7687"             # Bolt — cần accessible từ app server
    volumes:
      - neo4j_data:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p $${NEO4J_PASSWORD:-password} 'RETURN 1' || exit 1"]
      interval: 10s
      retries: 10

  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: odoo_semantic
      POSTGRES_USER: odoo_semantic
      POSTGRES_PASSWORD: ${PG_PASSWORD:-password}
    ports:
      - "5432:5432"             # Cần accessible từ app server
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U odoo_semantic"]
      interval: 10s
      retries: 5

volumes:
  neo4j_data:
  pg_data:
```

- [ ] **Bước 2: Tạo .env.example**

```bash
# .env.example
# Thay [db-server] bằng IP/hostname của DB server khi tách tier.
# Khi chạy all-in-one, giữ nguyên localhost.

# ── DB TIER — Neo4j ──────────────────────────────────────────────────
NEO4J_URI=bolt://localhost:7687
# Tách DB tier:  NEO4J_URI=bolt://192.168.1.10:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=                          # bắt buộc điền

# Chỉ cần set khi Neo4j chạy trên server riêng (xem docker-compose.yml):
# NEO4J_ADVERTISED_HOST=192.168.1.10

# ── DB TIER — PostgreSQL ─────────────────────────────────────────────
PG_DSN=postgresql://odoo_semantic:password@localhost:5432/odoo_semantic
# Tách DB tier:  PG_DSN=postgresql://odoo_semantic:password@192.168.1.10:5432/odoo_semantic
PG_PASSWORD=                             # bắt buộc điền

# ── APP TIER ─────────────────────────────────────────────────────────
ODOO_REPOS_BASE_DIR=/home/user/git

MCP_HOST=0.0.0.0
MCP_PORT=8002

# ── TEST (pytest integration tests) ─────────────────────────────────���
NEO4J_TEST_URI=bolt://localhost:7687
NEO4J_TEST_USER=neo4j
NEO4J_TEST_PASSWORD=password
```

- [ ] **Bước 3: Tạo pyproject.toml**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "odoo-semantic-mcp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastmcp>=2.3,<3.0",
    "neo4j>=5.0,<6.0",
    "psycopg2-binary>=2.9",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "neo4j: integration tests yêu cầu Neo4j đang chạy (skip bằng -m 'not neo4j')",
]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I"]
```

- [ ] **Bước 4: Tạo package init files**

```bash
touch src/__init__.py src/indexer/__init__.py src/mcp/__init__.py
```

- [ ] **Bước 5: Tạo tests/conftest.py**

```python
# tests/conftest.py
import os
import subprocess
import pytest
from pathlib import Path
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_TEST_PASSWORD", "password")

TEST_VERSION = "99.0"  # version riêng cho test, không ảnh hưởng data thật


def pytest_configure(config):
    config.addinivalue_line("markers", "neo4j: yêu cầu Neo4j đang chạy")


@pytest.fixture(scope="session")
def neo4j_driver():
    """Kết nối Neo4j. Skip toàn bộ test neo4j nếu không kết nối được."""
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j không sẵn sàng ({e}) — chạy 'docker compose up -d neo4j' để enable")
    yield driver
    driver.close()


@pytest.fixture
def clean_neo4j(neo4j_driver):
    """Xóa test data trước và sau mỗi test."""
    def cleanup():
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                        v=TEST_VERSION)
    cleanup()
    yield neo4j_driver
    cleanup()


def make_git_repo(path: Path, branch: str) -> Path:
    """Tạo git repo với 1 commit trên branch chỉ định."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", branch],
                   cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("test repo")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run([
        "git", "-c", "user.email=test@test.com",
        "-c", "user.name=Test", "commit", "-m", "init"
    ], cwd=path, check=True, capture_output=True)
    return path


def make_manifest(module_dir: Path, name: str, version: str, depends: list[str],
                  installable: bool = True) -> Path:
    """Tạo __manifest__.py cho một module."""
    module_dir.mkdir(parents=True, exist_ok=True)
    content = f"""{{
    'name': '{name}',
    'version': '{version}',
    'depends': {depends!r},
    'installable': {installable},
}}
"""
    (module_dir / "__manifest__.py").write_text(content)
    return module_dir
```

- [ ] **Bước 6: Khởi động services**

```bash
cp .env.example .env
docker compose up -d
docker compose ps   # đảm bảo cả 2 services healthy
```

Expected: `neo4j` và `postgres` ở trạng thái `healthy`.

- [ ] **Bước 7: Cài dependencies**

```bash
pip install -e ".[dev]"
```

- [ ] **Bước 8: Commit**

```bash
git add docker-compose.yml .env.example pyproject.toml src/ tests/conftest.py
git commit -m "feat: infrastructure setup (docker-compose, pyproject, test fixtures)"
```

---

## Task 2: Data Models

**Files:**
- Tạo: `src/indexer/models.py`

- [ ] **Bước 1: Viết test để xác nhận data model structure**

```python
# tests/test_models.py
from src.indexer.models import ModuleInfo, FieldInfo, MethodInfo, ModelInfo, ParseResult


def test_module_info_creation():
    m = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/git/odoo_17.0/sale", depends=["base", "account"],
        version_raw="17.0.1.0.0",
    )
    assert m.name == "sale"
    assert m.odoo_version == "17.0"
    assert "base" in m.depends


def test_model_info_defaults():
    model = ModelInfo(name="sale.order", module="sale", odoo_version="17.0")
    assert model.is_abstract is False
    assert model.is_transient is False
    assert model.inherit == []
    assert model.inherits == {}
    assert model.fields == []
    assert model.methods == []


def test_parse_result_creation():
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path="/tmp", depends=[], version_raw="",
    )
    result = ParseResult(module=module)
    assert result.models == []
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_models.py -v
```

Expected: `ModuleNotFoundError: No module named 'src'`

- [ ] **Bước 3: Implement src/indexer/models.py**

```python
# src/indexer/models.py
from dataclasses import dataclass, field


@dataclass
class ModuleInfo:
    name: str
    odoo_version: str
    repo: str
    path: str
    depends: list[str]
    version_raw: str = ""


@dataclass
class FieldInfo:
    name: str
    ttype: str
    related: str | None = None
    compute: str | None = None
    stored: bool = True
    required: bool = False


@dataclass
class MethodInfo:
    name: str
    has_super_call: bool = False
    decorators: list[str] = field(default_factory=list)


@dataclass
class ModelInfo:
    name: str
    module: str
    odoo_version: str
    is_abstract: bool = False
    is_transient: bool = False
    inherit: list[str] = field(default_factory=list)
    inherits: dict[str, str] = field(default_factory=dict)
    fields: list[FieldInfo] = field(default_factory=list)
    methods: list[MethodInfo] = field(default_factory=list)


@dataclass
class ParseResult:
    module: ModuleInfo
    models: list[ModelInfo] = field(default_factory=list)
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_models.py -v
```

Expected: 3 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/models.py tests/test_models.py
git commit -m "feat: data models (ModuleInfo, ModelInfo, FieldInfo, MethodInfo, ParseResult)"
```

---

## Task 3: Scanner

**Files:**
- Tạo: `src/indexer/scanner.py`
- Tạo: `tests/test_scanner.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_scanner.py
import subprocess
import pytest
from pathlib import Path
from tests.conftest import make_git_repo
from src.indexer.scanner import get_git_branch, is_odoo_version_branch, scan_repos


def test_get_git_branch_returns_version(tmp_path):
    repo = make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    assert get_git_branch(str(repo)) == "17.0"


def test_get_git_branch_returns_none_for_non_repo(tmp_path):
    assert get_git_branch(str(tmp_path / "not_a_repo")) is None


def test_is_odoo_version_branch():
    assert is_odoo_version_branch("17.0") is True
    assert is_odoo_version_branch("8.0") is True
    assert is_odoo_version_branch("19.0") is True
    assert is_odoo_version_branch("main") is False
    assert is_odoo_version_branch("feature/foo") is False
    assert is_odoo_version_branch("") is False


def test_scan_repos_finds_versioned_subdirs(tmp_path):
    make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    make_git_repo(tmp_path / "odoo_16.0", "16.0")
    results = scan_repos([str(tmp_path)])
    versions = {v for _, v in results}
    assert "17.0" in versions
    assert "16.0" in versions


def test_scan_repos_ignores_non_odoo_branches(tmp_path):
    make_git_repo(tmp_path / "some_repo", "main")
    results = scan_repos([str(tmp_path)])
    assert not any(str(tmp_path / "some_repo") == p for p, _ in results)


def test_scan_repos_handles_missing_base_dir():
    results = scan_repos(["/nonexistent/path"])
    assert results == []


def test_scan_repos_base_dir_itself_is_repo(tmp_path):
    repo = make_git_repo(tmp_path, "17.0")
    results = scan_repos([str(repo)])
    assert (str(repo), "17.0") in results
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_scanner.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.scanner'`

- [ ] **Bước 3: Implement src/indexer/scanner.py**

```python
# src/indexer/scanner.py
import re
import subprocess
from pathlib import Path


def get_git_branch(repo_path: str) -> str | None:
    """Trả về current branch name của git repo, hoặc None nếu không phải repo."""
    result = subprocess.run(
        ["git", "-C", repo_path, "branch", "--show-current"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch if branch else None


def is_odoo_version_branch(branch: str) -> bool:
    """Kiểm tra branch có phải Odoo version format không (e.g. '17.0', '8.0')."""
    return bool(re.match(r'^\d+\.\d+$', branch))


def scan_repos(base_dirs: list[str]) -> list[tuple[str, str]]:
    """
    Quét danh sách thư mục để tìm git repos có branch Odoo version.
    Trả về list of (repo_path, odoo_version).

    Logic:
    - Nếu base_dir bản thân là git repo với Odoo branch → thêm vào kết quả.
    - Nếu không, quét các subdirectory trực tiếp của base_dir.
    """
    results = []
    for base_dir in base_dirs:
        base = Path(base_dir)
        if not base.exists():
            continue

        branch = get_git_branch(str(base))
        if branch and is_odoo_version_branch(branch):
            results.append((str(base), branch))
            continue

        for subdir in sorted(base.iterdir()):
            if not subdir.is_dir():
                continue
            branch = get_git_branch(str(subdir))
            if branch and is_odoo_version_branch(branch):
                results.append((str(subdir), branch))

    return results
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_scanner.py -v
```

Expected: 8 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/scanner.py tests/test_scanner.py
git commit -m "feat: scanner — phát hiện git repos theo Odoo version branch"
```

---

## Task 4: Registry Builder

**Files:**
- Tạo: `src/indexer/registry.py`
- Tạo: `tests/test_registry.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_registry.py
import subprocess
import pytest
from pathlib import Path
from tests.conftest import make_git_repo, make_manifest
from src.indexer.registry import build_registry, parse_manifest, resolve_odoo_version


# --- Unit tests: parse_manifest ---

def test_parse_manifest_basic(tmp_path):
    manifest_path = tmp_path / "__manifest__.py"
    manifest_path.write_text("""
{
    'name': 'Sales',
    'version': '17.0.1.0.0',
    'depends': ['base', 'account'],
    'installable': True,
}
""")
    result = parse_manifest(str(manifest_path))
    assert result['name'] == 'Sales'
    assert result['depends'] == ['base', 'account']


def test_parse_manifest_returns_empty_on_error(tmp_path):
    bad = tmp_path / "__manifest__.py"
    bad.write_text("not valid python {{{")
    result = parse_manifest(str(bad))
    assert result == {}


# --- Unit tests: resolve_odoo_version ---

def test_resolve_from_long_format(tmp_path):
    repo = make_git_repo(tmp_path, "17.0")
    assert resolve_odoo_version("17.0.1.0.0", str(repo)) == "17.0"


def test_resolve_from_short_format_uses_branch(tmp_path):
    repo = make_git_repo(tmp_path, "16.0")
    assert resolve_odoo_version("1.0.0", str(repo)) == "16.0"


def test_resolve_returns_unknown_when_no_info(tmp_path):
    # Non-git dir, short version
    assert resolve_odoo_version("1.0.0", str(tmp_path)) == "unknown"


# --- Integration tests: build_registry ---

@pytest.fixture
def odoo_repo(tmp_path):
    """Repo với 3 modules: base, account, sale."""
    repo = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    make_manifest(repo / "base",    "Base",    "17.0.1.0.0", [])
    make_manifest(repo / "account", "Account", "17.0.1.0.0", ["base"])
    make_manifest(repo / "sale",    "Sales",   "17.0.1.0.0", ["base", "account"])
    return str(repo)


def test_build_registry_finds_all_modules(odoo_repo):
    registry = build_registry([(odoo_repo, "17.0")])
    assert set(registry["17.0"].keys()) >= {"base", "account", "sale"}


def test_build_registry_parses_depends(odoo_repo):
    registry = build_registry([(odoo_repo, "17.0")])
    assert registry["17.0"]["sale"].depends == ["base", "account"]


def test_build_registry_sets_repo_name(odoo_repo):
    from pathlib import Path
    registry = build_registry([(odoo_repo, "17.0")])
    assert registry["17.0"]["base"].repo == Path(odoo_repo).name


def test_build_registry_skips_non_installable(tmp_path):
    repo = make_git_repo(tmp_path / "repo_17.0", "17.0")
    make_manifest(repo / "disabled_mod", "Disabled", "17.0.1.0.0", [], installable=False)
    make_manifest(repo / "active_mod",   "Active",   "17.0.1.0.0", [])
    registry = build_registry([(str(repo), "17.0")])
    assert "disabled_mod" not in registry.get("17.0", {})
    assert "active_mod" in registry.get("17.0", {})


def test_build_registry_multi_repo(tmp_path):
    repo1 = make_git_repo(tmp_path / "odoo_17.0", "17.0")
    repo2 = make_git_repo(tmp_path / "acme_addons_17.0", "17.0")
    make_manifest(repo1 / "sale",         "Sales",       "17.0.1.0.0", ["base"])
    make_manifest(repo2 / "viin_sale",    "Viin Sales",  "17.0.1.0.0", ["sale"])
    registry = build_registry([(str(repo1), "17.0"), (str(repo2), "17.0")])
    assert "sale" in registry["17.0"]
    assert "viin_sale" in registry["17.0"]
    assert registry["17.0"]["viin_sale"].repo == "acme_addons_17.0"
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_registry.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.registry'`

- [ ] **Bước 3: Implement src/indexer/registry.py**

```python
# src/indexer/registry.py
import ast
import re
from pathlib import Path

from .models import ModuleInfo
from .scanner import get_git_branch, is_odoo_version_branch


def parse_manifest(manifest_path: str) -> dict:
    """Đọc __manifest__.py và trả về dict. Trả về {} nếu có lỗi.

    Chỉ duyệt tree.body (top-level statements) thay vì ast.walk toàn cây,
    tránh bắt nhầm nested dict như 'external_dependencies', 'assets', v.v.
    """
    try:
        source = Path(manifest_path).read_text(encoding='utf-8', errors='ignore')
        tree = ast.parse(source)
        for stmt in tree.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
                return ast.literal_eval(stmt.value)
    except Exception:
        pass
    return {}


def resolve_odoo_version(manifest_version: str, repo_path: str) -> str:
    """
    Xác định Odoo version từ manifest version string.
    Ưu tiên 1: long format "17.0.x.x.x" → lấy 2 phần đầu.
    Ưu tiên 2: git branch của repo → phải là Odoo version format.
    Fallback: "unknown".
    """
    m = re.match(r'^(\d+\.\d+)\.\d+', manifest_version)
    if m:
        return m.group(1)

    branch = get_git_branch(repo_path)
    if branch and is_odoo_version_branch(branch):
        return branch

    return "unknown"


def _find_manifests(repo_path: str) -> list[str]:
    results = []
    for p in Path(repo_path).rglob('__manifest__.py'):
        parts = p.parts
        if '.git' in parts or 'node_modules' in parts:
            continue
        results.append(str(p))
    return results


def build_registry(
    repo_version_pairs: list[tuple[str, str]],
) -> dict[str, dict[str, ModuleInfo]]:
    """
    Xây dựng module registry từ danh sách (repo_path, odoo_version).
    Trả về {odoo_version: {module_name: ModuleInfo}}.

    Xử lý conflict: nếu cùng tên module trong cùng version,
    ưu tiên module có manifest version dạng long format.
    """
    registry: dict[str, dict[str, ModuleInfo]] = {}

    for repo_path, repo_version in repo_version_pairs:
        for manifest_path in _find_manifests(repo_path):
            module_dir = Path(manifest_path).parent
            module_name = module_dir.name

            manifest = parse_manifest(manifest_path)
            if not manifest:
                continue
            if not manifest.get('installable', True):
                continue

            version_raw = manifest.get('version', '')
            odoo_version = resolve_odoo_version(version_raw, repo_path)
            if odoo_version == "unknown":
                odoo_version = repo_version  # fallback sang version từ scanner
            if odoo_version == "unknown":
                continue

            info = ModuleInfo(
                name=module_name,
                odoo_version=odoo_version,
                repo=Path(repo_path).name,
                path=str(module_dir),
                depends=manifest.get('depends', []),
                version_raw=version_raw,
            )

            if odoo_version not in registry:
                registry[odoo_version] = {}

            existing = registry[odoo_version].get(module_name)
            if existing:
                # Giữ module có long-format version (chứa Odoo version prefix)
                if re.match(r'^\d+\.\d+\.\d+', version_raw):
                    registry[odoo_version][module_name] = info
                # else: giữ existing
            else:
                registry[odoo_version][module_name] = info

    return registry
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_registry.py -v
```

Expected: 10 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/registry.py tests/test_registry.py
git commit -m "feat: registry builder — parse manifests, resolve version, build module map"
```

---

## Task 5: Dependency Resolver

**Files:**
- Tạo: `src/indexer/resolver.py`
- Tạo: `tests/test_resolver.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_resolver.py
from src.indexer.models import ModuleInfo
from src.indexer.resolver import topological_sort


def make_mod(name: str, depends: list[str]) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version="17.0", repo="test",
        path="/tmp", depends=depends,
    )


def test_simple_linear_chain():
    modules = {
        "base":    make_mod("base", []),
        "mail":    make_mod("mail", ["base"]),
        "sale":    make_mod("sale", ["base", "mail"]),
    }
    result = topological_sort(modules)
    assert result.index("base") < result.index("mail")
    assert result.index("mail") < result.index("sale")


def test_all_modules_present_in_result():
    modules = {
        "base":    make_mod("base", []),
        "account": make_mod("account", ["base"]),
        "sale":    make_mod("sale", ["account"]),
    }
    result = topological_sort(modules)
    assert set(result) == {"base", "account", "sale"}


def test_missing_dependency_is_ignored():
    modules = {
        "sale": make_mod("sale", ["base", "nonexistent_module"]),
        "base": make_mod("base", []),
    }
    result = topological_sort(modules)
    assert "sale" in result
    assert "base" in result
    assert result.index("base") < result.index("sale")


def test_circular_dependency_does_not_hang():
    modules = {
        "a": make_mod("a", ["b"]),
        "b": make_mod("b", ["a"]),
    }
    result = topological_sort(modules)
    assert set(result) == {"a", "b"}


def test_no_modules():
    assert topological_sort({}) == []


def test_single_module_no_deps():
    modules = {"base": make_mod("base", [])}
    assert topological_sort(modules) == ["base"]


def test_deterministic_for_same_input():
    modules = {
        "b": make_mod("b", []),
        "a": make_mod("a", []),
        "c": make_mod("c", ["a", "b"]),
    }
    result1 = topological_sort(modules)
    result2 = topological_sort(modules)
    assert result1 == result2
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_resolver.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.resolver'`

- [ ] **Bước 3: Implement src/indexer/resolver.py**

```python
# src/indexer/resolver.py
from collections import deque

from .models import ModuleInfo


def topological_sort(modules: dict[str, ModuleInfo]) -> list[str]:
    """
    Kahn's algorithm — sắp xếp modules theo thứ tự dependency.
    Base modules luôn đứng trước modules phụ thuộc vào chúng.

    Edge case:
    - Missing dep: bỏ qua, tiếp tục.
    - Circular dep: append phần còn lại theo alphabetical order.
    - Deterministic: dùng sorted() ở mọi bước.
    """
    if not modules:
        return []

    in_degree: dict[str, int] = {name: 0 for name in modules}
    dependents: dict[str, list[str]] = {name: [] for name in modules}

    for name, info in modules.items():
        for dep in info.depends:
            if dep in modules:
                in_degree[name] += 1
                dependents[dep].append(name)

    queue = deque(sorted(name for name, deg in in_degree.items() if deg == 0))
    result: list[str] = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for dependent in sorted(dependents[node]):
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                queue.append(dependent)

    # Circular deps: append remaining theo alphabetical order
    if len(result) < len(modules):
        remaining = sorted(set(modules) - set(result))
        result.extend(remaining)

    return result
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_resolver.py -v
```

Expected: 8 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/resolver.py tests/test_resolver.py
git commit -m "feat: dependency resolver — Kahn topological sort với circular dep handling"
```

---

## Task 6: Python AST Parser

**Files:**
- Tạo: `src/indexer/parser_python.py`
- Tạo: `tests/test_parser_python.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_parser_python.py
import textwrap
from pathlib import Path
import pytest
from src.indexer.models import ModuleInfo
from src.indexer.parser_python import parse_file, parse_module


@pytest.fixture
def sale_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=["base"], version_raw="17.0.1.0.0",
    )


def write_py(directory: Path, filename: str, content: str) -> str:
    filepath = directory / filename
    filepath.write_text(textwrap.dedent(content))
    return str(filepath)


# --- parse_file tests ---

def test_parse_basic_model_name(tmp_path, sale_module):
    f = write_py(tmp_path, "sale_order.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            _description = 'Sales Order'
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].name == "sale.order"


def test_parse_field_types(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class MyModel(models.Model):
            _name = 'my.model'
            name = fields.Char(required=True)
            amount = fields.Float(compute='_compute_amount', store=True)
            partner_id = fields.Many2one('res.partner')
            line_ids = fields.One2many('my.line', 'order_id')
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    model = result[0]
    field_map = {fld.name: fld for fld in model.fields}

    assert field_map["name"].ttype == "char"
    assert field_map["name"].required is True
    assert field_map["amount"].compute == "_compute_amount"
    assert field_map["amount"].stored is True
    assert field_map["partner_id"].ttype == "many2one"
    assert field_map["line_ids"].ttype == "one2many"


def test_computed_field_default_not_stored(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class M(models.Model):
            _name = 'm'
            computed = fields.Float(compute='_compute')
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["computed"].stored is False


def test_parse_single_inherit(tmp_path, sale_module):
    f = write_py(tmp_path, "extend.py", """
        from odoo import models, fields

        class SaleExtend(models.Model):
            _inherit = 'sale.order'
            x_custom = fields.Char()
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    model = result[0]
    assert model.name == "sale.order"
    assert "sale.order" in model.inherit


def test_parse_multi_inherit(tmp_path, sale_module):
    f = write_py(tmp_path, "mixin.py", """
        from odoo import models

        class SaleOrderMixin(models.Model):
            _name = 'sale.order'
            _inherit = ['sale.order', 'mail.thread', 'mail.activity.mixin']
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    assert set(model.inherit) == {'sale.order', 'mail.thread', 'mail.activity.mixin'}


def test_parse_inherits_delegation(tmp_path, sale_module):
    f = write_py(tmp_path, "employee.py", """
        from odoo import models, fields

        class HrEmployee(models.Model):
            _name = 'hr.employee'
            _inherits = {'res.users': 'user_id'}
            user_id = fields.Many2one('res.users', required=True)
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    assert model.inherits == {'res.users': 'user_id'}


def test_parse_method_with_super(tmp_path, sale_module):
    f = write_py(tmp_path, "override.py", """
        from odoo import models

        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            def action_confirm(self):
                result = super().action_confirm()
                return result

            def _prepare_invoice(self):
                vals = {}
                return vals
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    method_map = {m.name: m for m in model.methods}
    assert method_map["action_confirm"].has_super_call is True
    assert method_map["_prepare_invoice"].has_super_call is False


def test_parse_method_decorators(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, api

        class MyModel(models.Model):
            _name = 'my.model'

            @api.depends('partner_id')
            def _compute_name(self):
                pass

            @api.onchange('partner_id')
            def _onchange_partner(self):
                pass
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    method_map = {m.name: m for m in model.methods}
    assert "api.depends" in method_map["_compute_name"].decorators
    assert "api.onchange" in method_map["_onchange_partner"].decorators


def test_parse_skips_syntax_error_files(tmp_path, sale_module):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(: invalid syntax {{{")
    result = parse_file(str(bad), sale_module)
    assert result == []


def test_parse_skips_non_model_classes(tmp_path, sale_module):
    f = write_py(tmp_path, "utils.py", """
        class MyHelper:
            def do_something(self):
                pass
    """)
    result = parse_file(f, sale_module)
    assert result == []


# --- parse_module tests ---

def test_parse_module_scans_all_py_files(tmp_path, sale_module):
    sale_module_with_path = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="17.0.1.0.0",
    )
    write_py(tmp_path, "sale_order.py", """
        from odoo import models
        class SaleOrder(models.Model):
            _name = 'sale.order'
    """)
    write_py(tmp_path, "sale_line.py", """
        from odoo import models
        class SaleOrderLine(models.Model):
            _name = 'sale.order.line'
    """)
    result = parse_module(sale_module_with_path)
    model_names = {m.name for m in result.models}
    assert "sale.order" in model_names
    assert "sale.order.line" in model_names


def test_parse_module_skips_manifest(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="test",
        path=str(tmp_path), depends=[], version_raw="",
    )
    (tmp_path / "__manifest__.py").write_text("{'name': 'Sales'}")
    result = parse_module(module)
    assert result.models == []
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_parser_python.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.parser_python'`

- [ ] **Bước 3: Implement src/indexer/parser_python.py**

```python
# src/indexer/parser_python.py
import ast
from pathlib import Path

from .models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult

FIELD_TYPES = {
    'Char', 'Text', 'Html', 'Integer', 'Float', 'Monetary', 'Boolean',
    'Date', 'Datetime', 'Binary', 'Selection', 'Many2one', 'One2many',
    'Many2many', 'Reference', 'Json', 'Properties', 'Image',
}

MODEL_BASE_CLASSES = {'Model', 'TransientModel', 'AbstractModel', 'BaseModel'}


def _extract_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extract_inherit(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.List):
        return [s for elt in node.elts if (s := _extract_string(elt))]
    return []


def _extract_inherits(node: ast.expr) -> dict[str, str]:
    result = {}
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            key = _extract_string(k)
            val = _extract_string(v)
            if key and val:
                result[key] = val
    return result


def _has_super_call(func_node: ast.FunctionDef) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            # super().method(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
                inner = func.value
                if isinstance(inner.func, ast.Name) and inner.func.id == 'super':
                    return True
    return False


def _get_base_class_names(cls_node: ast.ClassDef) -> set[str]:
    names = set()
    for base in cls_node.bases:
        if isinstance(base, ast.Attribute):
            names.add(base.attr)
        elif isinstance(base, ast.Name):
            names.add(base.id)
    return names


def _parse_class(cls_node: ast.ClassDef, module_info: ModuleInfo) -> ModelInfo | None:
    base_names = _get_base_class_names(cls_node)
    is_model_class = bool(base_names & MODEL_BASE_CLASSES)

    name = None
    inherit: list[str] = []
    inherits: dict[str, str] = {}
    is_abstract = 'AbstractModel' in base_names
    is_transient = 'TransientModel' in base_names
    fields_list: list[FieldInfo] = []
    methods_list: list[MethodInfo] = []

    for node in cls_node.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                attr = target.id
                if attr == '_name':
                    name = _extract_string(node.value)
                elif attr == '_inherit':
                    inherit = _extract_inherit(node.value)
                elif attr == '_inherits':
                    inherits = _extract_inherits(node.value)
                elif attr == '_abstract' and isinstance(node.value, ast.Constant):
                    is_abstract = bool(node.value.value)
                elif attr == '_transient' and isinstance(node.value, ast.Constant):
                    is_transient = bool(node.value.value)

            # Field detection: field_name = fields.FieldType(...)
            if (isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == 'fields'
                    and node.value.func.attr in FIELD_TYPES
                    and node.targets
                    and isinstance(node.targets[0], ast.Name)):
                call = node.value
                field_name = node.targets[0].id
                field_type = call.func.attr.lower()
                kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}

                related = _extract_string(kwargs['related']) if 'related' in kwargs else None
                compute = _extract_string(kwargs['compute']) if 'compute' in kwargs else None
                required = bool(getattr(kwargs.get('required'), 'value', False))
                # store kwarg: computed và related fields mặc định store=False
                if 'store' in kwargs:
                    stored = bool(getattr(kwargs['store'], 'value', True))
                else:
                    stored = (compute is None and related is None)

                fields_list.append(FieldInfo(
                    name=field_name, ttype=field_type,
                    related=related, compute=compute,
                    stored=stored, required=required,
                ))

        elif isinstance(node, ast.FunctionDef) and not node.name.startswith('__'):
            decorators = []
            for dec in node.decorator_list:
                if isinstance(dec, ast.Attribute):
                    decorators.append(f'api.{dec.attr}')
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    decorators.append(f'api.{dec.func.attr}')
                elif isinstance(dec, ast.Name):
                    decorators.append(dec.id)

            methods_list.append(MethodInfo(
                name=node.name,
                has_super_call=_has_super_call(node),
                decorators=decorators,
            ))

    # _inherit mà không có _name → name = inherit[0] (Odoo convention)
    # Áp dụng cả khi inherit có nhiều phần tử: ['sale.order', 'mail.thread']
    if not name and inherit:
        name = inherit[0]

    # Không phải Odoo model nếu không có _name và không phải Model subclass
    if not name:
        return None
    if not is_model_class and not inherit and not inherits:
        return None

    return ModelInfo(
        name=name,
        module=module_info.name,
        odoo_version=module_info.odoo_version,
        is_abstract=is_abstract,
        is_transient=is_transient,
        inherit=inherit,
        inherits=inherits,
        fields=fields_list,
        methods=methods_list,
    )


def parse_file(filepath: str, module_info: ModuleInfo) -> list[ModelInfo]:
    """Parse một file Python, trả về list các ModelInfo tìm được."""
    try:
        source = Path(filepath).read_text(encoding='utf-8', errors='ignore')
        tree = ast.parse(source)
    except SyntaxError:
        return []

    models = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            model = _parse_class(node, module_info)
            if model:
                models.append(model)
    return models


def parse_module(module_info: ModuleInfo) -> ParseResult:
    """Parse toàn bộ file Python trong một module directory."""
    result = ParseResult(module=module_info)
    module_path = Path(module_info.path)

    SKIP_DIRS = {'.git', 'static', 'migrations', 'tests', '__pycache__'}

    for py_file in sorted(module_path.rglob('*.py')):
        if py_file.name == '__manifest__.py':
            continue
        if SKIP_DIRS & set(py_file.parts):
            continue
        models = parse_file(str(py_file), module_info)
        result.models.extend(models)

    return result
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_parser_python.py -v
```

Expected: 13 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/parser_python.py tests/test_parser_python.py
git commit -m "feat: Python AST parser — models/fields/methods/inherit chains"
```

---

## Task 7: Neo4j Writer

> **Lưu ý:** Task này là integration test — cần Neo4j đang chạy (`docker compose up -d`).

**Files:**
- Tạo: `src/indexer/writer_neo4j.py`
- Tạo: `tests/test_writer_neo4j.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_writer_neo4j.py
import pytest
from tests.conftest import TEST_VERSION
from src.indexer.models import ModuleInfo, ModelInfo, FieldInfo, MethodInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j):
    """Neo4jWriter kết nối tới test DB, dùng version TEST_VERSION."""
    import os
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def make_parse_result(module_name: str, model_name: str) -> ParseResult:
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        fields=[
            FieldInfo(name="name", ttype="char", required=True),
            FieldInfo(name="amount", ttype="float", compute="_compute", stored=False),
        ],
        methods=[
            MethodInfo(name="action_confirm", has_super_call=True, decorators=[]),
        ],
    )
    return ParseResult(module=module, models=[model])


def test_write_module_node(writer, clean_neo4j):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with clean_neo4j.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN m",
            n="sale", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["m"]["repo"] == "sale_repo"


def test_write_model_node(writer, clean_neo4j):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with clean_neo4j.session() as session:
        rec = session.run(
            "MATCH (m:Model {name: $n, odoo_version: $v}) RETURN m",
            n="sale.order", v=TEST_VERSION
        ).single()
    assert rec is not None


def test_write_field_nodes(writer, clean_neo4j):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with clean_neo4j.session() as session:
        fields = session.run(
            "MATCH (f:Field {model: $m, odoo_version: $v}) RETURN f.name as name",
            m="sale.order", v=TEST_VERSION
        ).data()
    field_names = {r["name"] for r in fields}
    assert "name" in field_names
    assert "amount" in field_names


def test_write_method_node(writer, clean_neo4j):
    result = make_parse_result("sale", "sale.order")
    writer.write_results([result])

    with clean_neo4j.session() as session:
        rec = session.run(
            "MATCH (m:Method {name: $n, model: $model, odoo_version: $v}) RETURN m",
            n="action_confirm", model="sale.order", v=TEST_VERSION
        ).single()
    assert rec is not None
    assert rec["m"]["has_super_call"] is True


def test_write_inherits_edge(writer, clean_neo4j):
    # base_mod định nghĩa sale.order, ext_mod extend nó (_inherit = 'sale.order')
    # Expected: (:Model {name:'sale.order', module:'ext_mod'})-[:INHERITS]->(:Model {name:'sale.order', module:'base_mod'})
    base_module = ModuleInfo(
        name="base_mod", odoo_version=TEST_VERSION,
        repo="base_repo", path="/tmp", depends=[], version_raw="",
    )
    base_model = ModelInfo(
        name="sale.order", module="base_mod", odoo_version=TEST_VERSION,
    )
    ext_module = ModuleInfo(
        name="ext_mod", odoo_version=TEST_VERSION,
        repo="ext_repo", path="/tmp", depends=["base_mod"], version_raw="",
    )
    ext_model = ModelInfo(
        name="sale.order", module="ext_mod", odoo_version=TEST_VERSION,
        inherit=["sale.order"],
    )
    # Write base trước (topo-order), rồi extension
    writer.write_results([
        ParseResult(module=base_module, models=[base_model]),
        ParseResult(module=ext_module, models=[ext_model]),
    ])

    with clean_neo4j.session() as session:
        rec = session.run("""
            MATCH (ext:Model {name: 'sale.order', module: 'ext_mod', odoo_version: $v})
                  -[:INHERITS]->(base:Model {name: 'sale.order', module: 'base_mod', odoo_version: $v})
            RETURN count(*) AS cnt
        """, v=TEST_VERSION).single()
    assert rec["cnt"] == 1


def test_write_delegates_to_edge(writer, clean_neo4j):
    module = ModuleInfo(
        name="hr", odoo_version=TEST_VERSION,
        repo="hr_repo", path="/tmp", depends=[], version_raw="",
    )
    model = ModelInfo(
        name="hr.employee", module="hr", odoo_version=TEST_VERSION,
        inherits={"res.users": "user_id"},
    )
    writer.write_results([ParseResult(module=module, models=[model])])

    with clean_neo4j.session() as session:
        rec = session.run("""
            MATCH (:Model {name: 'hr.employee', odoo_version: $v})
                  -[r:DELEGATES_TO]->(:Model {name: 'res.users', odoo_version: $v})
            RETURN r.via_field as via_field
        """, v=TEST_VERSION).single()
    assert rec is not None
    assert rec["via_field"] == "user_id"
```

- [ ] **Bước 2: Đảm bảo Neo4j đang chạy**

```bash
docker compose up -d neo4j
docker compose ps   # neo4j phải ở trạng thái healthy
```

- [ ] **Bước 3: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_writer_neo4j.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.indexer.writer_neo4j'`

- [ ] **Bước 4: Implement src/indexer/writer_neo4j.py**

```python
# src/indexer/writer_neo4j.py
from neo4j import GraphDatabase

from .models import ParseResult, ModuleInfo


def _write_parse_result(tx, result: ParseResult) -> None:
    module = result.module

    # Module node
    tx.run("""
        MERGE (m:Module {name: $name, odoo_version: $v})
        SET m.repo = $repo, m.path = $path, m.version_raw = $version_raw
    """, name=module.name, v=module.odoo_version,
         repo=module.repo, path=module.path, version_raw=module.version_raw)

    # DEPENDS_ON edges
    for dep in module.depends:
        tx.run("""
            MATCH (m:Module {name: $name, odoo_version: $v})
            MERGE (d:Module {name: $dep, odoo_version: $v})
            MERGE (m)-[:DEPENDS_ON]->(d)
        """, name=module.name, v=module.odoo_version, dep=dep)

    for model in result.models:
        # Model node key: (name, module, odoo_version) — mỗi module tạo 1 node riêng.
        # Đây là quyết định schema C1: N nodes per model name, không phải 1 node.
        # Lý do: cần phân biệt "sale.order được định nghĩa trong sale" vs
        # "sale.order được extend trong viin_sale" → 2 node khác nhau.
        tx.run("""
            MERGE (mod:Module {name: $module_name, odoo_version: $v})
            MERGE (m:Model {name: $name, module: $module_name, odoo_version: $v})
            SET m.is_abstract = $is_abstract,
                m.is_transient = $is_transient
            MERGE (m)-[:DEFINED_IN]->(mod)
        """, name=model.name, v=model.odoo_version,
             module_name=model.module,
             is_abstract=model.is_abstract,
             is_transient=model.is_transient)

        # INHERITS edges — 2 loại:
        for parent_name in model.inherit:
            if parent_name == model.name:
                # Override chain (cùng tên): liên kết với "tip" — node cùng tên
                # chưa bị node nào khác inherit đến. Topo-sort đảm bảo base đã tồn tại.
                tx.run("""
                    MATCH (ext:Model {name: $name, module: $mod, odoo_version: $v})
                    MATCH (tip:Model {name: $name, odoo_version: $v})
                    WHERE tip.module <> $mod
                      AND NOT (:Model {name: $name, odoo_version: $v})-[:INHERITS]->(tip)
                    MERGE (ext)-[:INHERITS]->(tip)
                """, name=model.name, mod=model.module, v=model.odoo_version)
            else:
                # Mixin / base khác tên (mail.thread, etc.)
                tx.run("""
                    MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                    MATCH (parent:Model {name: $parent_name, odoo_version: $v})
                    MERGE (m)-[:INHERITS]->(parent)
                """, model_name=model.name, mod=model.module,
                     v=model.odoo_version, parent_name=parent_name)

        # DELEGATES_TO edges
        for delegated_model, via_field in model.inherits.items():
            tx.run("""
                MATCH (m:Model {name: $name, module: $mod, odoo_version: $v})
                MATCH (d:Model {name: $delegated, odoo_version: $v})
                MERGE (m)-[:DELEGATES_TO {via_field: $via_field}]->(d)
            """, name=model.name, mod=model.module, v=model.odoo_version,
                 delegated=delegated_model, via_field=via_field)

        # Field nodes key: (name, model, module, odoo_version) — giữ field của mỗi module riêng
        for fld in model.fields:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (f:Field {name: $name, model: $model_name,
                               module: $mod, odoo_version: $v})
                SET f.ttype = $ttype, f.related = $related, f.compute = $compute,
                    f.stored = $stored, f.required = $required
                MERGE (f)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=fld.name, ttype=fld.ttype, related=fld.related,
                 compute=fld.compute, stored=fld.stored, required=fld.required)

        # Method nodes key: (name, model, module, odoo_version)
        for mth in model.methods:
            tx.run("""
                MATCH (m:Model {name: $model_name, module: $mod, odoo_version: $v})
                MERGE (mth:Method {name: $name, model: $model_name,
                                   module: $mod, odoo_version: $v})
                SET mth.has_super_call = $has_super_call,
                    mth.decorators = $decorators
                MERGE (mth)-[:BELONGS_TO]->(m)
            """, model_name=model.name, mod=model.module, v=model.odoo_version,
                 name=mth.name, has_super_call=mth.has_super_call,
                 decorators=mth.decorators)


class Neo4jWriter:
    def __init__(self, uri: str, user: str, password: str):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def setup_indexes(self) -> None:
        with self.driver.session() as session:
            for stmt in [
                "CREATE INDEX IF NOT EXISTS FOR (n:Module) ON (n.name, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Model)  ON (n.name, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Field)  ON (n.name, n.model, n.module, n.odoo_version)",
                "CREATE INDEX IF NOT EXISTS FOR (n:Method) ON (n.name, n.model, n.module, n.odoo_version)",
            ]:
                session.run(stmt)

    def write_results(self, results: list[ParseResult]) -> None:
        with self.driver.session() as session:
            for result in results:
                session.execute_write(_write_parse_result, result)
```

- [ ] **Bước 5: Chạy test — xác nhận PASS**

```bash
pytest tests/test_writer_neo4j.py -v
```

Expected: 7 tests PASSED.

- [ ] **Bước 6: Commit**

```bash
git add src/indexer/writer_neo4j.py tests/test_writer_neo4j.py
git commit -m "feat: Neo4j writer — nodes + edges cho modules/models/fields/methods"
```

---

## Task 8: MCP Server

> **Lưu ý:** Task này cần Neo4j đang chạy với ít nhất 1 version đã được index (dùng data từ Task 7 tests, hoặc chạy indexer thật).

**Files:**
- Tạo: `src/mcp/server.py`
- Tạo: `tests/test_mcp_server.py`

- [ ] **Bước 1: Viết failing tests**

```python
# tests/test_mcp_server.py
import os
import pytest
from tests.conftest import TEST_VERSION
from src.indexer.models import ModuleInfo, ModelInfo, FieldInfo, MethodInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def seeded_neo4j(neo4j_driver):
    """Seed Neo4j với test data cho MCP server tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Cleanup trước
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    # Seed: base module với model account.move
    base_mod = ModuleInfo("account", TEST_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="account.move", module="account", odoo_version=TEST_VERSION,
        fields=[FieldInfo("name", "char", required=True),
                FieldInfo("amount_total", "float", compute="_compute_amount", stored=True)],
        methods=[MethodInfo("action_post", has_super_call=False)],
    )

    # Seed: extension module
    ext_mod = ModuleInfo("viin_account", TEST_VERSION, "acme_addons_test", "/tmp",
                          ["account"], "")
    ext_model = ModelInfo(
        name="account.move", module="viin_account", odoo_version=TEST_VERSION,
        inherit=["account.move"],
        fields=[FieldInfo("x_approval_state", "selection")],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext_mod, models=[ext_model]),
    ])
    writer.close()
    yield
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def mcp_tools(seeded_neo4j):
    """Import MCP tool functions sau khi đã seed data."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    from src.mcp.server import resolve_model, resolve_field, resolve_method
    return resolve_model, resolve_field, resolve_method


def test_resolve_model_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account.move" in result
    assert TEST_VERSION in result


def test_resolve_model_shows_module(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account" in result


def test_resolve_model_not_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("nonexistent.model", TEST_VERSION)
    assert "Không tìm thấy" in result


def test_resolve_field_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "amount_total" in result
    assert "float" in result.lower()


def test_resolve_field_shows_compute(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "_compute_amount" in result


def test_resolve_field_not_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "nonexistent_field", TEST_VERSION)
    assert "Không tìm thấy" in result


def test_resolve_method_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "action_post", TEST_VERSION)
    assert "action_post" in result


def test_resolve_method_not_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "nonexistent_method", TEST_VERSION)
    assert "Không tìm thấy" in result
```

- [ ] **Bước 2: Chạy test — xác nhận FAIL**

```bash
pytest tests/test_mcp_server.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.mcp.server'`

- [ ] **Bước 3: Implement src/mcp/server.py**

```python
# src/mcp/server.py
import os
from dotenv import load_dotenv
from fastmcp import FastMCP
from neo4j import GraphDatabase

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

mcp = FastMCP("odoo-semantic")
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def _latest_version(session) -> str:
    # Dùng toFloat() để sort đúng: "17.0" > "9.0" (lexicographic thì "9.0" > "17.0")
    rec = session.run("""
        MATCH (m:Model)
        WITH DISTINCT m.odoo_version AS v
        RETURN v ORDER BY toFloat(v) DESC LIMIT 1
    """).single()
    return rec["v"] if rec else "17.0"


@mcp.tool()
def resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    """
    Trả về thông tin đầy đủ về Odoo model: inheritance chain,
    delegated models, field summary, method summary.

    Args:
        model_name:   Tên model, ví dụ 'sale.order', 'account.move'.
        odoo_version: Phiên bản Odoo, ví dụ '17.0'. Mặc định: version mới nhất.
    """
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        # Lấy tất cả module-scoped nodes của model (schema C1: N nodes per model name)
        # Order by số inbound INHERITS tăng dần → base (ít inbound) đứng trước
        layers = session.run("""
            MATCH (m:Model {name: $name, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
            RETURN m.module AS module_name, mod.repo AS repo
            ORDER BY size(()-[:INHERITS]->(m)) ASC
        """, name=model_name, v=odoo_version).data()

        if not layers:
            return f"Không tìm thấy model '{model_name}' trong Odoo {odoo_version}."

        base = layers[0]
        extensions = layers[1:]

        # Mixin parents khác tên (mail.thread, account.move.mixin, ...)
        parents = session.run("""
            MATCH (:Model {name: $name, odoo_version: $v})-[:INHERITS]->(p:Model)
            WHERE p.name <> $name
            OPTIONAL MATCH (p)-[:DEFINED_IN]->(mod:Module)
            RETURN DISTINCT p.name AS pname, mod.name AS module_name
        """, name=model_name, v=odoo_version).data()

        fields_count = session.run(
            "MATCH (f:Field {model: $n, odoo_version: $v}) RETURN count(f) AS c",
            n=model_name, v=odoo_version
        ).single()["c"]

        methods_count = session.run(
            "MATCH (m:Method {model: $n, odoo_version: $v}) RETURN count(m) AS c",
            n=model_name, v=odoo_version
        ).single()["c"]

    lines = [f"{model_name} (Odoo {odoo_version})"]
    lines.append(f"├─ Định nghĩa tại: [{base['repo']}] {base['module_name']}")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Kế thừa từ:    {parents_str}")

    if extensions:
        lines.append("├─ Mở rộng bởi:")
        for ext in extensions:
            lines.append(f"│   └─ [{ext['repo']}] {ext['module_name']}")

    lines.append(f"├─ Tổng số field:  {fields_count}")
    lines.append(f"└─ Tổng số method: {methods_count}")
    return "\n".join(lines)


@mcp.tool()
def resolve_field(model_name: str, field_name: str, odoo_version: str = "auto") -> str:
    """
    Trả về chi tiết một field: type, computed/related metadata, module nguồn.

    Args:
        model_name:   Tên model chứa field.
        field_name:   Tên field, ví dụ 'amount_total', 'partner_id'.
        odoo_version: Phiên bản Odoo. Mặc định: version mới nhất.
    """
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        # Schema C1: Field key = (name, model, module, odoo_version) — N nodes cho cùng field
        # dùng f.module trực tiếp, tra Module bằng {name, odoo_version}
        records = session.run("""
            MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: f.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $mn, module: f.module, odoo_version: $v})
            RETURN f, f.module AS module_name, mod.repo AS repo,
                   size(()-[:INHERITS]->(m_node)) AS depth
            ORDER BY depth ASC
        """, fn=field_name, mn=model_name, v=odoo_version).data()

    if not records:
        return f"Không tìm thấy field '{field_name}' trên model '{model_name}' trong Odoo {odoo_version}."

    base_f = records[0]["f"]
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Loại:     {base_f.get('ttype', '?')}",
        f"├─ Computed: {'Có' if base_f.get('compute') else 'Không'}"
        + (f" ({base_f['compute']})" if base_f.get('compute') else ""),
        f"├─ Stored:   {'Có' if base_f.get('stored', True) else 'Không'}",
        f"├─ Required: {'Có' if base_f.get('required', False) else 'Không'}",
        f"├─ Related:  {base_f.get('related') or '—'}",
        f"└─ Khai báo trong:",
    ]
    for r in records:
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        lines.append(f"    └─ {repo_str}{r['module_name']}")
    return "\n".join(lines)


@mcp.tool()
def resolve_method(model_name: str, method_name: str, odoo_version: str = "auto") -> str:
    """
    Trả về override chain của một method theo thứ tự base→top.

    Args:
        model_name:   Tên model chứa method.
        method_name:  Tên method, ví dụ 'action_confirm', '_compute_amount'.
        odoo_version: Phiên bản Odoo. Mặc định: version mới nhất.
    """
    with _get_driver().session() as session:
        if odoo_version == "auto":
            odoo_version = _latest_version(session)

        # Schema C1: Method key = (name, model, module, odoo_version)
        # dùng mth.module trực tiếp, tra Module bằng {name, odoo_version}
        records = session.run("""
            MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            OPTIONAL MATCH (m_node:Model {name: $model, module: mth.module, odoo_version: $v})
            RETURN mth, mth.module AS module_name, mod.repo AS repo,
                   size(()-[:INHERITS]->(m_node)) AS depth
            ORDER BY depth ASC
        """, mn=method_name, model=model_name, v=odoo_version).data()

    if not records:
        return f"Không tìm thấy method '{method_name}' trên model '{model_name}' trong Odoo {odoo_version}."

    lines = [f"{model_name}.{method_name}() (Odoo {odoo_version})", "Override chain:"]
    for r in records:
        mth = r["mth"]
        super_info = "✓ gọi super()" if mth.get("has_super_call") else "✗ không gọi super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        lines.append(f"  [{r['repo']}] {r['module_name']} — {super_info} — decorators: {decs}")
    return "\n".join(lines)


if __name__ == "__main__":
    # fastmcp >= 2.3: streamable-http transport. Verify params với `python -c "import fastmcp; help(fastmcp.FastMCP.run)"` khi implement.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8002, path="/mcp")
```

- [ ] **Bước 4: Chạy test — xác nhận PASS**

```bash
pytest tests/test_mcp_server.py -v
```

Expected: 8 tests PASSED.

- [ ] **Bước 5: Commit**

```bash
git add src/mcp/server.py tests/test_mcp_server.py
git commit -m "feat: MCP server — resolve_model, resolve_field, resolve_method"
```

---

## Task 9: Integration E2E — Chạy Indexer Thật + Kết Nối Claude Code

- [ ] **Bước 1: Chạy toàn bộ test suite**

```bash
pytest tests/ -v --tb=short
```

Expected: Tất cả tests PASSED.

- [ ] **Bước 2: Viết script indexer CLI tạm thời để test thật**

```python
# scripts/index_test.py
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
```

- [ ] **Bước 3: Chạy indexer**

```bash
python scripts/index_test.py
```

Expected output: "Done! Neo4j populated." không có traceback.

- [ ] **Bước 4: Khởi động MCP server**

```bash
python -m src.mcp.server
```

Expected: Server chạy trên `http://0.0.0.0:8002/mcp`

- [ ] **Bước 5: Thêm config vào Claude Code**

Mở `~/.claude/settings.json`, thêm:

```json
{
  "mcpServers": {
    "odoo-semantic": {
      "url": "http://localhost:8002/mcp"
    }
  }
}
```

- [ ] **Bước 6: Kiểm tra "First Wow"**

Trong Claude Code, hỏi:

```
Use resolve_model to explain the full inheritance chain of account.move in Odoo 17.0
```

Expected: Claude trả về cấu trúc inheritance chain đúng, cross-repo, không hallucinate.

- [ ] **Bước 7: Update TASKS.md — đánh dấu Milestone 1 hoàn thành**

Đánh dấu `[x]` tất cả tasks trong Milestone 1 của `TASKS.md`.

- [ ] **Bước 8: Commit**

```bash
git add scripts/index_test.py TASKS.md
git commit -m "feat: E2E integration — indexer script + Claude Code connect"
```

---

## Task 10: GitHub CI/CD — PR phải xanh trước khi merge

**Files:**
- Tạo: `.github/workflows/ci.yml`

- [ ] **Bước 1: Viết failing test xác nhận marker neo4j hoạt động**

```bash
# Chạy chỉ unit tests (không cần Neo4j) — phải PASS ngay cả khi Neo4j không chạy
pytest tests/ -v -m "not neo4j" --tb=short
```

Expected: Tất cả unit tests PASSED, integration tests bị skip.

- [ ] **Bước 2: Tạo `.github/workflows/ci.yml`**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff
      - run: ruff check src/ tests/

  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v -m "not neo4j" --tb=short

  integration-tests:
    runs-on: ubuntu-latest
    services:
      neo4j:
        image: neo4j:5
        env:
          NEO4J_AUTH: neo4j/password
        ports:
          - 7687:7687
        options: >-
          --health-cmd "cypher-shell -u neo4j -p password 'RETURN 1' || exit 1"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v -m "neo4j" --tb=short
        env:
          NEO4J_TEST_URI: bolt://localhost:7687
          NEO4J_TEST_USER: neo4j
          NEO4J_TEST_PASSWORD: password
```

- [ ] **Bước 3: Bật branch protection trên GitHub**

Vào **Settings → Branches → Add branch protection rule** cho `main`/`master`:

- ✅ Require status checks to pass before merging
- Thêm checks: `lint`, `unit-tests`, `integration-tests`
- ✅ Require branches to be up to date before merging
- ✅ Do not allow bypassing the above settings

- [ ] **Bước 4: Push để trigger CI lần đầu**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions — lint + unit + integration tests"
git push
```

Expected: GitHub Actions chạy 3 jobs, tất cả xanh (✅).

---

## Self-Review

**Spec coverage check:**

| Yêu cầu từ spec | Task xử lý |
|-----------------|-----------|
| Scanner: git branch detection | Task 3 |
| Registry: module-name → {repo, path, depends} per version | Task 4 |
| Version resolution: long format first, git branch fallback | Task 4 |
| Topological sort + circular dep handling | Task 5 |
| Python AST: `_name`/`_inherit`/`_inherits`/fields/methods | Task 6 |
| Neo4j: Module/Model/Field/Method nodes + edges | Task 7 |
| MCP: resolve_model / resolve_field / resolve_method | Task 8 |
| E2E test VS Code + Claude Code | Task 9 |
| GitHub CI/CD — PR phải xanh trước merge | Task 10 |
| docker-compose Neo4j + PostgreSQL | Task 1 |
| Version-scoped: mọi node có `odoo_version` property | Task 7 |
| Cross-repo inheritance chain | Task 7 + 9 |

**Không có placeholders, TBDs, hay bước thiếu code.**

**Type consistency:** `ModuleInfo`, `ModelInfo`, `FieldInfo`, `MethodInfo`, `ParseResult` được định nghĩa trong Task 2 và dùng nhất quán trong Tasks 3–8.

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`/README.md`](../../../README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ← | [`/docs/thiet-ke-kien-truc.md`](../../thiet-ke-kien-truc.md) | Kiến trúc đầy đủ: lý do thiết kế, Graph schema, pipeline |
| ← | [`/TASKS.md`](../../../TASKS.md) | Tiến độ tổng thể — đánh dấu task khi hoàn thành |
