---
status: draft
scope: research/odoo-internals
date: 2026-04-21
research_date: 2026-04-22
implications_for:
  - ../architecture/indexer.md
  - ../specs/resolve_model.md
  - ../specs/resolve_field.md
  - ../specs/resolve_method.md
---

# Odoo internals — inheritance + load order

Evidence-based trace of Odoo 17.0 Community Edition source at
`/home/soncrits/git/17.0/odoo/`. Every claim below cites a file:line in
CE 17.0. Where runtime state is genuinely ambiguous (e.g. DB-sourced
views, runtime-patched `_inherit`), the section explicitly calls it out
so the indexer can emit `resolution: unknown` instead of guessing.

---

## 1. Manifest + module load order algorithm

### Algorithm — load order

1. **Manifest parsing.** For each module name, Odoo reads
   `__manifest__.py` (fallback `__openerp__.py`) via `ast.literal_eval`
   and merges it into a default manifest. Key fields used for load
   order: `depends` (list of module names) and `auto_install` (bool or
   iterable). `load_manifest` coerces `auto_install=True` into
   `set(manifest['depends'])`.
2. **Graph construction.** `Graph.add_modules(cr, module_list)` is a
   fix-point loop: pop module `p`; if **all** of `p`'s `depends` are
   already in the graph, call `add_node(p)`; otherwise re-queue.
   Iterates until the queue stops shrinking. Unmet deps are logged and
   the module is dropped.
3. **Depth assignment.** `Graph.add_node` walks `info['depends']` and
   picks the parent with the **highest** `depth`; the new node's depth
   becomes `parent.depth + 1`. Ties on depth are resolved by
   "last-seen parent wins" — the loop uses `>=`, so the last-listed
   parent at the max depth becomes `father`. Then `father.add_child`
   appends to `father.children` and **sorts `children` by name**.
4. **Iteration (load order).** `Graph.__iter__` walks levels
   `0, 1, 2, ...`; within each level, modules are yielded
   `sorted((name, module) for ... if module.depth == level)`. So the
   effective load order is **(depth ASC, name ASC)**.
5. **`load_module_graph`** iterates the graph in this order, and for
   each package calls `load_openerp_module(name)` (Python import),
   `registry.load(cr, package)` (build model classes), data/demo
   loading, migration hooks, etc.

### Source references

- `odoo/modules/module.py:303` — `load_manifest`
- `odoo/modules/module.py:357` — `get_manifest` (with `lru_cache`)
- `odoo/modules/module.py:381` — `load_openerp_module`
- `odoo/modules/graph.py:31` — `Graph.add_node` (depth + father)
- `odoo/modules/graph.py:65` — `Graph.add_modules` (fix-point loop)
- `odoo/modules/graph.py:109` — `Graph.__iter__` (depth-then-name)
- `odoo/modules/graph.py:151` — `Node.add_child` (sorts by name)
- `odoo/modules/loading.py:126` — `load_module_graph` (top-level driver)
- `odoo/modules/loading.py:157` — `for index, package in enumerate(graph, 1)`
- `odoo/modules/registry.py:242` — `Registry.load(cr, module)` (consumes
  `MetaModel.module_to_models[module.name]` and calls `_build_model`)

### Implications for indexer — load order

- The indexer **can statically simulate load order** from manifests:
  parse `depends`, compute depth, sort `(depth, name)`. Matches
  Odoo's runtime order as long as `ir_module_module.state` filtering
  and `auto_install` triggers are applied.
- **Tie-break is alphabetical** (not manifest insertion order, not
  filesystem order). The parser MUST sort names within a depth level.
- Two real perturbations remain runtime-only:
  - `pre_upgrade_scripts` and `migrations/` changing effective order
    during upgrades — out of scope for a static index.
  - Dynamic registration via `MetaModel` triggered by `__import__`:
    any module whose Python package fails to import contributes no
    classes. Static parser should trust the AST and flag import
    errors as warnings rather than skip the module silently.

---

## 2. `_inherit` algorithm (standard / extension inheritance)

### Algorithm — `_inherit`

1. **Class collection.** `MetaModel.__new__` records every class
   declaring `_register=True` (default) into
   `MetaModel.module_to_models[module]`, keyed by the module name
   extracted from `cls.__module__` (must start with `odoo.addons.`).
