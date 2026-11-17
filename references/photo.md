# Casual photos of a painting: `picture.py rectify`

People send what they have: a phone photo of the painting on the wall, on a table or in their hands, in its frame or mat, taken at an angle under a warm lamp. Before anything is cut or animated, the working picture should be the painting as a flat scan under neutral, even light would show it. `rectify` finds the artwork in the photo and undoes only what the photo did to it: the crop, the perspective, residual lens bending, uneven light and a colour cast. It restores and never restyles: no sharpening, no saturation or contrast boost, no "enhancement". Everything runs on the machine (numpy, scipy, Pillow; Apple Vision when it is there).

Every automatic result is a starting point. Look at the review sheet before you prepare from the result.

## Running it

```
python3 scripts/picture.py rectify <photo> --out <job>/rectified.png
python3 scripts/picture.py prepare <job>/rectified.png --out <job>/work      # on exit 0; on exit 4 prepare the photo itself
```

| Option | Use |
|---|---|
| `--review SHEET.jpg` | the review sheet (default: next to `--out`, `*-review.jpg`) |
| `--corners "x,y;x,y;x,y;x,y"` | the artwork's four corners in photo pixels (after EXIF orientation), any order; skips detection. Read them off `picture.py look <photo>`, or take them from the report's `alternatives` |
| `--frame auto` | (default) remove a plain frame and the mat inside it and keep the painted surface; a sheet's own paper margin stays (the `--help` text words this as if the mat were kept: it is removed) |
| `--frame keep` | keep the frame and mat in the picture |
| `--frame remove` | also cut away a carved or uneven frame (the largest credible boundary well inside the outer one) |
| `--lens off`, `--light off`, `--color off` | skip that correction |
| `--max N` | cap the output's longer side (default 4096; 0 = no cap). The output is the work at its own detail in the photo, never enlarged beyond that; `prepare` caps it again later (1600 px) |

Exit codes: **0** the corrected picture was written (lossless PNG, sRGB). **4** nothing to correct, or no boundary found with confidence: nothing is written and the caller keeps the photo as it is. **2** an error, printed as `{"error": ...}`. Parts of the result that fall outside the photo (a corner of the work cut off by the photo, or `--corners` outside it) stay transparent rather than smeared, and a warning says so.

