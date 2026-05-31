// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * CookieConsentBanner — fixed bottom banner for GA4 Consent Mode v2.
 *
 * Behaviour:
 *  - On mount, reads localStorage.osm_analytics_consent.
 *    If 'granted' or 'denied' → banner already dismissed → render nothing.
 *  - Accept → store 'granted', call window.gtag consent update, hide banner.
 *  - Decline → store 'denied', hide banner (gtag stays at default 'denied').
 *
 * SSR safety: all window/localStorage access is inside useEffect or event
 * handlers — the component renders null on the server so no hydration mismatch.
 */
import { useEffect, useState } from 'react';
import IslandErrorBoundary from './IslandErrorBoundary';

declare global {
  interface Window {
    gtag?: (...args: unknown[]) => void;
    /** Set by GoogleAnalytics.astro once a measurement ID is resolved at runtime. */
    __osmGaId?: string;
  }
}

function CookieConsentBannerInner() {
  // null = unknown (pre-mount); false = show banner; true = already decided → hide.
  const [decided, setDecided] = useState<boolean | null>(null);

  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem('osm_analytics_consent');
    } catch {
      // localStorage blocked (private mode, etc.) — hide banner gracefully.
      setDecided(true);
      return;
    }
    if (stored === 'granted' || stored === 'denied') {
      setDecided(true); // already decided — no banner needed
      return;
    }
    // Undecided: only prompt once GA is actually configured (runtime-resolved by
    // GoogleAnalytics.astro). If analytics is disabled (empty ID), no banner.
    if (window.__osmGaId) {
      setDecided(false);
      return;
    }
    const onReady = () => setDecided(false);
    window.addEventListener('osm-ga-ready', onReady);
    return () => window.removeEventListener('osm-ga-ready', onReady);
  }, []);

  const handleAccept = () => {
    try {
      localStorage.setItem('osm_analytics_consent', 'granted');
    } catch { /* ignore */ }
    if (typeof window !== 'undefined' && typeof window.gtag === 'function') {
      window.gtag('consent', 'update', { analytics_storage: 'granted' });
    }
    setDecided(true);
  };

  const handleDecline = () => {
    try {
      localStorage.setItem('osm_analytics_consent', 'denied');
    } catch { /* ignore */ }
    // No gtag update — analytics_storage stays 'denied' per the Consent Mode default.
    setDecided(true);
  };

  // Render nothing until the effect has run (avoids SSR flash) or if already decided.
  if (decided !== false) return null;

  return (
    <div
      role="dialog"
      aria-label="Cookie consent"
      aria-live="polite"
      className="fixed bottom-0 left-0 right-0 z-50 flex items-center justify-between gap-4 bg-white/95 backdrop-blur-sm border-t border-gray-200 shadow-lg px-4 py-3 sm:px-6 text-sm"
    >
      <p className="text-viindoo-body flex-1 min-w-0">
        We use cookies for anonymous analytics to improve the product.{' '}
        <a
          href="/privacy"
          className="text-viindoo-primary hover:underline focus:outline-none focus:ring-2 focus:ring-viindoo-primary rounded"
          target="_blank"
          rel="noopener noreferrer"
        >
          Privacy policy
        </a>
        .
      </p>
      <div className="flex shrink-0 gap-2">
        <button
          onClick={handleDecline}
          className="rounded-md border border-gray-300 bg-white px-3 py-1.5 text-viindoo-body hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-viindoo-primary transition-colors"
        >
          Decline
        </button>
        <button
          onClick={handleAccept}
          className="rounded-md bg-viindoo-primary px-3 py-1.5 text-white font-medium hover:bg-viindoo-primary-deep focus:outline-none focus:ring-2 focus:ring-viindoo-primary focus:ring-offset-2 transition-colors"
        >
          Accept
        </button>
      </div>
    </div>
  );
}

export default function CookieConsentBanner() {
  return (
    <IslandErrorBoundary name="CookieConsentBanner">
      <CookieConsentBannerInner />
    </IslandErrorBoundary>
  );
}
