"""Tests for osm.indexer.resolver — 10 curated override-chain scenarios."""

from __future__ import annotations

import pathlib
import tempfile

from osm.indexer.load_order import LoadOrderRecord
from osm.indexer.python_parser import (
    FileParseResult,
    ParsedField,
    ParsedModel,
    parse_file,
    scan_models_package,
)
from osm.indexer.resolver import (
    MethodOverrideLink,
    ResolverResult,
    _c3_linearize,
    _c3_merge,
    compute_field_override_chains,
    compute_method_mro,
    compute_method_override_chains,
    compute_resolver_result,
    synthesize_inherits_fields,
)

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"
RESOLVER_FIXTURES = FIXTURES / "resolver"
PARSER_FIXTURES = FIXTURES / "python_parser"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lo(name: str, depth: int, order: int) -> LoadOrderRecord:
    return LoadOrderRecord(name=name, depth=depth, load_order=order)


def _parse(rel: str) -> FileParseResult:
    return parse_file(FIXTURES / rel)


def _parse_resolver(name: str) -> FileParseResult:
    return parse_file(RESOLVER_FIXTURES / name)


def _make_lo_map(*names: str) -> list[LoadOrderRecord]:
    return [_lo(n, i, i) for i, n in enumerate(names)]


def _restamp_models(
    fr: FileParseResult,
    module_name: str,
    filename: str = "model.py",
) -> FileParseResult:
    """Re-stamp file_path so _file_to_module() can resolve the module name."""
    new_path = f"/addons/{module_name}/models/{filename}"
    new_models = [
        ParsedModel(
            name=m.name,
            inherit=m.inherit,
            inherits=m.inherits,
            table=m.table,
            rec_name=m.rec_name,
            order=m.order,
            abstract=m.abstract,
            transient=m.transient,
            register_false=m.register_false,
            start_line=m.start_line,
            end_line=m.end_line,
            content_hash=m.content_hash,
            file_path=new_path,
            class_name=m.class_name,
            indexer_notes=m.indexer_notes,
        )
        for m in fr.models
    ]
    return FileParseResult(
        models=new_models,
        fields=fr.fields,
        methods=fr.methods,
        notes=fr.notes,
    )


def _stamp(fr: FileParseResult, module_name: str) -> FileParseResult:
    return _restamp_models(fr, module_name)


def _stamp_model_row(
    m: ParsedModel,
    module_name: str,
    filename: str = "model.py",
    name: str | None = ...,  # type: ignore[assignment]
    inherit: tuple[str, ...] | None = None,
    indexer_notes: dict | None = None,
) -> ParsedModel:
    """Return a copy of ParsedModel with updated path (and optionally name/inherit/notes)."""
    return ParsedModel(
        name=m.name if name is ... else name,  # type: ignore[comparison-overlap]
        inherit=m.inherit if inherit is None else inherit,
        inherits=m.inherits,
        table=m.table,
        rec_name=m.rec_name,
        order=m.order,
        abstract=m.abstract,
        transient=m.transient,
        register_false=m.register_false,
        start_line=m.start_line,
        end_line=m.end_line,
        content_hash=m.content_hash,
        file_path=f"/addons/{module_name}/models/{filename}",
        class_name=m.class_name,
        indexer_notes=m.indexer_notes if indexer_notes is None else indexer_notes,
    )


def _copy_field(f: ParsedField) -> ParsedField:
    return ParsedField(
        model_class_name=f.model_class_name,
        field_name=f.field_name,
        field_type=f.field_type,
        compute=f.compute,
        inverse=f.inverse,
        search=f.search,
        store=f.store,
        required=f.required,
        readonly=f.readonly,
        related=f.related,
        default_source=f.default_source,
        comodel_name=f.comodel_name,
        depends=f.depends,
        start_line=f.start_line,
        end_line=f.end_line,
        content_hash=f.content_hash,
        indexer_notes=f.indexer_notes,
    )


def _parse_inline(source: str, module_name: str) -> FileParseResult:
    """Parse inline source text via a temp file, stamped to module_name."""
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as fh:
        fh.write(source)
        tmp_path = pathlib.Path(fh.name)
    fr_raw = parse_file(tmp_path)
    tmp_path.unlink(missing_ok=True)
    return _restamp_models(fr_raw, module_name)


