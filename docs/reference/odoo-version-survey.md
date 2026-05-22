# Khảo Sát Phiên Bản Odoo — v8 → v19

> **Ground-truth cho indexer:** Tài liệu này là nguồn sự thật (SSOT) cho các quyết định parser theo version.
> Mọi thay đổi phải cross-reference với `docs/adr/0032-parser-hooks-registry.md` (VersionRegistry) và
> `docs/adr/0033-odoo-tools-symbol-coverage.md` (odoo.tools lifecycle).
>
> **Portable:** Không có path `/home/<user>/` hay machine-specific content. Mọi command dùng `<VENV>` và `<ODOO_SRC>` làm placeholder.
>
> **Derived from:** PR #160 WI-1 — WI-6 (reindex-prep DB-impact wave v8→v19, 2026-05-22).

---

## 1. Ma Trận Phiên Bản v8 → v19

Bảng dưới đây tóm tắt các đặc tính quan trọng nhất của từng major version Odoo,
dùng để định hướng parser và indexer của OSM.

| Version | Manifest | Core dir | ORM era | `@api.multi` | View tag mặc định | OWL / JS | Styles + Bootstrap | Python |
|---------|----------|----------|---------|-------------|-------------------|----------|-------------------|--------|
| **v8** | `__openerp__.py` | `openerp/` | era1 — `_columns` dict | Có (`@api.cr_uid_ids`) | `<tree>` | `web.Widget` (không có `odoo.define`) | LESS, Bootstrap 3.2.0 | 2.7 |
| **v9** | `__openerp__.py` | `openerp/` | MIXED era1 + era2 | Có | `<tree>` | `odoo.define` + Widget | LESS (+sass), Bootstrap 3.3.5 | 2.7 |
| **v10** | `__manifest__.py` (+ `__openerp__.py` dual-support) | `odoo/` | era2 | Có | `<tree>` | `odoo.define` + Widget | LESS, Bootstrap 3.3.5 | 2.7 |
| **v11** | `__manifest__.py` | `odoo/` | era2 | Có | `<tree>` | `odoo.define` + Widget | LESS → SCSS migration, Bootstrap 3.3.4 | 3.5+ |
| **v12** | `__manifest__.py` | `odoo/` | era2 | Có | `<tree>` | `odoo.define` + Widget | **SCSS**, Bootstrap 4.1.3 | 3.5+ |
| **v13** | `__manifest__.py` | `odoo/` | era2 | **Removed** | `<tree>` | `odoo.define` (không OWL) | SCSS, Bootstrap 4.3 | 3.6+ |
| **v14** | `__manifest__.py` | `odoo/` | era2 | - | `<tree>` | **OWL introduced** + Widget hybrid | SCSS, Bootstrap 4.4 | 3.6+ |
| **v15** | `__manifest__.py` + `assets` dict | `odoo/` | era2 | - | `<tree>` | OWL + legacy (~30/70 mix) | SCSS, Bootstrap 5.1.3 | 3.7+ |
| **v16** | `__manifest__.py` | `odoo/` | era2 | - | `<tree>` | OWL dominant; Widget deprecated | SCSS, Bootstrap 5.1.3 | 3.7+ |
| **v17** | `__manifest__.py` | `odoo/` (+ ORM refs) | era2 | - | `<tree>` + `<list>` alias | **OWL-only** | SCSS, Bootstrap 5.1.3 | 3.10+ |
| **v18** | `__manifest__.py` | `odoo/` | era2 | - | **`<list>`** (0 `<tree>`) | 100% OWL | SCSS, Bootstrap 5.3.3 | 3.10+ |
| **v19** | `__manifest__.py` | `odoo/orm/` split | era2 | - | `<list>` | OWL + loader | SCSS, Bootstrap 5.3.3 | 3.10+ |

### Ghi chú chi tiết

**Manifest file:**
- v8/v9: `__openerp__.py` (key `openerp`). `get_manifest_finder()` trong indexer trả `LegacyManifestFinder` cho `major <= 9`.
- v10: dual-support (`__openerp__.py` vẫn được đọc khi `__manifest__.py` thiếu). Từ v11+: chỉ `__manifest__.py`.

