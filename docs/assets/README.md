# Veil assets

## Trailer

- [veil-trailer.gif](veil-trailer.gif): the looping 54-second trailer at the top of the README (960 × 540, 15 fps).
- [veil-trailer.mp4](veil-trailer.mp4): the same trailer as 1080p H.264 video for sharing.
- [Renderer](../demo/trailer.py), which draws every frame from code.

The trailer covers the round trip, coding secrets, the Claude Code and Codex
launchers, verification, the 1.0.0rc1 candidate features, test evidence, and
limits. Its masked and restored text is produced by this checkout's Veil when
the trailer renders, using fictional data; rendering stops if Veil's output no
longer matches what a scene shows. Model replies are simulated, nothing is sent
over the network, and a caption on every frame says so. The desktop window is a
drawing of the masking preview page, not a screenshot. Icons are drawn shapes,
and the ribbon comes from [`veil-mark.png`](../../src/veil/assets/veil-mark.png).

From a source checkout with **ffmpeg** on PATH, run:

```sh
uv run --with "pillow>=10,<13" python docs/demo/trailer.py
```

Add `--still 7.5 30 --out <folder>` to write single frames for review instead.
The renderer uses Avenir Next and Menlo on macOS, or DejaVu fonts elsewhere; it
does not bundle fonts or add dependencies to Veil. Update the 1.0 scene and the
test count when the release changes.

## Demo

- [veil-demo.gif](veil-demo.gif): a captioned, looping 36-second README demo.
- [veil-demo.mp4](veil-demo.mp4): the same demo as H.264 video for sharing.
- [Runnable source](../../examples/first_round_trip.py) and [captured output](../demo/transcript.json).

The masking and restoration output was captured using the released 0.5.0 wheel.
The reply is simulated, with fictional contact data, no network calls, and an
in-memory vault. Every scene identifies this boundary. This is a rendered
walkthrough, not a screen recording of an AI client.

To rebuild, install Veil 0.5.0 in a separate environment, then run from the repo
root (replace `python` with that environment's interpreter):

```sh
python examples/first_round_trip.py --json > docs/demo/transcript.json
```

Run `python docs/demo/render.py` in an environment with **Pillow** installed and
**ffmpeg** on PATH. The renderer uses available system Arial/Menlo or DejaVu
fonts; it does not bundle them or add dependencies to Veil. These commands replace
the generated assets. The demo contains all captions on screen and has no audio.

## Logo

[veil-logo.png](veil-logo.png) is the selected privacy ribbon logo (concept D), prepared for the main README. Its opaque white background keeps the charcoal mark readable in light and dark interfaces.

Created with the built-in imagegen tool from the approved concept. The presentation heading and duplicate icon were removed for the final banner.

## Final editing prompt

```text
Use case: precise-object-edit
Asset type: final README logo banner derived from the user's selected concept D.
Input image: the supplied "D — Privacy ribbon" presentation is the edit target. Preserve the central horizontal logo lockup exactly: the dark ribbon covering three text lines, and the lowercase "veil" wordmark.
Primary request: Extract the main central lockup into a clean publication-ready horizontal logo banner. Remove the "D — Privacy ribbon" heading and the smaller duplicate icon below. Do not redesign the symbol, change the wordmark lettering, or add any objects or text. Keep the exact shape, proportions, spacing and charcoal color of the large lockup.
Composition: wide banner approximately 3:1 aspect ratio, centered logo occupying about 80 percent of the width, even modest padding on all sides. Only one logo, no extra duplicates, no label, no frame.
Background: completely opaque solid pure white including every corner and all negative space, so the black logo remains readable in both light and dark README interfaces. Do not create transparency. Flat vector-like edges, no gradients, no texture, no shadows.
Text verbatim: "veil".
```
