// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT cho so MCP tool/resource hien thi tren trang marketing.
 *  Drift duoc chan boi tests/test_tool_count_sync.py (so voi MCP surface that). */
export const TOOL_COUNT = 25;
export const RESOURCE_COUNT = 7;

/** Current server version shown in SiteFooter.
 *  Sync manually from [project].version in pyproject.toml (root). */
export const SITE_VERSION = '0.14.2';

/** Brand name SSOT.
 *  BRAND_FULL  — full product name; use in title/meta SEO, H1, legal copy,
 *                first mention, footer copyright.
 *  BRAND_SHORT — shorthand; use after first mention + in narrow UI (sidebar).
 *  BRAND_DEF   — definition string; use once in footer to introduce the shorthand. */
export const BRAND_FULL  = 'Odoo Semantic MCP';
export const BRAND_SHORT = 'OSM';
export const BRAND_DEF   = 'OSM (Odoo Semantic MCP)';