**Core dir:**
- v8/v9: `openerp/` — `_PREFIX_REGISTRY` trả `"openerp/"` cho `major <= 9`.
- v10-v18: `odoo/`.
- v19: module ORM được chuyển sang `odoo/orm/` — `_resolve_core_paths()` trong `parser_odoo_core.py` có fallback cho `odoo/fields.py`, `odoo/models.py`, `odoo/api.py` (path-existence check, không phải version-branch).

**ORM era:**
- Era1 (v8-v9): `_columns = {field_name: fields.Char(...)}` dict. Parser dùng text-regex (`_parse_era1_text()`). `FIELD_TYPES_LEGACY` bao gồm `"function"`, `"related"`, `"dummy"`, `"sparse"`.
- Era2 (v10+): `fields.Char(...)` class attributes — AST parse chuẩn. `_ERA_REGISTRY` (ADR-0032) trả `"era1"` cho `major <= 9`, `"era2"` cho `major >= 10`.
- MIXED (v9): một số module bắt đầu dùng cú pháp era2 nhưng `_columns` vẫn phổ biến.

**`@api.multi`:**
- Có mặt từ v8 đến v12 (decorator trên methods xử lý nhiều record).
- Bị xóa trong v13 (`api.multi removed` — `lookup_core_api("api.multi","13.0")` trả `status='removed'`).

**View tag:**
- v8-v16: `<tree>` là tag chuẩn cho list views.
- v17: `<list>` được giới thiệu như alias cho `<tree>`.
- v18+: `<list>` là tag duy nhất; `<tree>` không còn được parse.
- **Tác động MCP:** `view_type` filter trong `model_inspect`/`module_inspect` phải chấp nhận cả `"tree"` và `"list"`.

**OWL / JS:**
- v8/v9: `web.Widget` trực tiếp, không có `odoo.define`.
- v10-v13: `odoo.define("module_name.ClassName", ...)` pattern. Era1/era2 JS.
- v14: OWL 1 được giới thiệu — `Component` class từ `@odoo/owl`. Widget vẫn tồn tại.
- v15/v16: OWL 2, Widget dần bị loại bỏ. Era3: `odoo.define` song song với `import {Component}`.
- v17+: OWL-only (100% component). Era3 JS patch detection (`_extract_era3_patches`) chỉ chạy cho `major >= 14` — xem `_OWL_ENABLED_REGISTRY` (ADR-0032).

**Styles:**
- v8-v11: LESS (Bootstrap 3.x). Parser: `src/indexer/parser_less.py`.
- v12+: SCSS (Bootstrap 4/5). Parser: `src/indexer/parser_scss.py`.
- Bootstrap version (major.minor used by the indexer): xem `src/indexer/spec_data/bootstrap_versions.json`. The patch-level versions in the table below are from the upstream Odoo source survey; the JSON stores only the major.minor needed by the indexer and intentionally omits patch digits that do not affect compatibility.

---

## 2. `odoo.tools` Lifecycle Map

Bảng này tóm tắt vòng đời các symbol quan trọng trong `odoo.tools` qua các version.
Nguồn sự thật: `src/indexer/spec_data/tools_symbols_X.0.json` (12 files, v8-v19) + `docs/adr/0033-odoo-tools-symbol-coverage.md`.

### 2.1 Bảng lifecycle tóm tắt