2. **Name normalization.** If `_inherit` is a string, it is converted
   to a single-element list. If `_name` is absent, it defaults to
   `inherit[0]` when `len(inherit) == 1` (pure extension), otherwise
   to the Python class name (multi-inherit / new model).
3. **Registry class build.** Per module, per class,
   `BaseModel._build_model(pool, cr)`:
   - If the model name already exists in the pool (i.e. some previous
     class created it), reuse `pool[name]`. Otherwise create a new
     dynamic class `type(name, (cls,), {...})` with per-registry
     dicts (`_fields`, `_inherit_module`, `_inherit_children`, ...).
   - Iterate `parents = list(cls._inherit) + (['base'] if name != 'base' else [])`.
     For each `parent`:
     - If `parent == name` (pure extension), unpack
       `parent_class.__base_classes` into the `LastOrderedSet`
       `bases`.
     - Else (cross-model inherit), add the parent's registry class to
       `bases` and record
       `ModelClass._inherit_module[parent] = cls._module` plus
       `parent_class._inherit_children.add(name)`.
   - `ModelClass.__base_classes = tuple(bases)` — **authoritative
     override chain.** `LastOrderedSet` preserves first-insertion
     position but updates on re-insertion, so later additions still
     show up at the end of the tuple in a predictable order.
4. **MRO resolution.** `_prepare_setup` assigns
   `cls.__bases__ = cls.__base_classes` and Python's C3 linearization
   produces the effective method lookup. Because `bases` is built
   with the current class first, then parents appended, the
   **latest-loaded module's class sits earliest in the MRO** and
   overrides earlier modules' methods/fields.
5. **Field collection.** `_setup_base` walks
   `cls._model_classes__ = tuple(c for c in cls.mro() if not is_registry_class(c))`
   **in reverse** and collects field definitions per name. If more
   than one class defines the same field name, the framework builds a
   merged field whose `_base_fields` is the ordered tuple of
   definitions (later = higher priority).

### Source references — `_inherit`

- `odoo/models.py:193` — `class MetaModel`; `module_to_models`
  `defaultdict(list)`
- `odoo/models.py:199-220` — `MetaModel.__new__` (module + name
  normalization, `_field_definitions` bootstrap)
- `odoo/models.py:222-235` — `MetaModel.__init__` appends the class
  into `module_to_models[self._module]`
- `odoo/models.py:494-501` — `is_definition_class` /
  `is_registry_class` helpers
- `odoo/models.py:694-770` — `BaseModel._build_model`
- `odoo/models.py:714-715` — implicit `'base'` parent appended
- `odoo/models.py:737-753` — `bases = LastOrderedSet([cls])`; parents
  loop; `_inherit_module[parent] = cls._module`;
  `parent_class._inherit_children.add(name)`;
  `ModelClass.__base_classes = tuple(bases)`
- `odoo/models.py:796-837` — `_build_model_attributes` (per-registry
  `_description`, `_table`, `_sql_constraints`, `_inherits` merge)
- `odoo/models.py:3326-3409` — `_setup_base` (field collection from
  MRO, inherited field expansion, `_rec_name`/`_active_name`)
- `odoo/models.py:3349-3371` — definition merge:
  `len(fields_) == 1` → share directly; else build merged field with
  `_base_fields=tuple(fields_)`
- `odoo/modules/registry.py:265-268` — `registry.load` iterates
  `MetaModel.module_to_models[module.name]` and calls
  `_build_model`, then returns `self.descendants(model_names,
  '_inherit', '_inherits')`

### Key data structures on the registry class

- `_inherit_module` — dict `{parent_name: introducing_module}`. Tells
  you which module turned the current model into a child of a given
  parent.
- `_inherit_children` — `OrderedSet` of model names that extend this
  one. Populated incrementally as children are built.
- `_original_module` — the module that first created the model
  (`_name` not in `_inherit`).
- `__base_classes` — tuple of all definition classes in override
  order; authoritative chain for field/method resolution.

### Edge cases

