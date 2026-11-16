# Cutting layers: the plan, the selectors, the fill, the review

`plan.json` says which things can be lifted off the picture and how to find their edges. Before selecting, read the medium: deliberately lost, washed or ground-blended edges must stay that way. Do not manufacture a hard outline for a layer. Keep inseparable paint together and choose supported flow or light changes for the whole; this does not excuse incomplete extraction of a clearly outlined object. `picture.py cut` lifts the separable selections and repaints what they were hiding.

## Contents

1. How peeling works
2. Shape of the plan
3. Ways of selecting
4. The fill
5. Reading the review sheets
6. Defects and what fixes them
7. Reading coordinates

## 1. How peeling works

The peeling procedure below applies after deciding that a depicted region has a separable boundary. For a truly blended region, keep its painted continuity; test any whole-region flow or light change for unintended edges, duplicated marks and altered meaning.

Layers are listed **front to back**. For each one in turn the tool:

1. estimates coverage from the observed picture within the authored object region,
2. applies explicit background protection after selection growth and softening,
3. estimates the hidden background from owned donors or consumes a supplied plate,
4. solves foreground colour while keeping the accepted alpha and plate fixed; unexplained error remains a reported residual,
5. hands the plate, now without that thing, to the next layer.

What is left at the end is the base layer. The observed picture remains unchanged wherever this layer has zero alpha. `cut` reports floating-point and encoded compositing error; even a small error proves neither correct separation nor valid newly exposed background. Inspect the matte, plate and displaced edge independently at native and target display sizes. The lossless original fallback is a separate path.

Order matters where things overlap: whatever is in front must be peeled first, or it will be cut through by the thing behind it.

## 2. Shape of the plan

```json
{
  "image": "work/picture.png",
  "out": "work",
  "base": "ground",
  "layers": [
    { "id": "petals", "parts": [ ... ], "min_area": 6 },
    { "id": "tree",   "parts": [ ... ], "minus": [ ... ], "fill": { "quality": 2 } },
    { "id": "house",  "parts": [ ... ] }
  ]
}
```

Per layer:

