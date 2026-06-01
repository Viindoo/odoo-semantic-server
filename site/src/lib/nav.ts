// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT navigation items for SiteHeader (desktop + mobile drawer).
 *  Entries are either full-page routes (/examples, /tools, /pricing) or
 *  absolute hash anchors (e.g. /#benchmark) so links resolve cross-page
 *  (e.g. from /pricing, /tools, /bootstrap). */

export interface NavItem {
  label: string;
  href: string;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Examples',  href: '/examples' },
  { label: 'Tools',     href: '/tools' },
  { label: 'Benchmark', href: '/benchmark' },
  { label: 'Pricing',   href: '/pricing' },
  { label: 'Install',   href: '/install/' },
];

export const NAV_CTA: NavItem = { label: 'Get started', href: '/signup' };
