// SPDX-License-Identifier: AGPL-3.0-or-later
/** SSOT navigation items for SiteHeader (desktop + mobile drawer).
 *  Anchors use absolute paths (/#showcase, /#benchmark) so links work
 *  cross-page (e.g. from /pricing, /tools, /bootstrap). */

export interface NavItem {
  label: string;
  href: string;
}

export const NAV_ITEMS: NavItem[] = [
  { label: 'Live demo', href: '/#showcase' },
  { label: 'Tools',     href: '/tools' },
  { label: 'Benchmark', href: '/#benchmark' },
  { label: 'Pricing',   href: '/pricing' },
  { label: 'Install',   href: '/install/' },
];

export const NAV_CTA: NavItem = { label: 'Get started', href: '/signup' };