A flat scan or screenshot of the artwork alone passes through untouched (exit 4, nothing written): the seven real flat pictures used in testing (a scanned watercolour, five watermarked screenshots of paintings, the skill's example) and the benchmark's six flat cases all gave exit 4.

## What it does, in order

1. **Reads the photo** as `prepare` does (EXIF orientation, ICC profile to sRGB, HEIC and camera RAW through ImageIO on macOS) and the camera's 35 mm-equivalent focal length from EXIF (also inside HEIC, or from Spotlight). The focal length is used only when the picture still has the shape of the camera frame; a cropped photo says so in `camera.focal_note`. The work is done on the photo's 8-bit levels; only the part under the work is ever turned into linear light.
2. **Finds the boundary.** Two sources: Apple Vision's document and rectangle detectors (`lp.apple.quads`, macOS) and an edge finder of its own that works anywhere (colour edges on a 1000 px copy, Hough lines, every convex quadrilateral supported by edges on all four sides; one corner may lie outside the photo). Every candidate is refined on the full photo to sub-pixel edge points along each side; the refinement follows a gentle bow (lens distortion bends straight sides by up to about 1% of their length) and looks wider once when the edge leaves its window. A candidate is scored by how much of each side holds, how straight it is, and whether an edge reaches each corner: where the lines of two objects cross (a painting's top edge running on to its neighbour's side) no edge reaches the corner, and such a quad gives way to one whose edges do. Nested rectangles (frame, mouldings, mat, painting) give many good quads; the outermost credible one is the object and what lies inside it is read next. A candidate must be plausible as a phone's view of a rectangle: at least 5% of the photo (10% without camera data), corners between 35 and 145 degrees, opposite sides within 3x. Without camera data, a rectangle that is exactly square-on is taken as part of a flat picture (a shape in the painting, a scan's margin), never cropped.
3. **Frame, mat, paper margin.** Looking inward from each side, the strip is cut at straight edges parallel to that side; the pieces' colours are grouped across the four sides (by chromaticity, since a lamp lights one side more than another). Material that goes **all round** and ends at a straight edge is a band; a flat sky along one edge of the painting does not go all round, so it stays. Darker or coloured material at the outer edge is a frame, and a light band just inside it a mat: with `--frame auto` both are removed. A light, near-neutral band at the outer edge with nothing round it is the sheet's own paper margin: it is part of the work and stays (a print's border, a watercolour's margin).
4. **Lens.** One radial coefficient (`k1`, about the image centre, radius normalised to half the diagonal; negative is barrel) is fitted so that the edge points of all sides lie straightest. It is applied only when it clearly straightens them (`lens.straightness_px_before/after`). Phones correct most distortion themselves, so `k1` near 0 is the common answer.
5. **Perspective.** The real width-to-height ratio comes from the rectangle-from-homography method (Zhang & He) with a focal length: from EXIF (`perspective.focal: "exif"`); without one, from the quad's own two vanishing points when they are stable (`"vanishing points"`); else a phone's main camera is assumed (`"assumed 26 mm"`, and at oblique views a warning says the proportions may be a few percent off). `aspect_method` is `"focal"` for all three, `"sides"` only if that fails (the mean of opposite sides, which leans towards the near side). The output is sized so that nowhere is the photo shrunk (the nearest part of the work keeps its pixels), capped by `--max` (the photo is then first reduced in linear light, so nothing aliases), and resampled once in linear light with an interpolating cubic spline clamped to the four nearest pixels: no aliasing and no ringing halo. The blurred rim of the boundary is trimmed (about half the edge's measured width).
6. **Light.** A smooth illumination field is fitted only to **neutral, even evidence**: light, near-neutral paper margin or mat (the lightest material that goes all round, never the wood or gilt of a frame), sampled in blocks along each side and across its depth (a sheet's plain paper to a fifth of the way in, no deeper, since a pale wash further in would pass for shade); each block must be smooth and of the material's own colour, so a thumb, a pencil mark or a fading wash is left out. Without a margin or mat, plain wall or table round the work (low texture, one dominant material) is used. The field is modelled on the work's own plane (output coordinates), where a lamp's falloff, a broad hotspot or a soft shadow is a smooth function: a robust cubic where the evidence surrounds the work, a quadratic with one side missing (checked not to invent a dome or a pit there), a plane with two, and a smoothing spline on what the cubic leaves when that is clearly more than the noise between neighbouring cells (the soft edge of a shadow). Clipped white paper is kept as a lower bound (the light there is at least that bright). Evidence that scatters more than 5% around the field is not one even surface and is not used at all. The field is divided out only when its range over the work clearly exceeds the noise (at least 6% and five times the scatter), when it explains the evidence to within 15%, and when it stays under 4x. The painting itself is never used, so its own gradients (a dark foreground, a light sky) survive.
7. **Colour.** The evidence's colour, with its light divided out, gives von Kries gains in linear light, but only along the warm-cool (Planckian) axis: a lamp or shade moves a neutral surface along it, while a green, magenta or blue cast off that axis is more likely the material's own tint and is left. Strong warm casts (a 2700 K lamp under a daylight balance) may be corrected, and so are cool ones in full down to a warm-cool offset of -0.5 (only beyond that is a cool reference taken as blue paint rather than blue light, although the code comment says cool casts are corrected only mildly); a reference too colourful to be a neutral surface (C* above about 45-50) is refused. This means a cream, beige, peach or pale blue mat or wall (C* about 10-20) is accepted as neutral and its own tint is "corrected" into the painting: see the failure modes below. Gains are capped (0.5 to 2.5). Grey-world on the painting is never used.
8. **Exposure**, with a paper or mat reference: the reference is mapped to paper white (linear 0.80, sRGB 231), between x0.8 and x1.6, and never so far that the painting's own lightest paint clips. This applies to any light mat, not only white paper (a mid-grey mat brightens the painting up to 1.6x), and `--light off --color off` do not switch it off: there is no flag that keeps the photo's own tone.

Without evidence, light and colour stay as photographed and the report says so (in `light.why` and `color.why`, and in the note). A picture whose edges are the work's own (no boundary found, or the boundary is the picture's border) is meant to be changed only when a paper margin shows on all four sides with clearly uneven light. In practice a photo where no boundary was found can still exit 0 with `quad.corners` null and the whole photo colour-balanced against the wall at its border: treat that result as wrong.

## What it refuses to touch, and why

- **A flat picture** (scan, screenshot, an exactly square-on rectangle without camera data): exit 4. Cropping a painted rectangle out of a painting is worse than doing nothing. This protection holds only for files without camera data and only for axis-aligned shapes: see failure mode 1.
- **Colour without neutral evidence** (an oil on a teal or navy wall, a work filling the photo): left as shot. Guessing from the painting would neutralise a red painting or flatten a dark one. Light is not held to the same standard: a coloured wall (navy, cream damask, a green cutting mat) is still used to even out the light.
- **Light it cannot follow smoothly**: a hard shadow edge, a narrow hot spot, light changing more than fourfold, mixed warm and cool light. Half-correcting it would paint gradients the work never had; a warning asks for a reshoot.
- **Glare**: clipped, colourless spots brighter than the paint round them are boxed on the sheet and reported; the paint under them is not invented.
- **Tinted references off the warm-cool axis**: a greenish wall or a magenta mat is taken as that colour, not as light.

## Known failure modes: check these on every result

Found by independent reviewers on their own scenes and on fresh benchmark seeds; none is fixed in the code yet. Each comes with what gives it away and what to do. When `photo_corrected` is true, rule each one out on `rectified-review.jpg` before you prepare from the result.

1. **A shape inside a flat picture taken for the boundary.** A window, a door, a painted frame or any rectangle inside a picture that was already just the artwork, when the file carries camera data (a phone photo cropped to the artwork in Photos keeps its EXIF) or the shape is slightly tilted. The output is a fragment of the picture, often with only a "too few pixels" warning. Gives it away: the input had no wall, table or frame round the work. Do: `living-picture new … --reprepare --as-is`, or prepare the original directly.
2. **Two works side by side become one.** Two paintings hung level come out as one picture with the wall gap between them, at full confidence and with no warning; `alternatives` may not offer either work alone. Do: read the wanted work's corners off a grid view and pass `--corners`.
3. **A frame is kept, or used as white.** A frame without a mat that casts a crisp shadow is kept; a white or light frame (with or without a white mat) is taken for the sheet's paper margin and kept; a light wood frame is taken for paper and used as the white reference, turning the painting blue (gains like R 0.74, B 1.75). Gives it away: a band of frame along the output's edges, `frame.found` false on a framed work, `color.reference` "margin" on a framed work with gains far from 1. Do: `--corners` on the painted surface, plus `--color off` when the colour went wrong.
4. **A tinted mat or wall trusted as neutral.** Cream, beige, peach or pale blue mats and walls (C* about 10-20) are corrected to grey, which shifts the painting the other way: cooler under a cream mat, warmer and less saturated under a blue one, with only the generic "colour balanced on the wall or table" warning, or none for a mat. Do: compare the corrected colours with the photo where the painting is not near the lamp; if the mat or wall is visibly tinted in the photo, rerun with `--color off`.
5. **Exposure on a grey or tinted mat.** Any light mat is mapped to paper white: a mid-grey mat brightens the whole painting by up to 1.6x, and `--light off --color off` do not stop it. Gives it away: `exposure` well above 1.1 while `light` and `color` say unchanged, or a reference that is a mat rather than the sheet's own white paper. Do: if keeping the painting's tone matters more than straightening, prepare from the photo with `--as-is` and say why in the notes.
6. **A pale painting's own gradient flattened.** A near-white painting with no paper margin: its light edge zone is taken for a margin and its painted top-to-bottom gradient is divided out (`light.reference` "margin", a huge drop such as `variation_before` 0.32 to `variation_after` 0.015, no warning). Do: if the work has no white border of its own, `--light off`.
7. **A hard shadow half-corrected.** A crisp shadow band across a sheet with a margin is not removed; instead a smooth bright dome is invented in the middle, with no warning. Gives it away: the light panel shows a dome or bowl that does not follow any lamp, while the shadow band is still visible in the result. Do: `--light off` and ask for a reshoot without the shadow.
8. **No boundary, whole photo recoloured.** Exit 0 with `quad.corners` null and `size_out` equal to `size_in`: nothing was cropped but the whole photo, wall included, was colour-balanced against the wall at its border. Do: treat it as a failure: `--as-is`, or `--corners`.
9. **Busy backgrounds.** A brick or tile wall, a gridded cutting mat or a patterned wallpaper: a mortar or grid line becomes a side, and the output keeps a band of background or cuts into the work. Check every side against the work's real edge; pass `--corners`.
10. **Without Apple Vision** (not on a Mac, or Vision failed): the edge finder alone picks mixed quads on steep views (40 degrees and more), strong barrel distortion and wide mats, still at high confidence. On those systems check the corners of every result.
11. **Faded paint at a corner.** When the work's own paint fades into the wall at one corner, the right quad can be dropped for a wrong one that cuts the corner off. Check that corner.
12. **Cut-off works.** When an inner line is taken for the missing side of a work cut off by the photo, there is no cut-off warning. A work touching the photo's edge always deserves a look at that side.
13. **Capped output.** With the default cap (24-48 MP photos) strong edges get a faint dark halo and the far side of a steep view is a little softer than at `--max 0`; at `--max 0` a 48 MP photo with a large work can take about 19 s and 4 GB.
14. **An interrupted `new`.** `new` has no timeout on its child tools, and killing it (not Ctrl-C) leaves a half-made job: a placeholder notes.md and a hidden `.living-picture-new-*` folder, and the next `new` is refused as "another `living-picture new` is preparing". Remove those two after checking that nothing else is running, then rerun.

## The review sheet

Three panels: the photo with the chosen boundary (magenta, corners labelled), the removed frame or mat (cyan), a sample of the evidence cells used for light and colour (green dots) and glare spots (red boxes) | the corrected picture | where and how much light changed (blue darker, red brighter, about -1 to +1 stop, over a greyed copy of the result), with the light, colour and exposure decisions written underneath. The third panel folds exposure into "light" (a uniform tint across it is the exposure, even when the text says light unchanged) and never shows a colour change: read the gains in the text, and compare the colours of the first two panels yourself. Check at display size and zoomed: the boundary sits on the work's real edge all round, nothing of the frame or wall is left and nothing of the painting is cut, straight lines are straight, the light map follows the lamp rather than the painting, and the colours look like the painting, not like a white-balanced photo of a room.

## The report

JSON on stdout. The keys you will use most:

- `changed`, `written`, `review`, `size_in`, `size_out`, `seconds`
- `quad`: `corners` (TL, TR, BR, BL in photo pixels after EXIF orientation), `source` (`given`, `vision`, `edges`, or null), `confidence`; `alternatives` holds the next candidates with their corners, ready for `--corners`
- `frame`: `found`, `removed`, `widths` (band depth per side, fraction of the work)
- `lens`: `applied`, `k1`, `straightness_px_before`, `straightness_px_after`
- `perspective`: `applied`, `aspect`, `aspect_method`, `aspect_sides`, `focal`, `focal_35mm`, `tilt_deg`, `capped`
- `light`: `applied`, `reference` (`margin`, `surround`, `none`), `variation_before`, `variation_after` (95th minus 5th percentile over the median of the evidence's luminance, before and after), `model`, `noise`, `why` when not applied
- `color`: `applied`, `reference`, `gains` [r, g, b], `cast_before`, `cast_after` (CIELAB chroma of the reference), `warm_cool` and `tint` (its offset along and across the Planckian axis), `why` when not applied
- `exposure`, `camera`, `sharpness` (the boundary's edge width per side and the crispest edges per direction, 10-90% rise in px at the 1600 px working size), `warnings`, `note`

## Warnings and what to do

Warnings are in plain words and say when a reshoot would beat any correction.

| Warning says | Do |
|---|---|
| glare: part of the painting washed out | Paint under glare cannot be recovered. Reshoot with the light from one side at an angle, no flash, the phone turned a little so the reflection falls off the work (glass: turn off lights behind you) |
| camera shake / out of focus (blur) | Tap the painting to focus, hold still or lean on something, more light |
| too few pixels | Move closer or fill more of the frame with the work |
| steep angle, or one side much shorter | Reshoot square-on, the phone parallel to the painting |
| no focal length, proportions assume a phone's main camera | At oblique views without EXIF the proportions may be a few percent off; check them against the painting's real size, or reshoot more square-on |
| touches the edge of the photo / lies outside the photo (cut off) / picture seems to go on beyond an edge | Part of the work may be cut off, or the boundary may be a line inside the painting: check the sheet; reshoot with some wall showing all round, or pass `--corners` |
| an edge is not visible, Apple Vision placed it | The paper and the background were alike, or glare hid it: check that side; reshoot on a darker background |
| light has a hard edge or a hot spot / wall not evenly lit / changes more than fourfold | A shadow across the work or a lamp close by cannot be evened out smoothly, so the light was left. Reshoot in even, indirect daylight with nothing casting a shadow |
| darkest part lifted N stops / one colour channel boosted | Corrected, but noise is amplified there; daylight gives a cleaner photo |
| colour balanced on the wall or table | Right only if that surface is really grey or white; for a cream wall or a wooden table rerun with `--color off` |
| light and colour left as photographed | Nothing neutral was visible; include some plain white wall or paper next time |

On exit 4 the `note` says why; for camera photos warnings may add that a small rectangle, or three edges of one, looked like the work (pass its corners from `alternatives`, or reshoot), or that the photo itself is blurred.

## Limits

- **Mat or paper margin.** A light band at the work's outer edge is taken as the sheet's own margin and kept; inside a frame, the same band is taken as a mat and removed. A mat without a frame (a matted print on a table) is therefore kept, and a watercolour framed without a mat loses its margin. Check the sheet; `--frame keep` or `--corners` decide it.
- **Tinted paper or mat versus warm light.** A cream mat and a white mat under a warm lamp look the same; the correction will make a cream mat white and the painting a little cooler. Walls are worse (beige, wood). Look at the sheet; `--color off` when in doubt.
- **Light inside the work** is only interpolated from its margins or surround. A soft shadow across a sheet is followed from the margins (residual about 5-9% where the benchmark asks for 4%); a narrow hotspot in the middle of the work cannot be seen at all and stays.
- **Glare and sheen** are detected only where clipped; a soft veil of reflection on varnish or glass is not seen.
- **Cut-off works.** When the painting runs past the photo there is no true boundary: usually nothing is found (exit 4); with one corner outside the visible part is used and the rest left transparent; sometimes an inner line is taken for the missing side without a warning. Pass `--corners` or reshoot.
- **Ornate frames** are not uniform bands: `--frame auto` keeps them, `--frame remove` cuts at the strongest boundary inside.
- **Square-on photos without camera data** (EXIF stripped by a messenger app, taken perfectly level) are taken as flat pictures (exit 4): pass `--corners` to crop them.
- **Lens**: one coefficient about the image centre. Cropped photos (centre moved), strongly curled paper or a sagging canvas are not modelled; on a noisy, textured wall the bow of a small, distant work may not be measured (the benchmark's strong-barrel plaster case keeps 0.6% of bow where 0.35% is allowed).
- **Phone tone mapping** (HDR, local contrast) breaks the "light multiplies paint" model a little; corrections are measured on the evidence and refused when they do not fit it.
- Vision's helper is compiled once on first use (see `references/macos-vision.md`); without it the edge finder alone runs, on any system. Vision decodes the photo a second time, a large share of the run time on a 48 MP photo.

## How well it works (measured)

The independent benchmark (`tests/photo/bench.py`, simulated casual photos with exact ground truth, built without seeing this code):

- seed 1, 30 gating cases: 28 pass. The six flat no-op cases all exit 4. Failing: a sketch held in hands with a phone shadow (light residual 8.9%, limit 4%) and a small watercolour on textured plaster with strong barrel distortion (straightness 0.58%, limit 0.35%).
- fresh seeds, 30 gating cases each: 25 pass on seed 7, 24 on seeds 23 and 31, the 48 MP case among them (8-11 s). Recurring failures: the hands case, a soft shadow on a print (4.1-4.2% against 4.0%), the plaster case (wrong boundary), a framed painting next to a neighbour (the quad spans both), a watercolour on a wood table (wrong boundary), a cut-off sheet with no cut-off warning, and once a false "steep" alarm.
- the benchmark itself is weaker than these numbers suggest: its gates let a slightly cropped flat picture, a visible 10 px band of frame or wall, and a stress case with no boundary pass, and it has no busy backgrounds, light frames, tinted mats or flat pictures with EXIF (see `tests/photo/README.md`). On the reviewers' own scenes with those features the boundary or the colour went wrong (failure modes above).
- where the boundary is right, corner errors are hundredths of a percent of the diagonal; lens `k1` from the sides' bow matched the truth on the barrel and pincushion cases (-0.0505 for -0.0505, 0.0443 for 0.0442; -0.12 recovered for -0.12 on the reviewers' cases).

The skill's own synthetic tests (90 photos: frames, mats, sheets, lamp falloff, hotspots, soft shadows, vignetting, warm and cool casts, 13-77 mm lenses, EXIF on or off). Light error is the remaining variation of the true light over the work after correction (2nd-98th percentile), against 38-61% before:

- lamp falloff and casts (24 photos): median 2.3% with a margin or mat as reference, 2.8% with the wall; 80th percentile about 4%.
- with soft shadows and hotspots as well (60 photos): median 4.7-6.6% (margin) and 3.4-3.5% (wall), 80th percentile 7-13%; light varying more than fourfold is refused with a warning.
- boundary: median corner error 0.011-0.013% of the diagonal; misses are the mat/margin ambiguity and cut-off works.
- proportions without EXIF (vanishing points, else 26 mm): median error 0.2%, against 1.5% for the mean of opposite sides.

Timing on an M2 Max (Vision included, other jobs running): a 2-3 MP photo 2-4 s; 12 MP 4-5 s, 1.5-1.8 GB peak memory; 24 MP 6-7 s, 2.4-2.6 GB; 48 MP 8-9 s, 2.4-3.1 GB with the default cap of 4096 px (13-14 s and 2.5-3.0 GB with `--max 0`, a 6943x5015 output; up to 19 s and 4.1 GB when the work fills most of a 48 MP frame at `--max 0`; a 48 MP HEIC 15-17 s). A HEIC saved sideways with EXIF orientation 6 came out upright with its focal length read from the file.
