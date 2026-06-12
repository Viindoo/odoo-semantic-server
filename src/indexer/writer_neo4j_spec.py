# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/writer_neo4j_spec.py
"""Spec-layer Neo4j writers — CoreSymbol / LintRule / CLI* / PatternExample / LintViolation.

Extracted from writer_neo4j.py (B5 structural split, no behaviour change). Owns
the batch writers for the M4.5 spec layer (CoreSymbol, LintRule, CLICommand,
CLIFlag, cross-version REPLACED_BY edges), the M4.6 pattern layer
(PatternExample), and the M11 WI-E RelaxNG LintViolation layer. Every Cypher
MERGE here is byte-identical to the original.

Only ``_write_lint_violations_batch`` references the shared ``_profile_union_set``
Cypher fragment (ADR-0034 SSOT); it is imported lazily inside that function body
to avoid an import cycle (writer_neo4j re-exports this module at the bottom) — see
writer_neo4j_orm for the full rationale; the function-local import keeps this
child cold-importable. None of these batch writers emit log records, so this
module carries no module-level logger.
"""
from src.constants import (
    REL_CHECKS,
    REL_HAS_VIOLATION,
    REL_OF_COMMAND,
    REL_REPLACED_BY,
    REL_USES_CORE_SYMBOL,
)

from .models import (
    CLICommandInfo,
    CLIFlagInfo,
    CoreSymbolInfo,
    LintRuleInfo,
    LintViolationInfo,
    PatternExample,
    to_repo_relative,
)


def _write_core_symbols_batch(tx, symbols: list[CoreSymbolInfo]) -> None:
    for s in symbols:
        tx.run("""
            MERGE (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
            SET cs.kind = $kind,
                cs.signature = $sig,
                cs.file_path = $fp,
                cs.line = $line,
                cs.status = $status,
                cs.replacement_qname = $repl
        """, qn=s.qualified_name, v=s.odoo_version,
             kind=s.kind, sig=s.signature, fp=s.file_path,
             line=s.line, status=s.status, repl=s.replacement_qname)


def _write_replaced_by_edges(tx, replaced: list[tuple[str, str]],
                             from_version: str, to_version: str) -> None:
    for old_qn, new_qn in replaced:
        tx.run(f"""
            MATCH (a:CoreSymbol {{qualified_name: $old_qn, odoo_version: $vfrom}})
            MATCH (b:CoreSymbol {{qualified_name: $new_qn, odoo_version: $vto}})
            MERGE (a)-[:{REL_REPLACED_BY}]->(b)
        """, old_qn=old_qn, new_qn=new_qn,
             vfrom=from_version, vto=to_version)


def _write_lint_rules_batch(tx, rules: list[LintRuleInfo]) -> None:
    for r in rules:
        tx.run("""
            MERGE (l:LintRule {rule_id: $rid, odoo_version: $v})
            SET l.kind = $kind,
                l.message = $msg,
                l.severity = $sev,
                l.file_pattern = $fp,
                l.fix_template = $fix,
                l.core_symbol_qname = $cs,
                l.code_pattern = $cp
        """, rid=r.rule_id, v=r.odoo_version, kind=r.kind,
             msg=r.message, sev=r.severity, fp=r.file_pattern,
             fix=r.fix_template, cs=r.core_symbol_qname,
             cp=r.code_pattern)
        # CHECKS edge: when rule is bound to a specific CoreSymbol, link them.
        if r.core_symbol_qname:
            tx.run(f"""
                MATCH (l:LintRule {{rule_id: $rid, odoo_version: $v}})
                MATCH (cs:CoreSymbol {{qualified_name: $cs_qn, odoo_version: $v}})
                MERGE (l)-[:{REL_CHECKS}]->(cs)
            """, rid=r.rule_id, v=r.odoo_version, cs_qn=r.core_symbol_qname)


def _write_cli_commands_batch(tx, commands: list[CLICommandInfo]) -> None:
    for c in commands:
        tx.run("""
            MERGE (c:CLICommand {name: $name, odoo_version: $v})
            SET c.description = $desc,
                c.file_path = $fp
        """, name=c.name, v=c.odoo_version,
             desc=c.description, fp=c.file_path)


def _write_cli_flags_batch(tx, flags: list[CLIFlagInfo]) -> None:
    for f in flags:
        tx.run("""
            MERGE (f:CLIFlag {flag_name: $fn, command_name: $cmd, odoo_version: $v})
            SET f.status = $status,
                f.default = $default,
                f.type = $type,
                f.help = $help,
                f.replacement_flag_name = $repl,
                f.env_name = $env,
                f.posix_only = $posix
        """, fn=f.flag_name, cmd=f.command_name, v=f.odoo_version,
             status=f.status, default=f.default, type=f.type, help=f.help,
             repl=f.replacement_flag_name, env=f.env_name, posix=f.posix_only)
        # OF_COMMAND edge: link the flag to its command if the CLICommand exists.
        tx.run(f"""
            MATCH (f:CLIFlag {{flag_name: $fn, command_name: $cmd, odoo_version: $v}})
            MATCH (c:CLICommand {{name: $cmd, odoo_version: $v}})
            MERGE (f)-[:{REL_OF_COMMAND}]->(c)
        """, fn=f.flag_name, cmd=f.command_name, v=f.odoo_version)