# ---------------------------------------------------------------------------
# Case 1 — Pure extension chain: 1 base + 2 override modules → chain length 3
# ---------------------------------------------------------------------------


class TestCase1PureExtensionChain:
    """amount_total field overridden in 3 sequential modules."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr_base = _stamp(_parse_resolver("mod_base.py"), "mod_base")
        fr_ext1 = _stamp(_parse_resolver("mod_ext1.py"), "mod_ext1")
        fr_ext2 = _stamp(_parse_resolver("mod_ext2.py"), "mod_ext2")
        load_orders = _make_lo_map("mod_base", "mod_ext1", "mod_ext2")
        return [fr_base, fr_ext1, fr_ext2], load_orders

    def _sale_total_chain(self) -> list:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        return sorted(
            [c for c in chains if c.model_name == "sale.order" and c.field_name == "amount_total"],
            key=lambda c: c.load_order,
        )

    def test_chain_length_is_3(self) -> None:
        assert len(self._sale_total_chain()) == 3

    def test_root_has_no_override_of(self) -> None:
        chain = self._sale_total_chain()
        assert chain[0].override_of is None

    def test_chain_link_order(self) -> None:
        chain = self._sale_total_chain()
        assert chain[1].override_of is chain[0]
        assert chain[2].override_of is chain[1]

    def test_module_names_in_order(self) -> None:
        chain = self._sale_total_chain()
        assert chain[0].module_name == "mod_base"
        assert chain[1].module_name == "mod_ext1"
        assert chain[2].module_name == "mod_ext2"

    def test_not_synthesized(self) -> None:
        files, lo = self._build()
        for c in compute_field_override_chains(files, lo):
            assert not c.synthesized


# ---------------------------------------------------------------------------
# Case 2 — Multi-inherit with _name (declared model): fields stay on _name
# ---------------------------------------------------------------------------


class TestCase2MultiInheritDeclaredModel:
    """mailable.record has _name set + _inherit list — fields go to mailable.record only."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr = _stamp(_parse_resolver("mod_multi_inherit.py"), "mod_mailable")
        return [fr], _make_lo_map("mod_mailable")

    def test_field_belongs_to_declared_model(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        field_names = {c.field_name for c in chains if c.model_name == "mailable.record"}
        assert "name" in field_names
        assert "active" in field_names

    def test_no_bleed_to_parents(self) -> None:
        files, lo = self._build()
        for c in compute_field_override_chains(files, lo):
            assert c.model_name not in ("mail.thread", "mail.activity.mixin")


class TestCase2MultiInheritNoName:
    """Extension with no _name and _inherit=['a','b'] contributes to BOTH parent chains."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr_raw = _parse_resolver("mod_multi_inherit.py")
        new_models = [
            _stamp_model_row(
                m,
                "mod_mixin_ext",
                name=None,
                inherit=("mail.thread", "mail.activity.mixin"),
                indexer_notes={},
            )
            for m in fr_raw.models
        ]
        new_fields = [_copy_field(f) for f in fr_raw.fields]
        fr = FileParseResult(
            models=new_models, fields=new_fields, methods=fr_raw.methods, notes={}
        )
        return [fr], _make_lo_map("mod_mixin_ext")

    def test_field_in_both_parent_chains(self) -> None:
        files, lo = self._build()
        model_names = {c.model_name for c in compute_field_override_chains(files, lo)}
        assert "mail.thread" in model_names
        assert "mail.activity.mixin" in model_names

    def test_same_field_name_in_both(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        thread_fields = {c.field_name for c in chains if c.model_name == "mail.thread"}
        mixin_fields = {c.field_name for c in chains if c.model_name == "mail.activity.mixin"}
        assert "name" in thread_fields
        assert "name" in mixin_fields


# ---------------------------------------------------------------------------
# Case 3 — Override of inherited field (mail.thread extends sale.order)
# ---------------------------------------------------------------------------


class TestCase3OverrideInheritedField:
    """sale.order extends mail.thread and redeclares 'name' — chain reflects both."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        mail_src = (
            "from odoo import fields, models\n"
            "class MailThread(models.Model):\n"
            "    _name = 'mail.thread'\n"
            "    name = fields.Char('Name')\n"
        )
        sale_src = (
            "from odoo import fields, models\n"
            "class SaleOrderMail(models.Model):\n"
            "    _name = 'sale.order'\n"
            "    _inherit = ['mail.thread']\n"
            "    name = fields.Char('Sale Reference', required=True)\n"
        )
        fr_mail = _parse_inline(mail_src, "mod_mail")
        fr_sale = _parse_inline(sale_src, "mod_sale")
        return [fr_mail, fr_sale], _make_lo_map("mod_mail", "mod_sale")

    def test_mail_thread_name_chain_length_1(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        mail_name = [
            c for c in chains
            if c.model_name == "mail.thread" and c.field_name == "name"
        ]
        assert len(mail_name) == 1
        assert mail_name[0].override_of is None

    def test_sale_order_name_chain_length_1(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        sale_name = [
            c for c in chains
            if c.model_name == "sale.order" and c.field_name == "name"
        ]
        assert len(sale_name) == 1


# ---------------------------------------------------------------------------
# Case 4 — Override without super(): chain still links correctly
# ---------------------------------------------------------------------------


class TestCase4OverrideWithoutSuper:
    """mod_ext2 overrides action_confirm without calling super() — chain must link."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr_base = _stamp(_parse_resolver("mod_base.py"), "mod_base")
        fr_ext2 = _stamp(_parse_resolver("mod_ext2.py"), "mod_ext2")
        return [fr_base, fr_ext2], _make_lo_map("mod_base", "mod_ext2")

    def _confirm_chain(self) -> list:
        files, lo = self._build()
        return sorted(
            [
                c for c in compute_method_override_chains(files, lo)
                if c.model_name == "sale.order" and c.method_name == "action_confirm"
            ],
            key=lambda c: c.load_order,
        )

    def test_chain_length_is_2(self) -> None:
        assert len(self._confirm_chain()) == 2

    def test_ext2_does_not_call_super(self) -> None:
        chain = self._confirm_chain()
        assert chain[1].source_row is not None
        assert not chain[1].source_row.calls_super

    def test_override_of_linked(self) -> None:
        chain = self._confirm_chain()
        assert chain[0].override_of is None
        assert chain[1].override_of is chain[0]


# ---------------------------------------------------------------------------
# Case 5 — _inherits delegation: list_price synthesized for product.product
# ---------------------------------------------------------------------------


class TestCase5InheritsDelegation:
    """list_price is on product.template only; must be synthesized on product.product."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr = _stamp(_parse_resolver("mod_inherits_child.py"), "mod_product")
        return [fr], _make_lo_map("mod_product")

    def test_list_price_synthesized(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        lp = [
            s for s in synth
            if s.model_name == "product.product" and s.field_name == "list_price"
        ]
        assert len(lp) == 1
        assert lp[0].synthesized is True

    def test_synthesized_via_fk_field(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        lp = next(
            s for s in synth
            if s.model_name == "product.product" and s.field_name == "list_price"
        )
        assert lp.synthesized_via == "product_tmpl_id"

    def test_related_path_derivable(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        lp = next(
            s for s in synth
            if s.model_name == "product.product" and s.field_name == "list_price"
        )
        assert f"{lp.synthesized_via}.{lp.field_name}" == "product_tmpl_id.list_price"

    def test_name_also_synthesized(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        names = {s.field_name for s in synth if s.model_name == "product.product"}
        assert "name" in names


# ---------------------------------------------------------------------------
# Case 6 — _inherits collision: child-local field suppresses synthesis (Risk R1)
# ---------------------------------------------------------------------------


class TestCase6InheritsCollision:
    """child.model locally defines shared_field — synthesis must be suppressed.
    only_parent (not locally defined) MUST still be synthesized.
    """

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr = _stamp(_parse_resolver("mod_inherits_collision.py"), "mod_col")
        return [fr], _make_lo_map("mod_col")

    def test_shared_field_not_synthesized(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        synth_names = {s.field_name for s in synth if s.model_name == "child.model"}
        assert "shared_field" not in synth_names, (
            "shared_field locally defined on child — must not be synthesized (Risk R1)"
        )

    def test_only_parent_synthesized(self) -> None:
        files, lo = self._build()
        synth = synthesize_inherits_fields(files, lo)
        op = [
            s for s in synth
            if s.model_name == "child.model" and s.field_name == "only_parent"
        ]
        assert len(op) == 1
        assert op[0].synthesized is True
        assert op[0].synthesized_via == "parent_id"

    def test_local_shared_field_in_chain_not_synthesized(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        child_shared = [
            c for c in chains
            if c.model_name == "child.model" and c.field_name == "shared_field"
        ]
        assert len(child_shared) == 1
        assert not child_shared[0].synthesized


# ---------------------------------------------------------------------------
# Case 7 — Conditional import: warning surfaces, chain flagged incomplete
# ---------------------------------------------------------------------------


class TestCase7ConditionalImport:
    """OptionalModel is guarded by try/except ImportError — warning must appear."""

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        init_path = PARSER_FIXTURES / "conditional_import" / "__init__.py"
        conditional = scan_models_package(init_path)

        fr_base = parse_file(
            PARSER_FIXTURES / "conditional_import" / "base_mod.py",
            conditional_submodules=conditional,
        )
        fr_opt = parse_file(
            PARSER_FIXTURES / "conditional_import" / "optional_mod.py",
            conditional_submodules=conditional,
        )
        fr_base = _restamp_models(fr_base, "mod_ci", "base_mod.py")
        fr_opt = _restamp_models(fr_opt, "mod_ci", "optional_mod.py")
        return [fr_base, fr_opt], _make_lo_map("mod_ci")

    def test_conditional_warning_emitted(self) -> None:
        files, lo = self._build()
        result = compute_resolver_result(files, lo)
        assert any("conditional" in w.lower() for w in result.warnings)

    def test_base_model_field_chain_present(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        base_fields = [c for c in chains if c.model_name == "base.model"]
        assert len(base_fields) >= 1


# ---------------------------------------------------------------------------
# Case 8 — Dynamic _inherit: chain omitted, warning emitted
# ---------------------------------------------------------------------------


class TestCase8DynamicInherit:
    """DynamicInheritModel has non-literal _inherit → dynamic_inherit=True.
    Resolver must emit no chains and surface a warning.
    """

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr_raw = _parse("python_parser/dynamic_inherit.py")
        fr = _restamp_models(fr_raw, "mod_dyn")
        return [fr], _make_lo_map("mod_dyn")

    def test_no_field_chains(self) -> None:
        files, lo = self._build()
        assert compute_field_override_chains(files, lo) == []

    def test_no_method_chains(self) -> None:
        files, lo = self._build()
        assert compute_method_override_chains(files, lo) == []

    def test_warning_emitted(self) -> None:
        files, lo = self._build()
        result = compute_resolver_result(files, lo)
        assert any("dynamic" in w.lower() for w in result.warnings)


# ---------------------------------------------------------------------------
# Case 9 — _register=False: chain still processed, flag on model preserved
# ---------------------------------------------------------------------------


class TestCase9RegisterFalse:
    """AbstractQWebBase has _register=False. Resolver processes its declared fields;
    the ParsedModel carries register_false_chain=True in indexer_notes.
    """

    def _build(self) -> tuple[list[FileParseResult], list[LoadOrderRecord]]:
        fr_raw = _parse("python_parser/register_false.py")
        fr = _restamp_models(fr_raw, "mod_regfalse")
        return [fr], _make_lo_map("mod_regfalse")

    def test_register_false_flag_on_parsed_model(self) -> None:
        files, _lo = self._build()
        model = files[0].models[0]
        assert model.indexer_notes.get("register_false_chain") is True

    def test_field_chains_still_computed(self) -> None:
        files, lo = self._build()
        chains = compute_field_override_chains(files, lo)
        assert len(chains) >= 1

    def test_chain_entries_are_not_synthesized(self) -> None:
        files, lo = self._build()
        for link in compute_field_override_chains(files, lo):
            assert not link.synthesized


# ---------------------------------------------------------------------------
# Case 10 — Method MRO multi-inherit: MRO differs from linear field-stack
# ---------------------------------------------------------------------------


class TestCase10MethodMROMultiInherit:
    """Model inheriting ['mod.a', 'mod.b'] — C3 MRO puts mod.a before mod.b;
    method defined in mod.b is present in linear chain but MRO resolves correctly.
    """

    def _make_chain(self) -> tuple[list[MethodOverrideLink], dict[str, list[str]]]:
        mod_a = MethodOverrideLink(
            method_row_id=None,
            model_name="mod.a",
            method_name="do_thing",
            module_name="mod_a",
            load_order=0,
            override_of=None,
            source_row=None,
        )
        mod_b = MethodOverrideLink(
            method_row_id=None,
            model_name="mod.b",
            method_name="do_thing",
            module_name="mod_b",
            load_order=1,
            override_of=None,
            source_row=None,
        )
        child = MethodOverrideLink(
            method_row_id=None,
            model_name="mod.child",
            method_name="do_thing",
            module_name="mod_child",
            load_order=2,
            override_of=mod_a,
            source_row=None,
        )
        graph = {"mod.child": ["mod.a", "mod.b"], "mod.a": [], "mod.b": []}
        return [mod_a, mod_b, child], graph

    def test_mro_returns_nonempty(self) -> None:
        chain, graph = self._make_chain()
        result = compute_method_mro("mod.child", "do_thing", chain, graph)
        assert len(result) >= 1

    def test_mro_child_first(self) -> None:
        chain, graph = self._make_chain()
        result = compute_method_mro("mod.child", "do_thing", chain, graph)
        assert result[0].model_name == "mod.child"

    def test_c3_first_parent_before_second(self) -> None:
        graph = {"c": ["a", "b"], "a": [], "b": []}
        mro = _c3_linearize("c", graph)
        assert mro.index("a") < mro.index("b")

    def test_c3_includes_all_nodes(self) -> None:
        graph = {"c": ["a", "b"], "a": [], "b": []}
        assert set(_c3_linearize("c", graph)) == {"c", "a", "b"}

    def test_c3_merge_candidate_selection(self) -> None:
        result = _c3_merge([["a", "b"], ["c", "b"], ["a", "c"]])
        assert result[0] == "a"
        assert set(result) == {"a", "b", "c"}

    def test_mro_empty_for_unknown_model(self) -> None:
        chain, graph = self._make_chain()
        assert compute_method_mro("nonexistent", "do_thing", chain, graph) == []

    def test_linear_chain_differs_from_mro(self) -> None:
        """Verify that the linear chain and MRO are NOT the same for multi-inherit.

        Linear chain orders all entries by load_order; MRO orders by C3 which
        puts first-listed parent before second-listed.
        """
        chain, graph = self._make_chain()
        mro = compute_method_mro("mod.child", "do_thing", chain, graph)
        linear_models = ["mod.a", "mod.b", "mod.child"]
        mro_models = [lnk.model_name for lnk in mro]
        assert mro_models != linear_models


# ---------------------------------------------------------------------------
# ResolverResult integration
# ---------------------------------------------------------------------------


class TestResolverResult:
    def test_result_is_resolver_result_instance(self) -> None:
        fr_base = _stamp(_parse_resolver("mod_base.py"), "mod_base")
        fr_ext1 = _stamp(_parse_resolver("mod_ext1.py"), "mod_ext1")
        lo = _make_lo_map("mod_base", "mod_ext1")
        result = compute_resolver_result([fr_base, fr_ext1], lo)
        assert isinstance(result, ResolverResult)

    def test_result_lists_are_lists(self) -> None:
        fr = _stamp(_parse_resolver("mod_base.py"), "mod_base")
        lo = _make_lo_map("mod_base")
        result = compute_resolver_result([fr], lo)
        assert isinstance(result.field_chains, list)
        assert isinstance(result.method_chains, list)
        assert isinstance(result.synthesized_fields, list)
        assert isinstance(result.warnings, list)

    def test_product_inherits_synthesized_in_result(self) -> None:
        fr = _stamp(_parse_resolver("mod_inherits_child.py"), "mod_product")
        lo = _make_lo_map("mod_product")
        result = compute_resolver_result([fr], lo)
        synth = {
            s.field_name
            for s in result.synthesized_fields
            if s.model_name == "product.product"
        }
        assert "list_price" in synth
        assert "description" in synth