- **Multi-inherit `_inherit = ['a', 'b']` with new `_name = 'c'`.**
  `bases` is populated in list order (`a` before `b`). Because
  `LastOrderedSet` keeps insertion order and `cls` is inserted first,
  the resulting MRO (after C3) is roughly
  `c_def_class -> a_registry -> b_registry -> base`. A method
  defined in both `a` and `b` resolves to `a`'s (first in the list).
  For **fields**, `_setup_base` still respects the `_base_fields`
  override stack, so later-loaded extensions of `a` or `b` still win
  against earlier ones.
- **Extending the same model twice within one module.** Two classes
  with `_inherit='sale.order'` in the same module both register into
  `module_to_models[module]` and both get added to `bases`. Order is
  the order `MetaModel.__new__` saw them — Python import order
  (top-to-bottom in the file, and the order files are imported by
  `models/__init__.py`).
- **`base` is always an implicit parent.** Every model except `base`
  itself gets `'base'` appended to its parents list at
  `odoo/models.py:715`.

### Implications for indexer

- To determine **which module's override wins** for a given
  `(model, field_or_method)`:
  1. Find all classes defining the model across installed modules.
  2. Order them by module load order (Section 1).
  3. Within one module, preserve file import order (walk
     `models/__init__.py` imports top-to-bottom, then classes
     within each file top-to-bottom).
  4. The **last** class defining the symbol wins for field override
     stacks; for method dispatch, normal Python MRO applies.
- `_inherit_module` is the source of truth but exists only at
  runtime. A static indexer must reconstruct it from
  `(module_name, class._inherit)` pairs.
- For multi-`_inherit` lists, the indexer must record the full list
  in order; method resolution picks the **first** parent that
  defines the method (Python MRO), not the last.

---

## 3. `_inherits` algorithm (delegation / composition inheritance)

### Algorithm

1. **Declaration.** `_inherits = {'parent.model': 'fk_field_name', ...}`.
   Each entry says: this model embeds a delegated pointer to
   `parent.model` via a Many2one field.
2. **FK validation.** `_inherits_check` (`odoo/models.py:3287`):
   - If the named field is missing, auto-create a
     `Many2one(parent, required=True, ondelete='cascade')`.
   - If it exists, **force** `required=True` and `ondelete` to one of
     `('cascade', 'restrict')`, and set `field.delegate = True`.
   - Additionally, any existing Many2one with `delegate=True` and no
     `related` is folded back into `_inherits` and the parent's
     `_inherits_children` set.
3. **Field delegation.** `_add_inherited_fields`
   (`odoo/models.py:3256`) collects every field of every `_inherits`
   parent and, for each field **not already present on the child**,
   creates a shadow field:

   ```python
   Field(
       inherited=True,
       inherited_field=field,
       related=f"{parent_fname}.{name}",
       related_sudo=False,
       copy=field.copy,
       readonly=field.readonly,
       ...
   )
   ```

   So delegated fields are **implemented as `related` fields** whose
   path is `<fk>.<field>`. They read/write through the FK, do not
   duplicate storage, and respect the parent's access rules
   (`related_sudo=False`).
4. **Inherits aggregation across bases.** `_build_model_attributes`
   walks `cls.__base_classes` in reverse and merges every base
   class's `_inherits` into the registry class's `_inherits` dict
   (`inherits.update(base._inherits)`). If two definitions both
   declare `_inherits['parent']`, the later one's FK name wins (dict
   `update` semantics). The parent model's `_inherits_children` set
   is then updated.

### Source references — `_inherits`

- `odoo/models.py:572-589` — `_inherits = frozendict()` base
  definition, docstring noting the last-entry-wins rule on name
  collision across parents
- `odoo/models.py:801-832` — `_build_model_attributes`:
  `inherits.update(base._inherits)`; `pool[parent_name]._inherits_children.add(cls._name)`
- `odoo/models.py:3256-3284` — `_add_inherited_fields` creates shadow
  `related` fields
- `odoo/models.py:3287-3310` — `_inherits_check` validates FK,
  auto-adds one if missing, forces `required + ondelete`, sets
  `delegate=True`
- `odoo/models.py:3374-3382` — `_setup_base` sequence:
  `_inherits_check()` → recurse into each parent's `_setup_base()` →
  `_add_inherited_fields()`

### Canonical example — `product.product`