def _write_pattern_examples_batch(tx, patterns: list[PatternExample]) -> None:
    """MERGE PatternExample nodes + USES_CORE_SYMBOL edges (silent skip per ADR-0003)."""
    for p in patterns:
        tx.run("""
            MERGE (pe:PatternExample {pattern_id: $pid})
            SET pe.intent_keywords = $kw,
                pe.file_ref = $fr,
                pe.snippet_text = $sn,
                pe.gotchas = $g,
                pe.odoo_version_min = $vmin,
                pe.language = $lang
        """, pid=p.pattern_id, kw=p.intent_keywords, fr=p.file_ref,
             sn=p.snippet_text, g=p.gotchas, vmin=p.odoo_version_min,
             lang=p.language)
        # USES_CORE_SYMBOL edges — silent skip when no CoreSymbol matches
        # (M4.5 not shipped yet, or symbol simply absent at this version).
        for cs_name in p.core_symbol_names:
            tx.run(f"""
                MATCH (pe:PatternExample {{pattern_id: $pid}})
                MATCH (cs:CoreSymbol {{odoo_version: $v}})
                WHERE cs.qualified_name = $cs
                   OR cs.qualified_name ENDS WITH '.' + $cs
                MERGE (pe)-[:{REL_USES_CORE_SYMBOL}]->(cs)
            """, pid=p.pattern_id, v=p.odoo_version_min, cs=cs_name)


def _write_cli_flag_replacements(tx, replaced: list[tuple[str, str]],
                                 command_name: str,
                                 from_version: str, to_version: str) -> None:
    for old_fn, new_fn in replaced:
        tx.run(f"""
            MATCH (a:CLIFlag {{flag_name: $a_fn, command_name: $cmd, odoo_version: $vfrom}})
            MATCH (b:CLIFlag {{flag_name: $b_fn, command_name: $cmd, odoo_version: $vto}})
            MERGE (a)-[:{REL_REPLACED_BY}]->(b)
        """, a_fn=old_fn, b_fn=new_fn, cmd=command_name,
             vfrom=from_version, vto=to_version)


# ---------------------------------------------------------------------------
# RelaxNG LintViolation writer (WI-E, M11)
# ---------------------------------------------------------------------------

def _write_lint_violations_batch(
    tx, violations: list[LintViolationInfo], profiles: list[str],
    repo_root=None,
) -> None:
    """MERGE :LintViolation nodes + :HAS_VIOLATION edge from owning :View.

    Composite MERGE key: (file_path, line, rule, odoo_version).
    The :HAS_VIOLATION edge source is the :View node keyed on (xmlid,
    odoo_version) — i.e. (view)-[:HAS_VIOLATION]->(lv).  Silent skip when the
    View does not yet exist — the edge will be created once the View is written
    (idempotent MERGE on next run).

    ADR-0037: *repo_root* relativizes file_path (a MERGE-key component) the same
    way as Stylesheet — without this, fresh nodes stay absolute-keyed and the
    post-reindex cleanup (ops/cleanup_absolute_path_nodes.cypher) would wrongly
    delete them.  None → stored verbatim (back-compat for callers without it).
    """
    from .writer_neo4j import _profile_union_set

    for v in violations:
        fp_rel = to_repo_relative(v.file_path, repo_root)
        # Upsert the LintViolation node.
        # Composite key (file_path, line, rule, odoo_version) collapses multiple
        # same-line/same-rule messages into one node (last-write-wins, by design).
        tx.run(f"""
            MERGE (lv:LintViolation {{
                file_path: $fp, line: $line,
                rule: $rule, odoo_version: $ver
            }})
            ON CREATE SET lv.message = $msg,
                          lv.severity = $sev,
                          lv.view_xmlid = $xmlid,
                          lv.view_type = $vtype,
                          lv.profile = $profiles
            ON MATCH  SET lv.message = $msg,
                          lv.severity = $sev,
                          lv.view_xmlid = $xmlid,
                          lv.view_type = $vtype,
                          lv.profile =
                              {_profile_union_set("lv")}
        """, fp=fp_rel, line=v.line, rule=v.rule, ver=v.odoo_version,
             msg=v.message, sev=v.severity, xmlid=v.view_xmlid,
             vtype=v.view_type, profiles=profiles)

        # HAS_VIOLATION edge from :View to :LintViolation
        # Silent skip when the View node does not exist yet.
        tx.run(f"""
            MATCH (view:View {{xmlid: $xmlid, odoo_version: $ver}})
            MATCH (lv:LintViolation {{
                file_path: $fp, line: $line,
                rule: $rule, odoo_version: $ver
            }})
            MERGE (view)-[:{REL_HAS_VIOLATION}]->(lv)
        """, xmlid=v.view_xmlid, ver=v.odoo_version,
             fp=fp_rel, line=v.line, rule=v.rule)
