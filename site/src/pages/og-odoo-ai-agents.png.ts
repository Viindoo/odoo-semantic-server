// SPDX-License-Identifier: AGPL-3.0-or-later
// Build-time prerendered OG image for the /odoo-ai-agents landing page.
// 1200 x 630 px - brand faithful: dark bg (#07131A), cyan #00BBCE, purple #7F4282.
// Uses satori (virtual DOM -> SVG) + @resvg/resvg-js (SVG -> PNG).
// No browser needed; runs entirely at Astro build time (prerender = true).
//
// Satori constraints (must be followed or build errors):
//   - Every container with >1 child must have explicit display: 'flex'.
//   - Only div/span/img/svg allowed (no h1/p/br/ul/li etc.).
//   - No 'display: block' on container with children.
//   - Font data must be woff or ttf (NOT woff2).

import type { APIRoute } from 'astro';
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import { readFileSync } from 'node:fs';
import path from 'node:path';

export const prerender = true;

// ---------------------------------------------------------------------------
// Font loading — fonts committed to public/fonts/ so the path survives
// Astro's prerender bundle (process.cwd() == site/ during build).
// Never use import.meta.url __dirname here: bundled output lands in dist/server/
// and the relative path to node_modules breaks. woff1 only (satori limitation).
// ---------------------------------------------------------------------------
const fontsDir = path.join(process.cwd(), 'public', 'fonts');
const fontBold    = readFileSync(path.join(fontsDir, 'montserrat-latin-700-normal.woff'));
const fontSemiBold = readFileSync(path.join(fontsDir, 'montserrat-latin-600-normal.woff'));

// ---------------------------------------------------------------------------
// Constellation SVG (background layer, rendered as data URI img in satori)
// Core knowledge-graph node centred at (760, 315); 8 agent nodes orbit it.
// ---------------------------------------------------------------------------
const CX = 760;
const CY = 315;

function polar(cx: number, cy: number, deg: number, r: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [Math.round(cx + r * Math.cos(rad)), Math.round(cy + r * Math.sin(rad))];
}

// [angle_deg, radius, label, stroke_color]
const AGENTS: [number, number, string, string][] = [
  [ -90, 190, 'Architect',     '#7F4282'],
  [ -45, 210, 'Coder',         '#00BBCE'],
  [  10, 200, 'Frontend',      '#00BBCE'],
  [  65, 195, 'Reviewer',      '#00BBCE'],
  [ 120, 205, 'Backend Debug', '#00BBCE'],
  [ 175, 210, 'UI Debug',      '#00BBCE'],
  [-155, 195, 'UI Review',     '#00BBCE'],
  [-120, 200, 'Intent',        '#00BBCE'],
];

const edges = AGENTS.map(([a, r, , c]) => {
  const [x, y] = polar(CX, CY, a, r);
  return `<line x1="${CX}" y1="${CY}" x2="${x}" y2="${y}" stroke="${c}" stroke-width="1.5" stroke-dasharray="5,4" opacity="0.5"/>`;
}).join('');

const nodes = AGENTS.map(([a, r, label, c]) => {
  const [x, y] = polar(CX, CY, a, r);
  return `<circle cx="${x}" cy="${y}" r="28" fill="#07131A" stroke="${c}" stroke-width="2"/>
          <text x="${x}" y="${y + 5}" text-anchor="middle" font-family="Montserrat" font-size="10" font-weight="600" fill="${c}">${label}</text>`;
}).join('');

// Skills node (green accent)
const [skX, skY] = polar(CX, CY, 200, 230);
const skillsNode = `
  <line x1="${CX}" y1="${CY}" x2="${skX}" y2="${skY}" stroke="#4CAF50" stroke-width="1.2" stroke-dasharray="4,4" opacity="0.45"/>
  <circle cx="${skX}" cy="${skY}" r="32" fill="#07131A" stroke="#4CAF50" stroke-width="1.8"/>
  <text x="${skX}" y="${skY - 5}" text-anchor="middle" font-family="Montserrat" font-size="11" font-weight="700" fill="#4CAF50">+42</text>
  <text x="${skX}" y="${skY + 10}" text-anchor="middle" font-family="Montserrat" font-size="9" font-weight="600" fill="#4CAF50">skills</text>
`;

// Core node
const coreNode = `
  <circle cx="${CX}" cy="${CY}" r="52" fill="#0A1E2B" stroke="#00BBCE" stroke-width="2.5"/>
  <text x="${CX}" y="${CY - 8}" text-anchor="middle" font-family="Montserrat" font-size="11" font-weight="700" fill="#00BBCE">knowledge</text>
  <text x="${CX}" y="${CY + 8}" text-anchor="middle" font-family="Montserrat" font-size="11" font-weight="700" fill="#00BBCE">graph</text>
  <text x="${CX}" y="${CY + 24}" text-anchor="middle" font-family="Montserrat" font-size="9" font-weight="600" fill="#4FC3CB">v8 to latest</text>
`;

// Dot grid
const dots: string[] = [];
for (let gx = 40; gx < 1200; gx += 60) {
  for (let gy = 30; gy < 630; gy += 60) {
    dots.push(`<circle cx="${gx}" cy="${gy}" r="1" fill="#1A3040"/>`);
  }
}

const bgSvg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">
  <rect width="1200" height="630" fill="#07131A"/>
  ${dots.join('')}
  ${edges}
  ${nodes}
  ${skillsNode}
  ${coreNode}
  <defs>
    <radialGradient id="glow" cx="${CX}" cy="${CY}" r="200" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#00BBCE" stop-opacity="0.07"/>
      <stop offset="100%" stop-color="#07131A" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#glow)"/>
