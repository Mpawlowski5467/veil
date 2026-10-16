# Desktop logo assets

The original project artwork remains in `veil-logo.png`. The desktop uses two
derived PNG assets, created with the built-in image-generation tool:

- [`veil-mark.png`](../../src/veil/assets/veil-mark.png): transparent charcoal
  symbol for the sidebar and animated loader.
- [`veil-app-icon.png`](../../src/veil/assets/veil-app-icon.png): white symbol on
  a rounded teal tile, with transparent outer corners, for the application icon.

Both masters are 1254 × 1254 pixels. The UI renders the symbol separately from a
native text wordmark so letters cannot be clipped by a raster crop. The native
application icon receives 512, 256, 128, 64, and 32 pixel versions, rather than
reusing the small UI image. Basic installations without Pillow use Tk subsampling.

## Symbol prompt

Use case: background-extraction / precise-object-edit. Edit target: the attached
existing Veil project logo. Produce a clean production master of ONLY the ribbon
symbol at the LEFT of the reference; omit the word 'veil' entirely. Preserve the
exact identity, geometry, silhouette, ribbon folds, detached horizontal bars,
spacing, and relative proportions of that existing mark. This is a faithful
cleanup and extraction, not a redesign. Solid flat charcoal #292c2f with smooth
crisp vector-like edges, no texture, no gradient, no blur, no shadow, no border,
no circle, no tile, no text. Actual transparent background including all negative
spaces. Square 1024x1024 canvas with the complete symbol centered, occupying
about 82% width and 70% height, evenly padded so no element is cut off. High
resolution and sharp enough to derive native desktop app icons and loaders.

## Application icon prompt

Use case: precise-object-edit. Asset type: polished macOS desktop application icon
for Veil. Reference/edit target is the exact attached Veil ribbon symbol. Preserve
the shape and all interior gaps of the symbol exactly. Recolor the ribbon to
clean solid warm white #f9fcfa. Center it inside a simple flat deep teal #18745c
rounded-square tile, with the ribbon occupying 65% of tile width, with comfortable
balanced padding. Tile has macOS-style smoothly rounded squircle corners,
occupies 88% of a square 1024x1024 canvas, centered; all space OUTSIDE the
rounded-square tile is actual alpha transparency. Design must be legible at 32
pixels in a Dock. Flat, crisp, high resolution, no text, no border, no bevel,
no 3D, no gloss, no shadows, no gradient, no added symbols. Do not include any
white rectangular background. This is the same existing brand mark, not a new
logo.