| Symbol / API | Thêm vào | Status v16 | Status v17 | Status v18 | Status v19 | Ghi chú |
|---|---|---|---|---|---|---|
| `odoo.tools.SQL` | v17 | not-available | stable | stable | stable | Structured SQL helper. Dùng thay raw string SQL từ v17+. |
| `safe_eval` | v8+ | stable | stable | stable | **deprecated** | Import LUÔN từ `odoo.tools.safe_eval` (submodule), KHÔNG từ `odoo.tools` trực tiếp. Signature thay đổi keyword-only trong v19. `test_expr` bị xóa trong v19. |
| `image_resize_image` | v8 | removed | removed | removed | removed | Bị xóa trong v13. Thay bằng `odoo.tools.image_process`. |
| `image_resize_image_big` | v8 | removed | removed | removed | removed | Như trên. |
| `image_resize_image_medium` | v8 | removed | removed | removed | removed | Như trên. |
| `image_resize_image_small` | v8 | removed | removed | removed | removed | Như trên. |
| `image_process` | v13 | stable | stable | stable | stable | Hàm xử lý ảnh thay thế cho 4 hàm cũ trên. |
| `pycompat` | v8 | stable | stable | deprecated | **dropped** | Module tương thích Python 2/3. Bị xóa khỏi `odoo.tools.__init__` trong v19. Vẫn có file nhưng không export qua `__init__`. |
| `ustr` | v8 | stable | soft-dep | soft-dep | soft-dep | Vẫn tồn tại qua v19 nhưng không cần thiết trong Python 3. |
| `html_escape` | v8 | stable | **deprecated** | deprecated | deprecated | Từ v17: dùng `markupsafe.escape` thay thế. |
| `float_compare` | v8 | stable | stable | stable | deprecated | v19: chuyển sang `odoo.tools.float_utils`, re-exported qua `odoo.tools`. |
| `float_round` | v8 | stable | stable | stable | deprecated | Như `float_compare`. |
| `get_modules` | v8 | stable | stable | **deprecated** | deprecated | v18: path module thay đổi. |
| `format_datetime` | v13 | stable | stable | stable | stable | Thêm trong v13 cùng với locale utils. |
| `format_amount` | v13 | stable | stable | stable | stable | Như `format_datetime`. |
| `get_lang` | v13 | stable | stable | stable | stable | Helper lấy lang record từ env. |
| `date_utils` | v12 | stable | stable | stable | stable | Thêm trong v12. |
| `js_transpiler` | v15 | stable | stable | stable | stable | Thêm trong v15 cùng với OWL 2 build pipeline. |

### 2.2 Cách dùng `safe_eval` đúng (mọi version)

```python
# ĐÚNG — import trực tiếp từ submodule
from odoo.tools.safe_eval import safe_eval

result = safe_eval(expr, locals_dict={}, globals_dict={})

# SAI — đừng import từ odoo.tools trực tiếp (v19 breaking change)
from odoo.tools import safe_eval  # risky từ v19+
```

**v19 breaking changes cho `safe_eval`:**
- `test_expr` parameter bị xóa — nếu code đang dùng `safe_eval(expr, mode='eval', test_expr=True)` → xóa `test_expr`.
- Một số args trở thành keyword-only — gọi explicitly (`safe_eval(expr, locals_dict=d)` thay vì positional).

### 2.3 Image API migration v8-v12 → v13+

```python
# Trước v13 (đã bị xóa — ĐỪNG dùng):
from odoo.tools import image_resize_image  # DeprecationError từ v13+
image_resize_image(base64_source, max_width=1920, max_height=1080)

# v13+ (stable):
from odoo.tools.image import image_process
image_process(base64_source, size=(1920, 1080))
```

---

## 3. View Tag Migration

Dùng khi query hoặc viết view XML cho multi-version codebase.

| Version range | Tag trong source | `view_type` trong OSM graph | Ghi chú |
|---|---|---|---|
| v8-v16 | `<tree>` | `"tree"` | Tag chuẩn cho list views. |
| v17 | `<tree>` hoặc `<list>` | `"tree"` hoặc `"list"` | Alias được giới thiệu. |
| v18+ | `<list>` | `"list"` | `<tree>` không còn valid. |

Khi dùng `model_inspect(method="views", view_type="tree")` với dữ liệu v18+: kết quả trả về 0 (vì graph lưu `"list"`). Truyền `view_type="list"` hoặc bỏ filter để lấy tất cả.

---

## 4. Manifest Assets Format

