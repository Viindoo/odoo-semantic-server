# Branding assets

Source-of-truth brand assets for Odoo Semantic MCP. Two identities (per the
combination strategy in PR #215):

- **OSM product mark** (`favicon.svg`, `logo-mark.svg`, `logo-horizontal.svg`) —
  the inheritance-stack glyph. Used for favicon, app-icons, OG image, and
  product chrome (nav, sidebars, auth badge).
- **Viindoo company logo** (`viindoo-logo-inverted.svg` / `.webp`) — the official
  inverted (white) Viindoo wordmark, downloaded from the Brand SSOT
  (`30_Resources/Brand/Viindoo Brand Assets.md` §3, "logo không slogan" v2
  inverted). Used for footers and email headers (company brand). `.webp` is the
  raster the cloudfront CDN serves; `.svg` is the vector equivalent.

## Web assets are generated into `site/public/`

The files under `site/public/` are derived from the sources here. Regenerate
with ImageMagick (`magick`) after changing a source:

```bash
cd <repo>
# favicon (multi-size ICO from the simplified 3-bar mark)
magick -background none branding/favicon.svg -define icon:auto-resize=16,32,48 \
  site/public/favicon.ico
# apple-touch-icon (white bg, padded)
magick -background white branding/logo-mark.svg -resize 150x150 -gravity center \
  -extent 180x180 -depth 8 site/public/apple-touch-icon.png
# PWA icons (transparent)
magick -background none branding/logo-mark.svg -resize 192x192 -depth 8 site/public/icon-192.png
magick -background none branding/logo-mark.svg -resize 512x512 -depth 8 site/public/icon-512.png
# email logo (white Viindoo, for the cyan email header band)
magick branding/viindoo-logo-inverted.webp -resize 360x -depth 8 site/public/logo-email.png
```

`og-image.png` (1200×630) is composed from `logo-mark.svg` + wordmark text
(DejaVu-Sans-Bold) on a white canvas — see PR #215 for the exact `magick`
invocation. `logo-mark-ondark.svg` (white bars + cyan dot, dark chrome) and
`logo-mark-white.svg` (white bars + dark dot, cyan badge) are hand-authored
surface variants kept directly under `site/public/`.

The SVG sources (`favicon.svg`, `logo-mark.svg`, `logo-horizontal.svg`,
`viindoo-logo-inverted.svg`) are also copied verbatim into `site/public/` so
Astro serves them at `/`.