- `id`: the name the scene will use.
- `parts`: one or more selections, united.
- `minus`: selections to subtract (a window in a wall, a face excluded from a colour key).
- `protect`: geometric regions owned by another surface or occluder rather than this layer. They force alpha to zero after growth and softening and are excluded from the fill hole. This is an extraction constraint, not a runtime attachment or motion lock.
- `feather` (default 1.0): softness of the edge in px. Raise to 2 for soft washes, 0 for hard-edged or already-soft keyed selections.
- `grow`: px to expand (positive) or shrink the selection.
- `min_area` (default 24): specks smaller than this are dropped.
- `fill`: how to repaint behind it (section 4).
- `halo`: `{"width": 2.5, "level": 0.5}` adds a thin soft rim round the solid paint, so a watercolour wet edge travels with the mark instead of staying behind as a ghost.
- `contour`: a width in px; the dark line hugging the outline joins the object (kept inside the parts' own regions, so it never reaches a neighbour of like tone).
- `pigment`: for marks that will leave their ground (section 4, "Pigment").

Plan level: `"surfaces": [{"id": "wall", "poly": [...], "texture": "smooth"}, ...]` declares the background surfaces once (later entries win; unclaimed pixels are `rest`). Layers then fill with `"method": "surfaces"` (section 4). `cut` writes `review/surfaces.jpg`; check the map before trusting a fill.

Output: `work/layers/<id>.png`, `work/layers.json`, and `work/review/`. Results are cached according to the current reveal/cache contract. Reuse is conservative; asset identity and subsequent motion acceptance are separate.

## 3. Ways of selecting

Each part gives a **rough region** and a **mode** that estimates a boundary. You must inspect it; the selector does not know semantic object identity.

Region, one of:

- `"poly": [[x,y], ...]` a rough outline (six to twenty points is usually enough)
- `"rect": [x0, y0, x1, y1]`
- `"ellipse": {"c": [x,y], "r": [rx, ry]}`
- `"stroke": [[x,y], ...], "width": w` a thick line, for long thin things

Mode:

**`snap`** (default): for a solid thing with a real outline, not an invented boundary across a blended wash. Draw the polygon roughly along the edge; the tool finds the true edge within `tol` px of it by colour and contrast. Help it where colours are similar: `"keep": [[x,y], ...]` marks points that are certainly the thing, `"drop"` points that are certainly not (a point, or a list of points forming a stroke).

`tol` defaults to a few percent of the thing's size. Smaller keeps closer to your outline; larger lets it search farther. Use snap for one object at a time. A polygon around a whole group of different things takes the background between them too.

**`key` by colour**: for many scattered things that share a palette against a different ground: foliage, petals, a flock, stars.

```json
{ "poly": [...], "mode": "key",
  "like":   [[60,90,40], [130,150,60], [170,200,90]],
  "unlike": [[235,225,225], [150,215,225], [40,35,45]], "soft": 0.05 }
```

Samples are colours `[r,g,b]` (0 to 255) or picture points `[x,y]` to sample from. A pixel belongs if it is nearer to a `like` than to any `unlike`. List every shade of the thing in `like` and every neighbouring ground in `unlike`, including dark lines that should stay behind. `"cap": 0.16` additionally rejects anything too far from every `like` colour (use it when the region is the whole picture).

**`key` by tone**: for thin structures darker or lighter than what is behind them: bare twigs, wires, masts, lettering, white ripple lines.

```json
{ "poly": [...], "mode": "key", "tone": "dark", "radius": 10 }
```

`radius` must exceed the half-width of the thickest stroke you want. `gain` strengthens a faint result; `"solid": 2` closes pinholes when the thing should be opaque.

**`matte`**: refine a colour or tone key into soft coverage using local foreground/background colour pairs from the observed picture. Use the same `like`/`unlike` or `tone` options as `key`; the geometric region is the maximum allowed support and must include the intended soft edge. `keep` and `drop` mark definite opaque foreground and background, respectively; `seed_width` defaults to 2 source px. `edge` (default 4 source px) bounds the local edge search; `residual` (default 0.035 normalized RGB RMS) limits colour-model mismatch. An unreliable fit retains its initial estimate: this is uncertainty, not a successful opacity measurement. The mode does not inspect the repainted plate, discover missing object parts, or resolve a fully translucent object without usable foreground samples. A colour-key score is not itself physical opacity.

For `matte` only, optional `background` samples declare the complete observed background palette within the region, using picture points or RGB colours as above. `background_tol` (default 0.07) and `background_soft` (default 0.05) set the absolute Lab-colour match range and transition. Pixels far from every declared background colour can strengthen foreground seeds even when the relative key is weak. Missing background shades can therefore select unrelated structures: inspect the region and retain `protect`/`drop` ownership constraints.

**`wand`**: for flat-colour art. Seeds flood outward to everything of nearly the same colour.

```json
{ "wand": [[x, y]], "tol": 0.08, "mode": "wand" }
```

**`exact`**: use the region as drawn (with `feather`). For geometric things (a window opening, a frame, a sign), for subtracting, and for "everything except" layers.

**`level`**: coverage from value alone, for a pale figure on a dark ground (or the reverse) with a crisp edge: `{"poly": [...], "mode": "level", "tone": "light", "lo": 95, "hi": 175}` (luma 0..255; coverage rises from `lo` to `hi`). The region only bounds where to look: keep it tight on a side where a neighbour of like value sits. Add `exact` parts for dark features inside the figure (eyes, a pendant) and `contour` for its outline.

**`mask`**: `{"mode": "mask", "image": "masks/x.png"}` reads a grayscale selection made elsewhere (your own code, another tool) at the working picture's size, white = this layer; an optional region clips it. The file's pixels are part of the cache key.

For an opening, select the wall and subtract the opening with `minus`; what is seen through it belongs to the layer behind. Set `fill.reach` from the actual joint reveal requirement, not a fixed example width.

## 4. The fill

By default each lifted thing is replaced by paint copied from nearby parts of the same picture, matched patch by patch. This reuses source marks but does not identify the correct background surface, continuity, or brushwork. A supplied plate must represent the background after this particular peel, including the correct remaining occluders; it is not automatically trustworthy because it came from a file.

`"fill"` options:

- `"quality"`: 1 quick, 2 normal, 3 careful.
- `"reach": n`: only the band n px inside the hole's edge gets full care. Use it for big layers of which only the rim can ever be uncovered.
- `"from": [[[x,y],...], ...]`: restrict observed donors to these polygons. Patch donors must fit wholly inside the allowed source: the entire square patch, including its corners, must avoid forbidden pixels at every pyramid level.
- `"avoid": ["id", ...]`: exclude those layers' areas from donor selection. It does not establish which remaining surface is correct. Patch, smooth and region fills consume this exclusion.
- `"search": n`: how far (px) to look for paint.
- `"spread"` (default 3): expand the donor exclusion around the selection to avoid copying its contaminated edge. It does not extend the layer's ownership, increase alpha or repaint observed zero-alpha background.
- `"method": "smooth"`: a soft colour fill, for flat or gradient grounds (a plain sky). `"method": "none"`: do not fill (the thing will never move off its spot).

Additional fill modes and controls:

- `"method": "regions"`: assign every hole pixel exactly once using `regions`, each with geometric `target` parts, donor `from` polygons and `method` (`patch` or `smooth`). Use separate assignments where the hidden background crosses surfaces. Target overlaps, missing assignments and absent donors are errors; ownership is authored, not inferred.
- `"method": "plate", "image": "background-for-this-peel.png"`: consume a supplied opaque plate at the exact normalized working size. See the [reveal contract](reveal-contract.md) for source and transparency restrictions.
- `"method": "surfaces"`: repaint each hidden pixel only from the visible paint of its own surface in the plan-level `surfaces` map; the layer's own coverage (and `spread`) is never a donor. Options: `"rim": "repaint"` (default; for a layer that leaves its place) or `"keep"` with `"rim_level"` (repaint only coverage above it and keep the observed pixels under the soft rim; for a layer that shifts a little); `"as": "<surface>"` repaints the whole hole as that surface (round an opening, the interior continues); `"reach"` px limits work to the band that can be uncovered; `"texture"` overrides the surface's `smooth` (multiscale continuation, thin dark lines dropped unless `"lines": "keep"`) or `patch` (copies same-surface brush marks; for textured surfaces and small holes). A surface with too little visible paint near the hole borrows from all surfaces and says so in the log and in `plate_solve.surfaces`.
- `"confidence"` (default 0) and `"regularization"` (default 0.04): control the foreground-colour prior, not alpha. `confidence: 1` removes that prior; it is an author assertion, not a measured reliability score or a way to make an inaccurate plate pass. Alpha and the consumed plate remain fixed even if colours clip or rest residual remains.

Rules that apply whichever fill you use:

- **Surface ownership is authored.** Decide which background surface each hidden pixel belongs to (a hand-drawn surface map, or `regions`), and repaint each surface only from its own visible paint. Similar colour is not the same surface: a terracotta pot and a warm wall, a window interior and its frame. Guesses by colour class leak one surface into the next.
- **A layer's own coverage is never donor evidence.** Exclude every pixel the layer covers, its faint soft edge included, not only a `spread` band.
- **The soft rim follows how the layer moves.** A layer that only shifts a little can keep the observed pixels under its soft rim: rest stays exact, and lines running under the rim (a frame, a pilaster edge) survive. A layer that leaves its place entirely must have the rim repainted too, or a faint outline stays behind. The stock fill repaints everything the layer covers; to keep an observed rim, supply a plate (`method: plate`) built that way.
- **Openings continue inward.** Around an opening (window, door, arch), the band hidden by the front layer uncovers, under parallax, more of the opening's interior, not more wall: continue the interior there.
- **Texture needs texture.** On a textured surface copy same-surface brush marks (patch fill); a smooth fill shows the removed object's silhouette inside the texture. Smooth fills suit flat washes and bands that are never uncovered.

`"method": "surfaces"` implements these rules; with the other methods they are yours to enforce.

**Pigment** (a mark that will leave its ground). `"pigment": {"model": "glaze"}` treats the paint as a transparent glaze: observed = ground x transmittance; the layer stores the transmittance (the paint on white paper) and the bundle composites it with multiply, so on any ground it darkens it as a glaze would and takes the light of that ground. `floor` (default 0.03) keeps the old ground's own texture out; `grow` (px) widens the support. A glaze can only darken: the cut logs the share of the mark that is lighter than its ground, and a large share means the paint has body. `"pigment": {"model": "body", "reference": [[x0, y0, x1, y1], ...]}` treats it as semi-opaque paint: each rect is drawn round a mark where the same paint lies thick enough to hide its ground, with a margin of bare ground; the paint colour comes from there, and each pixel's coverage is how far it has moved from its ground toward that colour, so thin tails stay thin. `"paint": [r, g, b]` supplies the colour directly; `"coverage": k` a constant coverage when no colour is known. `layers.json` reports the paint, coverage, clipped pixels (off the ground-to-paint line: texture, another paint, or a wrong plate) and the reference spread. Inspect the pieces on several grounds afterwards (`picture.py inspect --pieces`).

When donor ownership or hidden geometry is unknown, repair those inputs rather than absorbing their error into alpha or writing foreground colour back into the plate. The foreground prior comes from observed opaque paint; it cannot recover missing detail or certify a soft edge. Keep matte uncertainty, background uncertainty and reconstruction residual separate.

The fill is good at continuing texture and at short gaps. It cannot invent what was never painted: a face behind a hand, the far bank behind a boat. Determine the required band from object motion, parent motion and camera together. Use existing author layers first. Repair the needed background with the correct surface and source marks; if an approved external image-editing service is needed, respect the user's existing authorization and private-material boundary. Do not hide an incorrect plate by reducing motion or depth. An artistic small amplitude needs its own image-based reason. When model completion and alternative expressions have actually been tried and a true conflict remains, apply the main rules, step 8; incomplete engineering is not that conflict.

## 5. Reading the review sheets

Before accepting a selection, compare it with the response planned from the original picture: trace the required carrier, its constraints, connections and representative movable regions across neighbouring backgrounds. A colour match or a populated bounding box does not establish structural completeness. A single extracted layer may carry relative deformation; it is not automatically a rigid body. Keep intentionally lost edges soft, and do not force inseparable paint apart.

`work/review/overview.jpg`: every layer's outline in a different colour on the dimmed picture. Check that each thing is fully enclosed, that nothing you meant to take was missed, and that neighbours do not eat into each other.

`work/review/layer-<id>.jpg`: three panels for one layer.

1. **Outline on the picture.** Does the line run on a real edge, and do deliberately soft/lost edges remain soft/lost?
2. **The lifted layer on a magenta checkerboard.** Is it the thing and only the thing? Background that came along shows as patches of the old ground. Holes in the thing show as checkerboard. A dark or pale fringe may indicate a coverage error. Compare it with the original boundary first; adjust coverage only for a confirmed extraction defect, preserving intentional softness.
3. **What is left behind.** Would you believe this is the painting with the thing never there? Inspect the entire measured joint reveal band; its width comes from the actual motion and viewpoints, not a fixed 10–20 px assumption.

`work/review/plate-after-<id>.jpg`: the same "left behind", whole and at full size. For a large layer the third panel is small; open this file with `look --box` on the edges that will be uncovered.

`work/review/base.jpg`: the final plate.

`work/review/surfaces.jpg`: the plan's surface map over the picture, when it declares one.

`work/review/rest-residual.png` and `rest_clusters` in `layers.json`: where the stacked layers differ from the picture at rest, with the largest clusters' boxes, so you can look there with `look --box`.

`picture.py inspect work --layer id`: the layer on several grounds beside the original and beside what it uncovers (all layers behind it stacked). `--pieces` shows every connected piece alone on each ground, numbered with its home position and core colour: the per-piece check for released marks.

`picture.py measure` and `picture.py palette` replace reading values by eye: per-row extents (`--rows`), separate marks with centre, axis and a ready blink entry (`--blobs`), an outline polygon with a margin (`--outline`), colours at points or a region's main colours. They are starting points; draw what they print back with `look --poly/--points` before using it.

## 6. Defects and what fixes them

| You see | Cause | Fix |
|---|---|---|
| Background patches lifted with the thing | snap on a group, or polygon too loose | select by colour instead, or tighten the polygon, lower `tol`, add `drop` points |
| Part of the thing missing | similar colour to the ground | add `keep` points or a second part for that piece |
| Thin parts (stems, whiskers, strings) cut off | snap smooths them away | add a `key` by tone or a `stroke` part for them |
| A pale or dark outline of the thing left on the plate | missed soft coverage, contaminated donors or an incorrect hidden surface | compare the original boundary, inspect matte and donor ownership, then verify the repaired plate in motion; `fill.spread` alone does not recolour visible background |
| A fringe of old background travelling with the layer | wrong alpha, foreground colour contamination or an inseparable wash treated as an object | inspect alpha separately from foreground colour on contrasting grounds; repair a separable edge without making it more opaque merely to reduce rest error |
| Fill is the wrong colour near an edge (floor colour where wall should be) | paint copied from the wrong neighbour | constrain donors by background surface or provide the correct plate; if extraction needs several parts, preserve one object's relationship and motion |
| Fill is smooth mud in the middle | the hole is large; nothing true to copy | verify it is outside the joint reveal band; otherwise repair the plate or revise the mechanism and recheck the intended expression |
| A line that should continue behind the thing (a horizon, a sill) is broken | patch fill does not extend long lines reliably | reconstruct the line on its correct background surface; verify the entire joint reveal band |
| Specks lifted that are not the thing | colour key too greedy | add those colours to `unlike`, raise `min_area`, use `cap` |
| Structured `rest_error` or encoded/browser difference | uncertain matte/plate, foreground regularization, sampling or encoding disagreement | compare native working image, composite and paused fallback separately; locate the residual and inspect its moving edge instead of forcing an exact fit |
| A thin outline stays put while the object moves | the object's own contour line was left on the plate | include the contour in the object's coverage and repaint under it |
| A faint outline or a smooth silhouette where a released piece used to be | the soft rim was kept on the plate, or a smooth fill was used inside texture | repaint the rim for layers that leave their place; copy same-surface texture |
| A released piece looks a different colour on other grounds | the transparent mixture was taken as the pigment colour | recover the pigment colour (motion.md §7) |
| Fill takes the colour of a neighbouring surface of similar hue (pot into wall, window into frame) | ownership guessed from colour | author the surface map or `regions` |

## 7. Reading coordinates

`picture.py look work/picture.png` draws grid lines labelled in picture pixels. For any outline, zoom first: `--box x0 y0 x1 y1` with `--grid 20` or `--grid 10` gives labels fine enough to place points within two or three pixels. Before a long cut, draw your outline or points back onto the picture to confirm them:

Use `look --poly` and `look --points` with the coordinates measured for that image. Keep representative per-image selections in fixtures rather than reusable rules.

The same goes for roots, pivots and eye centres in the scene: read them off a zoomed view, do not estimate them from the whole picture.

Bounding regions are measured too (row thresholds or a fine grid), not drawn by eye. Keep a region tight only on a side where a confounder sits (an adjacent area of similar colour or tone); elsewhere keep it loose and let colour or tone decide the edge. An eyeballed polygon tends to cut off part of the object.

## Current alpha and cache contract

The current prepare path applies orientation and ICC-to-sRGB once, preserves alpha and records the working size. Unknown embedded profiles fail explicitly. Cut carries alpha rather than flattening onto white. Hidden alpha beneath a fully opaque object is underdetermined and may be estimated; validate the exposed result on relevant backgrounds. Asset cache identity and later dynamic/reveal validation identity are distinct. Changing a camera need not re-cut unchanged assets, but invalidates prior reveal acceptance.
