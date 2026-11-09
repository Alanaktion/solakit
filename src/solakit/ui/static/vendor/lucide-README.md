# Lucide icons

Pinned upstream: https://github.com/lucide-icons/lucide/tree/0.468.0

`lucide.svg` contains only the icons used by Solakit, copied from that release's
`icons/*.svg` into named symbols. Geometry is unchanged; shared stroke attributes
are supplied by `.icon` in app.css. Keep both upstream license notices in
`lucide-LICENSE`. No runtime download, JavaScript renderer, or Node build is needed.

To add or update an icon, use SVG geometry from the pinned release (or explicitly
update this version), retain its 24 × 24 viewBox, and add a named symbol. Use the
`macros/icons.html` macro in templates, including independently rendered fragments.
Only application-owned icon names belong in templates; never render SVG from
conversation content. Check references, wheel assets, and browser rendering.
