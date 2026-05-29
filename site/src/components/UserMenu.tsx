// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * UserMenu — header avatar dropdown island (WS2b)
 *
 * ## What this does
 * Replaces the previously-inert avatar circle in the layout headers with an
 * interactive dropdown trigger. Clicking the avatar opens a light popover menu
 * (white panel on the dark header) with the user's identity header + a list of
 * account / admin / tenant navigation links + Logout.
 *
 * ## Mount location
 * Used by AdminLayout / AccountLayout / TenantLayout in the top-right header:
 *
 *   ```astro
 *   import UserMenu from '../components/UserMenu';
 *   ...
 *   <UserMenu client:idle username={...} email={...} isAdmin={...} isTenantAdmin={...} />
 *   ```
 *
 * ## A11y
 * - Trigger is a <button aria-haspopup="menu" aria-expanded={open}> with an
 *   accessible label "Open user menu".
 * - Panel is role="menu"; each link is role="menuitem".
 * - Opens on click; closes on outside-click, Escape, or after navigation.
 *   Focus returns to the trigger when closed via Escape / outside-click.
 */

import { useState, useEffect, useRef, useCallback } from 'react';

export interface UserMenuProps {
  username: string;
  email: string;
  isAdmin: boolean;
  isTenantAdmin: boolean;
}

interface MenuLink {
  label: string;
  href: string;
}

function roleBadge(isAdmin: boolean, isTenantAdmin: boolean): string {
  if (isAdmin) return 'Admin';
  if (isTenantAdmin) return 'Tenant admin';
  return 'User';
}

export default function UserMenu({
  username,
  email,
  isAdmin,
  isTenantAdmin,
}: UserMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const avatarLetter = (username || '?').charAt(0).toUpperCase();
  const badge = roleBadge(isAdmin, isTenantAdmin);

  const close = useCallback((returnFocus = false) => {
    setOpen(false);
    if (returnFocus) triggerRef.current?.focus();
  }, []);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (
        panelRef.current &&
        !panelRef.current.contains(target) &&
        triggerRef.current &&
        !triggerRef.current.contains(target)
      ) {
        close();
      }
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open, close]);

  // Close on Escape (return focus to trigger)
  useEffect(() => {
    if (!open) return;
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.stopPropagation();
        close(true);
      }
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [open, close]);

  // Common items for ALL users
  const accountItems: MenuLink[] = [
    { label: 'My API Keys', href: '/account/api-keys' },
    { label: 'My Repositories', href: '/account/repos' },
    { label: 'Usage', href: '/account/usage' },
    { label: 'Security / 2FA', href: '/account/security' },
    { label: 'Change password', href: '/account/password' },
  ];

  const adminItems: MenuLink[] = [
    { label: 'Admin Dashboard', href: '/admin/' },
    { label: 'Admin Settings', href: '/admin/settings' },
  ];

  const tenantItems: MenuLink[] = [
    { label: 'Tenant Settings', href: '/tenant/settings' },
  ];

  // Anchor click: let navigation proceed, but close the menu first so a
  // client-side back/forward (bfcache) restore doesn't show a stale-open menu.
  const handleItemClick = useCallback(() => {
    setOpen(false);
  }, []);

  const itemClass =
    'block px-4 py-2 text-sm text-gray-700 hover:bg-gray-100 focus:outline-none focus:bg-gray-100 focus:ring-2 focus:ring-inset focus:ring-viindoo-primary-deep';

  return (
    <div className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Open user menu"
        data-testid="user-menu-trigger"
        className="w-8 h-8 rounded-full bg-viindoo-primary flex items-center justify-center text-viindoo-bg-0 text-sm font-bold focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-viindoo-primary-deep"
      >
        {avatarLetter}
      </button>

      {open && (
        <div
          ref={panelRef}
          role="menu"
          aria-label="User menu"
          data-testid="user-menu-panel"
          className="absolute right-0 mt-2 w-64 origin-top-right rounded-xl bg-white shadow-lg ring-1 ring-black/5 py-1 z-50"
        >
          {/* Identity header */}
          <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100">
            <div className="w-9 h-9 flex-shrink-0 rounded-full bg-viindoo-primary flex items-center justify-center text-viindoo-bg-0 text-sm font-bold">
              {avatarLetter}
            </div>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-gray-900 truncate">{username}</p>
              {email && (
                <p className="text-xs text-gray-500 truncate">{email}</p>
              )}
              <span className="inline-block mt-1 text-[10px] font-semibold px-1.5 py-0.5 rounded-full bg-viindoo-primary text-viindoo-bg-0 uppercase tracking-wide">
                {badge}
              </span>
            </div>
          </div>

          {/* Account items (all users) */}
          <div className="py-1">
            {accountItems.map((item) => (
              <a
                key={item.href}
                href={item.href}
                role="menuitem"
                onClick={handleItemClick}
                className={itemClass}
              >
                {item.label}
              </a>
            ))}
          </div>

          {/* Admin section */}
          {isAdmin && (
            <div className="py-1 border-t border-gray-100">
              {adminItems.map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  role="menuitem"
                  onClick={handleItemClick}
                  className={itemClass}
                >
                  {item.label}
                </a>
              ))}
            </div>
          )}

          {/* Tenant section */}
          {isTenantAdmin && (
            <div className="py-1 border-t border-gray-100">
              {tenantItems.map((item) => (
                <a
                  key={item.href}
                  href={item.href}
                  role="menuitem"
                  onClick={handleItemClick}
                  className={itemClass}
                >
                  {item.label}
                </a>
              ))}
            </div>
          )}

          {/* Logout */}
          <div className="py-1 border-t border-gray-100">
            <a
              href="/admin/logout"
              role="menuitem"
              data-testid="user-menu-logout"
              onClick={handleItemClick}
              className="block px-4 py-2 text-sm font-medium text-viindoo-danger hover:bg-red-50 focus:outline-none focus:bg-red-50 focus:ring-2 focus:ring-inset focus:ring-viindoo-primary-deep"
            >
              Logout
            </a>
          </div>
        </div>
      )}
    </div>
  );
}