```python
# v8-v14: assets khai báo trong QWeb view XML hoặc qua addons/__manifest__.py key không chuẩn
{
    'name': 'My Module',
    'version': '14.0.1.0.0',
    'depends': ['web'],
    # KHÔNG có 'assets' key — CSS/JS khai báo trong <xpath> view XML
}

# v15+: 'assets' dict trong __manifest__.py (bundle-based)
{
    'name': 'My Module',
    'version': '15.0.1.0.0',
    'depends': ['web'],
    'assets': {
        'web.assets_backend': [
            'my_module/static/src/scss/main.scss',
            'my_module/static/src/js/main.js',
        ],
    },
}
```

---

## 5. Tác Động Lên OSM Parsers

Bảng này liên kết từng đặc điểm version với parser/registry cụ thể trong codebase OSM.

| Đặc điểm | Version range | File xử lý | Cơ chế |
|---|---|---|---|
| `__openerp__.py` manifest | v8-v9 | `src/indexer/registry.py` | `LegacyManifestFinder` |
| `openerp/` core dir | v8-v9 | `src/indexer/parser_odoo_core.py` | `_PREFIX_REGISTRY` (ADR-0032) |
| Era1 `_columns` parse | v8-v9 | `src/indexer/parser_python.py` | `_ERA_REGISTRY` (ADR-0032) + `_parse_era1_text()` |
| LESS stylesheet | v8-v11 | `src/indexer/parser_less.py` | regex-based (ADR-0025 addendum) |
| `openerp/` CLI path | v8-v9 | `src/indexer/parser_cli.py` | `_PKG_PREFIX_REGISTRY` (ADR-0032 PR#160) |
| SCSS stylesheet | v12+ | `src/indexer/parser_scss.py` | regex + tree-sitter-css fallback (ADR-0025) |
| OWL extraction | v14+ | `src/indexer/parser_js.py` | `_OWL_ENABLED_REGISTRY` (ADR-0032) |
| `<list>` view tag | v17+ | query time — không thay đổi parser | `view_type` filter accept cả `"tree"` và `"list"` |
| Generic field `Field[int]` | v18-v19 | `src/indexer/parser_odoo_core.py` | `ast.Subscript` detection (WI-1 PR#160) |
| `odoo/orm/` split | v19 | `src/indexer/parser_odoo_core.py` | `_resolve_core_paths()` path-existence fallback |
| `odoo.tools` symbols | v8-v19 | `src/indexer/parser_tools_symbols.py` | Curated JSON per version (ADR-0033) |

---

## 6. v20 — Cách Thêm Support

Nhờ ADR-0032 (`VersionRegistry`), thêm v20 là thao tác cục bộ trong registry list:

```python
# parser_python.py — nếu v20 có era3 (giả định):
_ERA_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8,  LEGACY_ERA_MAX_MAJOR, "era1"),
    (10, 19,                   "era2"),  # cap: đổi từ open-ended sang bounded
    (20, None,                 "era3"),  # thêm v20
])

# Nếu v20 giữ nguyên era2 (không thay đổi):
# _ERA_REGISTRY không cần sửa — entry (10, None, "era2") đã cover v20 automatically.
```

Xem `docs/adr/0032-parser-hooks-registry.md §v20` cho ví dụ đầy đủ.

---

## Tài Liệu Liên Quan

| File | Nội dung |
|------|----------|
| `docs/adr/0032-parser-hooks-registry.md` | `VersionRegistry` design + v20 extension pattern |
| `docs/adr/0033-odoo-tools-symbol-coverage.md` | `odoo.tools` curated symbol policy + safe_eval lifecycle |
| `docs/adr/0025-css-scss-indexing.md` | CSS/SCSS/LESS indexing schema |
| `docs/adr/0002-spec-schema-policy.md` | `_DEPRECATED_API_SYMBOLS` policy (19 entries v2) |
| `docs/adr/0005-core-coverage-version-paths.md` | `openerp/` vs `odoo/` path resolution |
| `src/indexer/spec_data/bootstrap_versions.json` | Bootstrap version per Odoo major (curated) |
| `src/indexer/version_registry.py` | `VersionRegistry` implementation |
| `docs/deploy/reindex-v8-v19-runbook.md` | Ops runbook — prod reindex sau PR #160 |
