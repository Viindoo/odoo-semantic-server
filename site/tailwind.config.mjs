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
          secondary: '#7F4282',          // purple
          'secondary-bright': '#A263A5',
          success: '#00B365',
          'success-deep': '#007A47',     // WCAG AA on white (5.5:1)
          warning: '#C99700',
          'warning-deep': '#8B6900',     // WCAG AA on white (5.6:1)
          info: '#0099E6',
          'info-deep': '#006BB3',        // WCAG AA on white (5.5:1)
          dark: '#21272B',               // text on light
          body: '#282F33',
          muted: '#6B6D70',
          'bg-0': '#07131A',             // deepest dark BG
          'bg-1': '#0C1E26',             // hero dark BG
          'bg-2': '#112B36',             // card dark BG
          'bg-3': '#173846',             // elevated dark BG
          'on-dark': '#E6F2F4',
          'on-dark-muted': '#95B1B8',
          'on-dark-dim': '#5A7782',
        },
      },
      fontFamily: {
        display: ['Montserrat', 'system-ui', 'sans-serif'],
        body: ['Roboto', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
};