```python
# odoo/addons/product/models/product_product.py:15
class ProductProduct(models.Model):
    _name = "product.product"
    _inherits = {'product.template': 'product_tmpl_id'}
    _inherit = ['mail.thread', 'mail.activity.mixin']
```

Effect:

- `product.product` gets every field of `product.template` as a
  `related='product_tmpl_id.<field>'` shadow.
- `product.template._inherits_children` contains `'product.product'`.
- `product_tmpl_id` is forced
  `required=True, ondelete='cascade', delegate=True`.
- `product.product` also **extends** (not delegates) `mail.thread`
  and `mail.activity.mixin` via `_inherit` — the two mechanisms
  compose without interference.

Another in-tree example: `res.users` `_inherits = {'res.partner': 'partner_id'}`
at `odoo/addons/base/models/res_users.py:331`.

### Implications for indexer — `_inherits`

- A `resolve_field` call for `('product.product', 'list_price')`
  should return a synthetic entry:
  - `kind: "inherited"`
  - `source_model: "product.template"`
  - `via_field: "product_tmpl_id"`
  - `definition_module`: the module that introduced the parent
    field.
- **`_inherits` ≠ `_inherit`.** Parser MUST not merge them:
  - `_inherits`: dict, delegation, runtime = related fields + FK.
  - `_inherit`: str/list, Python extension, runtime = MRO-based
    class composition sharing `_name`.
- **Collision rule:** if the child also defines a field locally with
  the same name, `_add_inherited_fields` skips the inherited copy
  (`if name not in self._fields`). The local definition wins.

---

## 4. View XPath inheritance algorithm

### Algorithm — view inheritance

1. **View discovery.** Every view has `inherit_id` (parent) and
   `mode` (`'primary'` or `'extension'`). A request for a view's
   arch starts at a **primary** view and pulls every descendant
   whose inherit chain terminates at that primary.
2. **Descendant fetch.** `_get_inheriting_views`
   (`ir_ui_view.py:602`) runs a **recursive CTE** starting from
   `self.ids`, following `inherit_id` edges filtered by
   `ir_ui_view.mode = 'extension'` and
   `coalesce(ir_ui_view.model, '') = coalesce(parent.model, '')`,
   ordering final result by `priority ASC, id ASC`.
   - **Priority** is a developer-set integer (default 16); lower =
     applied earlier.
   - **ID** provides stable secondary order (earlier-created views
     win on tie), which correlates with module load order because
     `base` views are inserted before `stock` views, etc.
3. **Hierarchy flattening.** `_get_combined_arch`
   (`ir_ui_view.py:935`) walks up `inherit_id` to the topmost
   parent, collects the full descendant set, builds a `hierarchy`
   dict `{parent_view: [child_views]}`, then calls
   `_combine(hierarchy)` on the root.
4. **Combine traversal.** `_combine` (`ir_ui_view.py:844`) does a
   **pre-order depth-first traversal** using a double-ended queue:
   - For each view popped off the queue, parse its `arch`, call
     `apply_inheritance_specs(combined_arch, arch)`.
   - Extension children are pushed **left** (stack-like) so they are
     processed immediately.
   - **Primary child views** are pushed at the **right** (tail) of
     the deque so they are processed **after** all extensions of the
     current primary — i.e. extensions are layered first, then
     primary descendants inherit the already-combined arch.
5. **Spec application.** `apply_inheritance_specs` delegates to
   `odoo/tools/template_inheritance.py:98`:
   - `locate_node` resolves the target:
     - `<xpath expr="...">`: runs `etree.ETXPath(expr)(arch)`; picks
       first match (or `None`).
     - `<field name="foo">`: scans `arch.iter('field')`; matches on
       `name` only.
     - Any other tag: first element whose non-`position` attributes
       all match the spec's.
   - Then applies `position`:
     - `inside` (default): append spec children into located node.
     - `after` / `before`: insert spec children as siblings.
     - `replace` (default `mode='outer'`): remove the located node,
       insert spec children in its place. If the located node is
       the root, the entire arch becomes the spec's content.
     - `replace` with `mode='inner'`: wipe children and text of the
       located node, append spec children.
     - `attributes`: iterate `<attribute name="X">` children;
       `add`/`remove` for list-like attrs, else overwrite.
     - `move` (as child of an outer `after`/`before`/`inside`):
       extract a node from the source and re-attach at the new
       position.

