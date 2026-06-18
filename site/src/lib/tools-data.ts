// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT for the 31 MCP tools and 9 MCP resources.
 *
 *  Extracted from site/src/pages/index.astro (formerly inline arrays at lines
 *  14-39 and 251-280). Both index.astro (#tools section) and the /tools page
 *  import from here — one source, zero drift.
 *
 *  Note: count constants (TOOL_COUNT=31, RESOURCE_COUNT=9) live in
 *  site/src/lib/constants.ts (SSOT, enforced by tests/test_tool_count_sync.py).
 *  This module exports the *content* (name/desc/group), not the count. */

export type ToolGroup =
  | 'resolve'
  | 'version'
  | 'quality'
  | 'superset'
  | 'session'
  | 'stylesheet'
  | 'orm'
  | 'testing';

export interface Tool {
  /** Zero-padded two-digit ordinal, e.g. '01'. */
  num: string;
  name: string;
  desc: string;
  group: ToolGroup;
}

export interface Resource {
  /** URI template, e.g. 'odoo://{v}/model/{name}'. */
  uri: string;
  desc: string;
}

/** Tailwind bg color class for each tool group's accent stripe. */
export const TOOL_GROUP_COLORS: Record<ToolGroup, string> = {
  resolve:    'bg-viindoo-primary',
  version:    'bg-viindoo-secondary',
  quality:    'bg-viindoo-warning',
  superset:   'bg-viindoo-success',
  session:    'bg-viindoo-info',
  stylesheet: 'bg-viindoo-secondary',
  orm:        'bg-viindoo-secondary-bright',
  testing:    'bg-viindoo-warning',
};

/** Human-readable group labels with tool counts (for the legend). */
export const TOOL_GROUP_LABELS: Record<ToolGroup, string> = {
  resolve:    'Resolve (7)',
  version:    'Version (2)',
  quality:    'Quality (2)',
  superset:   'Supersets (4)',
  session:    'Session (4)',
  stylesheet: 'Stylesheet (2)',
  orm:        'ORM (4)',
  testing:    'Testing (6)',
};

/** 31 MCP tools — original 24 lifted from index.astro, +profile_inspect (ADR-0028 WI-4), +6 testing tools (test-surface-index feature). */
export const TOOLS: Tool[] = [
  { num: '01', name: 'find_examples',         desc: 'Pull real-world usage from indexed repos.',                                                               group: 'resolve' },
  { num: '02', name: 'impact_analysis',        desc: 'Measure blast radius of a field change.',                                                                group: 'resolve' },
  { num: '03', name: 'lookup_core_api',        desc: 'Identify ORM, CLI, and framework symbols.',                                                              group: 'resolve' },
  { num: '04', name: 'api_version_diff',       desc: 'Delta between two versions.',                                                                            group: 'version' },
  { num: '05', name: 'find_deprecated_usage',  desc: 'Scan a codebase for deprecated APIs.',                                                                   group: 'version' },
  { num: '06', name: 'lint_check',             desc: 'Odoo-specific lint, not generic Python.',                                                                group: 'quality' },
  { num: '07', name: 'cli_help',               desc: 'odoo-bin flags for your installed version.',                                                             group: 'quality' },
  { num: '08', name: 'suggest_pattern',        desc: 'Curated implementation patterns.',                                                                       group: 'resolve' },
  { num: '09', name: 'check_module_exists',    desc: "Settle \"is this built-in\" debates.",                                                                   group: 'resolve' },
  { num: '10', name: 'find_override_point',    desc: 'Pinpoint the safest hook for custom logic.',                                                             group: 'resolve' },
  { num: '11', name: 'describe_module',        desc: 'Architecture overview of a module: manifest, defined/extended models, view + JS-patch counts.',          group: 'resolve' },
  { num: '12', name: 'model_inspect',          desc: 'Superset of resolve_model/list_fields/list_methods — discriminator-based.',                         group: 'superset' },
  { num: '13', name: 'module_inspect',         desc: 'Superset for module-scoped queries — fields/views/owl/qweb/patches.',                               group: 'superset' },
  { num: '14', name: 'entity_lookup',          desc: 'Superset of resolve_field/resolve_method/resolve_view.',                                                 group: 'superset' },
  { num: '15', name: 'set_active_version',     desc: 'Sticky version for the session — call once, every tool uses it.',                                   group: 'session' },
  { num: '16', name: 'set_active_profile',     desc: 'Sticky profile for the session.',                                                                        group: 'session' },
  { num: '17', name: 'list_available_versions',desc: 'What versions are indexed.',                                                                             group: 'session' },
  { num: '18', name: 'list_available_profiles',desc: 'What profiles you can switch to.',                                                                       group: 'session' },
  { num: '19', name: 'resolve_stylesheet',     desc: 'Full stylesheet chain + variable list for a module.',                                                     group: 'stylesheet' },
  { num: '20', name: 'find_style_override',    desc: 'Trace which module last overrides a CSS selector or custom property.',                                    group: 'stylesheet' },
  { num: '21', name: 'resolve_orm_chain',      desc: 'Trace a dotted field path through the model graph — validates each hop.',                           group: 'orm' },
  { num: '22', name: 'validate_domain',        desc: 'Static-check an Odoo domain: unknown fields, invalid operators per version.',                            group: 'orm' },
  { num: '23', name: 'validate_depends',       desc: 'Validate @api.depends paths before runtime; flags depends-on-id.',                                       group: 'orm' },
  { num: '24', name: 'validate_relation',      desc: "Confirm a relational field's comodel matches the expected model.",                                       group: 'orm' },
  { num: '25', name: 'profile_inspect',        desc: 'Profile composition — ancestor chain, child profiles, repos, and module list for a profile.',           group: 'superset' },
  { num: '26', name: 'find_test_examples',     desc: 'Find real test examples for a model, field, or pattern — returns test + JS chunks.',                    group: 'testing' },
  { num: '27', name: 'tests_covering',         desc: 'Which tests cover a given model, field, or method in the indexed graph.',                               group: 'testing' },
  { num: '28', name: 'test_class_inspect',     desc: 'Inspect a test class: base-class chain, setUp, what it covers, subclassed-by.',                        group: 'testing' },
  { num: '29', name: 'test_base_classes',      desc: 'Which test base class to use (TransactionCase vs HttpCase vs ...) and the cursor/commit contract per version.', group: 'testing' },
  { num: '30', name: 'test_coverage_audit',    desc: 'Fields and methods in a module with no static test coverage.',                                          group: 'testing' },
  { num: '31', name: 'js_test_inspect',        desc: 'Frontend JS test suites (Hoot / QUnit / tour) defined in a module.',                                   group: 'testing' },
];

/** 9 MCP resources — verbatim from README.md "MCP Resources" table + src/mcp/resources.py. */
export const RESOURCES: Resource[] = [
  { uri: 'odoo://{v}/model/{name}',                    desc: 'Snapshot of a model — fields, methods, inheritance, defining module.' },
  { uri: 'odoo://{v}/field/{model}/{field}',           desc: 'Field record — type, defaults, compute, related, overrides.' },
  { uri: 'odoo://{v}/method/{model}/{method}',         desc: 'Method record — signature, decorators, override chain across modules.' },
  { uri: 'odoo://{v}/view/{xmlid}',                    desc: 'View + its XPath inheritance order, by xml_id.' },
  { uri: 'odoo://{v}/module/{name}',                   desc: 'Module manifest, depends, defined+extended models, view counts.' },
  { uri: 'odoo://{v}/pattern/{pattern_id}',            desc: 'Curated pattern snippet + gotchas for common Odoo coding patterns.' },
  { uri: 'odoo://{v}/stylesheet/{module}/{file_path}', desc: 'Raw CSS/SCSS source for stylesheet override analysis and branding.' },
  { uri: 'odoo://{v}/test/{module}/{class_name}',      desc: 'Full test class tree — base chain, setUp, covered models, subclasses.' },
  { uri: 'odoo://{v}/testcoverage/{model}',            desc: 'Static reference coverage for a model: which fields/methods have test hits.' },
];