</svg>`;

const bgDataUri = `data:image/svg+xml;base64,${Buffer.from(bgSvg).toString('base64')}`;

// ---------------------------------------------------------------------------
// satori layout — all containers must have display:'flex' (satori constraint).
// Use only div/span/img. No h1/p/br.
// ---------------------------------------------------------------------------

// Pill badge helper
function pill(label: string, bg: string, border: string, color: string) {
  return {
    type: 'div',
    props: {
      style: {
        display: 'flex',
        alignItems: 'center',
        padding: '5px 14px',
        background: bg,
        border: `1.5px solid ${border}`,
        borderRadius: '20px',
        fontSize: 13,
        fontWeight: 600,
        color,
        marginRight: 8,
      },
      children: label,
    },
  };
}

const layout = {
  type: 'div',
  props: {
    style: {
      display: 'flex',
      width: 1200,
      height: 630,
      background: '#07131A',
      fontFamily: 'Montserrat, sans-serif',
      position: 'relative',
    },
    children: [
      // Full-canvas background SVG
      {
        type: 'img',
        props: {
          src: bgDataUri,
          width: 1200,
          height: 630,
          style: { position: 'absolute', top: 0, left: 0 },
        },
      },
      // Left-side gradient overlay + text panel
      {
        type: 'div',
        props: {
          style: {
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            position: 'absolute',
            top: 0,
            left: 0,
            width: 580,
            height: 630,
            padding: '52px 52px',
            background: 'linear-gradient(90deg, rgba(7,19,26,0.97) 68%, rgba(7,19,26,0) 100%)',
          },
          children: [
            // Kicker row: accent bar + label
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'row',
                  alignItems: 'center',
                  marginBottom: 18,
                },
                children: [
                  {
                    type: 'div',
                    props: {
                      style: {
                        width: 28,
                        height: 3,
                        background: '#00BBCE',
                        borderRadius: 2,
                        marginRight: 10,
                        flexShrink: 0,
                      },
                    },
                  },
                  {
                    type: 'span',
                    props: {
                      style: {
                        fontSize: 12,
                        fontWeight: 600,
                        color: '#00BBCE',
                        letterSpacing: '0.09em',
                      },
                      children: 'VIINDOO FOR CLAUDE CODE',
                    },
                  },
                ],
              },
            },
            // Main title: "Odoo AI" (white) + "Agent Team" (cyan)
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'column',
                  marginBottom: 16,
                },
                children: [
                  {
                    type: 'span',
                    props: {
                      style: {
                        fontSize: 52,
                        fontWeight: 700,
                        lineHeight: 1.05,
                        color: '#FFFFFF',
                      },
                      children: 'Odoo AI',
                    },
                  },
                  {
                    type: 'span',
                    props: {
                      style: {
                        fontSize: 52,
                        fontWeight: 700,
                        lineHeight: 1.05,
                        color: '#00BBCE',
                      },
                      children: 'Agent Team',
                    },
                  },
                ],
              },
            },
            // Subtitle line 1
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'row',
                  marginBottom: 4,
                },
                children: {
                  type: 'span',
                  props: {
                    style: {
                      fontSize: 17,
                      fontWeight: 600,
                      color: '#89BAC8',
                      lineHeight: 1.4,
                    },
                    children: '42 skills · 8 agents · 10 commands',
                  },
                },
              },
            },
            // Subtitle line 2
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'row',
                  marginBottom: 28,
                },
                children: {
                  type: 'span',
                  props: {
                    style: {
                      fontSize: 17,
                      fontWeight: 600,
                      color: '#89BAC8',
                    },
                    children: 'for Claude Code',
                  },
                },
              },
            },
            // Pill row
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'row',
                  flexWrap: 'wrap',
                  marginBottom: 32,
                },
                children: [
                  pill('42 Skills',   'rgba(0,187,206,0.14)',  '#00BBCE', '#00BBCE'),
                  pill('8 Agents',    'rgba(127,66,130,0.14)', '#7F4282', '#C084C9'),
                  pill('10 Commands', 'rgba(0,187,206,0.10)',  '#00BBCE', '#7EC8D0'),
                ],
              },
            },
            // Brand footer
            {
              type: 'div',
              props: {
                style: {
                  display: 'flex',
                  flexDirection: 'row',
                  alignItems: 'center',
                  marginTop: 'auto',
                },
                children: [
                  {
                    type: 'span',
                    props: {
                      style: {
                        fontSize: 14,
                        fontWeight: 700,
                        color: '#7F4282',
                        letterSpacing: '0.06em',
                      },
                      children: 'VIINDOO',
                    },
                  },
                  {
                    type: 'span',
                    props: {
                      style: {
                        fontSize: 14,
                        fontWeight: 400,
                        color: '#3D6070',
                        marginLeft: 8,
                      },
                      children: '· odoo-semantic.viindoo.com',
                    },
                  },
                ],
              },
            },
          ],
        },
      },
    ],
  },
};

// ---------------------------------------------------------------------------
// Astro API route — generates PNG at build time (prerender = true)
// ---------------------------------------------------------------------------
export const GET: APIRoute = async () => {
  const svg = await satori(layout as Parameters<typeof satori>[0], {
    width: 1200,
    height: 630,
    fonts: [
      { name: 'Montserrat', data: fontBold.buffer,     weight: 700, style: 'normal' },
      { name: 'Montserrat', data: fontSemiBold.buffer, weight: 600, style: 'normal' },
    ],
  });

  const resvg = new Resvg(svg, { fitTo: { mode: 'width', value: 1200 } });
  const png   = resvg.render().asPng();

  return new Response(new Uint8Array(png), {
    status: 200,
    headers: {
      'Content-Type':  'image/png',
      'Cache-Control': 'public, max-age=31536000, immutable',
    },
  });
};