### Source references — views

- `odoo/addons/base/models/ir_ui_view.py:590-600` —
  `_get_inheriting_views_domain`, `_get_filter_xmlid_query`
- `odoo/addons/base/models/ir_ui_view.py:602-655` — recursive CTE,
  final `ORDER BY v.priority, v.id`
- `odoo/addons/base/models/ir_ui_view.py:657-675` —
  `_filter_loaded_views` (upgrade safety: only views whose module is
  already in `pool._init_modules`)
- `odoo/addons/base/models/ir_ui_view.py:741-754` — `locate_node`
  wrapper
- `odoo/addons/base/models/ir_ui_view.py:818-842` —
  `apply_inheritance_specs` wrapper + error wrapping
- `odoo/addons/base/models/ir_ui_view.py:844-912` — `_combine` deque
  traversal (extensions via `appendleft`, primaries via `append`)
- `odoo/addons/base/models/ir_ui_view.py:935-963` —
  `_get_combined_arch`
- `odoo/tools/template_inheritance.py:62-95` — `locate_node`
  implementation
- `odoo/tools/template_inheritance.py:98-230+` —
  `apply_inheritance_specs` core dispatcher

### Implications for indexer — views

- **Order of application** for multiple views inheriting the same
  parent is deterministic from static data alone: `(priority, id)`.
  Since `id` is DB-assigned, the static indexer must approximate
  with `(priority, module_load_order, file_order_in_manifest,
  position_in_file)`. `resolve_view` docs must warn that exact `id`
  ordering is only available from a running DB.
- **`position="replace"` is the dangerous case.** It replaces the
  whole matched subtree — any downstream extension targeting a node
  inside the replaced subtree will fail to locate and raise
  `ValueError`. The indexer should flag "view X uses
  `position=replace` on Y" as a potential breaking edit to
  downstream consumers.
- **`<field name="X">` spec only matches by `name`.** Two fields
  with the same name at different places in the arch are ambiguous
  — first `.iter('field')` match wins. Parser should not try to
  disambiguate.
- **Primary vs extension matters for traversal.** `_combine` treats
  them differently. Carry `mode` explicitly.
- **Cross-model edges.** The CTE requires matching `model` between
  child and parent — an extension view cannot change the target
  model. If an inheriting view has a different `model`, it becomes
  an implicit primary-ish detach and is not followed in combination.

---

## 5. Dynamic / runtime `_inherit` (edge case)

### What we found

A grep across CE 17.0 (`odoo/` + `addons/`) for runtime assignment to
`_inherit` (`self._inherit = ...`, `cls._inherit = ...`,
`_inherit = property(...)`) returns **zero in-tree occurrences**:

```text
grep -rn "self\._inherit\s*=\|cls\._inherit\s*="
  /home/soncrits/git/17.0/odoo/odoo /home/soncrits/git/17.0/odoo/addons
# (no results)
```

`_inherit` is consumed at **class creation time** by
`MetaModel.__new__` (`odoo/models.py:214`) and again at
**registry-build time** by `_build_model` (`odoo/models.py:713`).
After `MetaModel.__init__` runs, the class is already registered
into `MetaModel.module_to_models[module]` and any later mutation of
the `_inherit` attribute is **ignored** by the loading path — the
metaclass never re-reads it. Runtime override is therefore not a
supported mechanism in CE 17.0.

### Edge cases that look dynamic but aren't

- **`_register = False`** (e.g.
  `odoo/addons/base/models/ir_qweb.py:2702`). These classes opt out
  of registry insertion entirely. A parent class can be
  `_register=False` and a subclass turns it on. Parser must
  evaluate `_register` statically; if True (default) and class is
  inside an `odoo.addons.<mod>` package, treat as registered.
- **Conditional imports.** A module's `models/__init__.py` may
  import a file only if an optional dependency is installed. The
  `_inherit` list inside each file is still static; what is
  conditional is whether the class is loaded at all. Parser should
  record the import statement as conditional (guarded by
  `try/except ImportError`) and emit `resolution: conditional` for
  symbols introduced in that file.
