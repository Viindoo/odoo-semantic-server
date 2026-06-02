/** @type {import('tailwindcss').Config} */
export default {
  content: ['./src/**/*.{astro,html,js,jsx,md,mdx,svelte,ts,tsx,vue}'],
  theme: {
    extend: {
      colors: {
        viindoo: {
          primary: '#00BBCE',           // cyan main
          'primary-bright': '#2DD4E8',
          'primary-deep': '#008A99',
          'primary-text': '#00747F',     // cyan-as-text on white — WCAG AA 5.52:1
          secondary: '#7F4282',          // purple
          'secondary-bright': '#A263A5',
          success: '#00B365',
          'success-deep': '#007A47',     // WCAG AA on white (5.5:1)
          warning: '#C99700',
          'warning-deep': '#8B6900',     // WCAG AA on white (5.6:1)
          info: '#0099E6',
          'info-deep': '#006BB3',        // WCAG AA on white (5.5:1)
          danger: '#C0331F',             // red — WCAG AA on white (5.74:1, vs #C0331F on #FFF)
          'danger-deep': '#9A2817',      // deeper red for focus rings on white (8.2:1)
          dark: '#21272B',               // text on light
          body: '#282F33',
          muted: '#6B6D70',
          'bg-0': '#07131A',             // deepest dark BG
          'bg-1': '#0C1E26',             // hero dark BG
          'bg-2': '#112B36',             // card dark BG
          'bg-3': '#173846',             // elevated dark BG
          'on-dark': '#E6F2F4',
          'on-dark-muted': '#95B1B8',
          'on-dark-dim': '#7E9BA6',        // WCAG AA on #07131A (6.38:1)
        },
      },
      fontFamily: {
        display: ['Montserrat', 'system-ui', 'sans-serif'],
        body: ['Roboto', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      // Living Cartography — luxury depth + accent glows.
      boxShadow: {
        glow: '0 0 0 1px rgba(0,187,206,0.18), 0 8px 40px -12px rgba(0,187,206,0.45)',
        'glow-purple': '0 0 0 1px rgba(127,66,130,0.20), 0 8px 40px -12px rgba(127,66,130,0.42)',
        glass: '0 1px 0 0 rgba(255,255,255,0.06) inset, 0 24px 60px -28px rgba(0,0,0,0.7)',
        lift: '0 24px 70px -32px rgba(0,0,0,0.75), 0 0 0 1px rgba(0,187,206,0.22)',
      },
      keyframes: {
        'cv-rise': {
          '0%': { opacity: '0', transform: 'translateY(18px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        'cv-aurora': {
          '0%, 100%': { transform: 'translate3d(0,0,0) scale(1)', opacity: '0.9' },
          '50%': { transform: 'translate3d(2%,-3%,0) scale(1.08)', opacity: '1' },
        },
        'cv-dash': {
          to: { 'stroke-dashoffset': '-16' },
        },
        'cv-pulse': {
          '0%, 100%': { opacity: '1', transform: 'scale(1)' },
          '50%': { opacity: '0.55', transform: 'scale(1.12)' },
        },
        'cv-sheen': {
          '0%': { 'background-position': '-180% 0' },
          '100%': { 'background-position': '180% 0' },
        },
      },
      animation: {
        'cv-rise': 'cv-rise 0.7s cubic-bezier(0.16,1,0.3,1) both',
        'cv-aurora': 'cv-aurora 16s ease-in-out infinite',
        'cv-dash': 'cv-dash 1.4s linear infinite',
        'cv-pulse': 'cv-pulse 2.6s ease-in-out infinite',
        'cv-sheen': 'cv-sheen 6s linear infinite',
      },
    },
  },
  plugins: [],
};