- **Studio / custom models.** `ir.model.fields` rows with
  `state = 'manual'` inject fields at runtime via
  `ir.model.fields._add_manual_fields`, called from `_setup_base`
  at `odoo/models.py:3374`. These are **database-driven** and
  invisible to a static AST parser. Recommended handling:
  `resolution: unknown` plus an optional live-DB introspection
  mode.
- **`studio_customization`** is explicitly filtered out of graph
  construction (`odoo/modules/graph.py:18`). Static indexers can
  safely ignore it.

### Source references — dynamic `_inherit`

- `odoo/models.py:199-220` — `MetaModel.__new__` reads `_inherit`
  once
- `odoo/models.py:713` — `_build_model` re-reads `cls._inherit` at
  registry build
- `odoo/models.py:3374-3375` — manual field injection (runtime,
  DB-sourced)
- `odoo/modules/graph.py:16-22` — `_ignored_modules` (filters
  `studio_customization` + rows with `imported=True`)
- `odoo/addons/base/models/ir_qweb.py:2702` — real example of
  `_register = False`

### Implications for indexer — dynamic `_inherit`

- **Default stance: trust static `_inherit`.** No CE 17.0 code path
  mutates it after class creation. The parser can safely treat
  `_inherit = [...]` literals in AST as authoritative.
- **Emit `resolution: unknown` only when:**
  1. The class is inside a `try/except ImportError` guard
     (optional dependency).
  2. `_register = False` is set on the class or inherited from a
     base class that the parser cannot resolve statically.
  3. The model is custom (`ir.model` row with `state='manual'`,
     or Studio-generated), i.e. DB-origin.
- **Do not speculate on studio / third-party runtime patches.** If
  a downstream module uses `__class__` monkey-patching or `type()`
  hackery, flag the module at parse time and fall back to
  `resolution: unknown` rather than silently producing wrong
  output.

---

## Summary table — where each algorithm lives

| Algorithm | Primary file | Key functions |
| --- | --- | --- |
| Manifest parsing | `odoo/modules/module.py` | `load_manifest:303`, `get_manifest:357`, `load_openerp_module:381` |
| Dependency graph | `odoo/modules/graph.py` | `Graph.add_node:31`, `Graph.add_modules:65`, `Graph.__iter__:109` |
| Load driver | `odoo/modules/loading.py` | `load_module_graph:126`, `load_modules:376` |
| Registry build | `odoo/modules/registry.py` | `Registry.load:242`, `setup_models:273` |
| Metaclass registration | `odoo/models.py` | `MetaModel.__new__:199`, `__init__:222` |
| `_inherit` chain | `odoo/models.py` | `_build_model:694`, `_build_model_attributes:796` |
| Field setup | `odoo/models.py` | `_setup_base:3326`, `_setup_fields:3412` |
| `_inherits` delegation | `odoo/models.py` | `_add_inherited_fields:3256`, `_inherits_check:3287` |
| View inheritance | `odoo/addons/base/models/ir_ui_view.py` | `_get_inheriting_views:602`, `_combine:844`, `_get_combined_arch:935` |
| XPath spec apply | `odoo/tools/template_inheritance.py` | `locate_node:62`, `apply_inheritance_specs:98` |

---

## Open items (for follow-up traces)

- **QWeb template inheritance** (for P4 `resolve_template`) — not
  yet traced. `_combine` handles both regular views and QWeb
  templates (`t-name`, `t-extend`), but QWeb adds its own
  compile-time rendering path in `odoo/addons/base/models/ir_qweb.py`
  that may apply further node-level transformations. TBD — need to
  trace the `ir_qweb` compile pipeline.
- **Migration hooks affecting order.** `pre_upgrade_scripts`,
  `pre_init_hook`, `post_init_hook`, and `migrations/<version>/`
  run around registry build and can mutate the DB. Out of scope for
  the static indexer; document as "runtime drift, cannot be
  reconstructed statically".
- **`_abstract` → concrete promotion.** `_build_model_check_base`
  (`odoo/models.py:773`) raises if a concrete class tries to extend
  an abstract model. Not a real dynamic-inherit case, but the
  indexer should record the `_abstract` flag so it can validate
  cross-module chains.
