# Apple Vision starting points (macOS)

`picture.py vision` asks Apple's Vision framework, which is built into macOS and runs on the machine, what it sees in the working picture: subject and person outlines, faces and their landmarks, body joints, where the eye goes, and lettering. It turns the answers into entries in this skill's own vocabulary (mask parts, blink and pose warps, protect polygons, fixed-layer snippets, saliency boxes). Vision was trained on photographs. On paintings it is often silent and sometimes confidently wrong, so every entry is a starting point that you check by looking, never an answer.

Nothing leaves the machine. Vision downloads nothing; the helper `scripts/macos/lpvision.swift` is compiled once per version with `xcrun swiftc -O` into `~/Library/Caches/living-picture/` (about 3 s). The helper carries the bundle id `living-picture`, so the text model macOS compiles for each new build (up to about a minute, on its first text pass) is cached in that same folder (`com.apple.e5rt.e5bundlecache`); deleting the folder removes everything the command keeps. Builds made before the bundle id was added left `~/Library/Caches/lpvision-<hash>/` folders, which are safe to delete. It needs macOS 14 or later and the Xcode command line tools (`xcode-select --install`). On any other system, without the compiler, or when that cache folder cannot be written (a sandbox), the command prints `{"error": ..., "available": false}`, exits with status 3, and the rest of the skill works as before. Bad options, an unreadable picture or an `--out` it will not replace give `{"error": ...}` and status 2, before Vision runs.

## Running it

```
python3 scripts/picture.py vision <job>/work/picture.png --out <job>/work/vision
```

| Option | Use |
|---|---|
| `--what subjects,persons,faces,pose,saliency,text` | which analyses to run (default: all six) |
| `--box x0 y0 x1 y1` | look only at this region, enlarged up to 4x (longer side about 1280 px). Small faces and figures are found this way; results stay in picture pixels and masks stay picture-sized |
| `--zoom k` | the enlargement, more than 0 and at most 8, with or without `--box` |
| `--face x0 y0 x1 y1` | a face you can see that detection missed: landmarks are fitted inside this box (repeatable) |
| `--eyes '[[x,y],[x,y,r]]'` | eyes you can see: blink entries are built from the painted marks there (`r` is a rough radius) |
| `--langs zh-Hans,en-US` | lettering languages, most likely first. By default the language is detected, which read mixed Latin and Chinese lettering better than a fixed list in testing |
| `--plan-dir DIR` | where `plan.json` lives, for the mask paths in the snippets (default: the job folder above `work/`) |
| `--force` | replace an earlier run in `--out` even though its masks change |

Output in `--out`: `vision.json` (everything below), `review.jpg` (one labelled sheet: the picture with every result drawn on it, the attention heat beside it, and each eye enlarged at the bottom), and grayscale PNG masks and heat maps at the picture's size. stdout gets a short summary. Warm runs take about 2 s for a 1200 px picture on an M2 Max (subjects 0.2 s, persons 0.4–0.6 s, faces 0.05 s, pose 0.2 s, saliency 0.08 s, text 0.15 s). The first text pass of a new build, and of a new language setting, can take 25 s to a little over a minute while macOS compiles its text model for the helper; the command says so when that may happen.

**One folder holds one run.** A run is written to a temporary folder beside `--out` and moved into place only when it is complete, so a failure leaves the folder as it was and a new run leaves nothing stale behind. `plan.json` may point at an earlier run's masks, so a new run replaces it only when those masks come out the same: same picture, `--box` and `--zoom`, with the analyses that made them asked again (adding `--eyes` to the same command is fine). Otherwise it stops before Vision runs and says why; if Vision itself answers differently (after a macOS update, say), it stops after the run and leaves the folder as it was. Give each `--box` its own folder (`work/vision-face`, `work/vision-window`), or pass `--force` when no plan uses the old masks. A folder holding files that vision does not write is never replaced.

Coordinates are picture pixels with the origin at the top left. Polygons meant for `plan.json` (outlines, snap regions, protect and fixed-layer polygons) follow the cut tools, which take pixel `x` to sit at `x`. Blink entries follow the player, where pixel `x` spans `x` to `x + 1` with its centre at `x + 0.5`, so a mark covering pixels 544–549 is centred at 547.0. Transparency is flattened on white and EXIF orientation is applied, as in the skill's own analysis view.

## What each analysis gives and how to use it

**Subjects** (`subjects`): Vision's "lift the subject" masks, one per separable thing, soft-edged. Each entry carries:

- `part`: `{"mode": "mask", "image": "work/vision/subject-1.png"}`, ready for a layer's `parts` in plan.json. Vision computes these masks at low resolution and enlarges them, so on crisp paint the edge is soft and a few pixels loose, and thin parts (a trunk's foot, a wire, a tail) can be missing.
- `pieces`: the separate pieces of the mask, largest first, each with `bbox`, `px`, `outline` (to draw with `look --poly`) and `snap_part`: `{"mode": "snap", "poly": [...]}`, a rough region for `snap`, which then finds the painted edge. One mask can hold several things (one person instance held two dancing figures in a test), so paste the snap part of every piece you want; a piece left out is not cut. `specks` counts the tiny pieces that got no entry. On the example's flat-colour tree the snap part gave a clean crisp cut where the raw mask left a soft halo. Prefer it for hard edges; keep the mask for soft, wet or furry edges, and clip either with your own poly where it overreaches.
- `bbox`, `share` of the picture.

On a busy painting a subject is often a whole scene block: on the watercolour shop front it was the entire building (70% of the picture), and inside a zoomed `--box` of the window it was the whole window interior. Such a mask can still carry one true edge: clipped with a `rect` on the mask part (a region on a mask part keeps only what is inside it), the window mask followed the painted rabbit's left outline well. Otherwise use it only as a region, or not at all.

**Persons** (`persons`): one mask per person (with `pieces` as above) plus `persons_matte`, a finer matte (hair, fingers) that merges everyone. Each person has `matte_agreement`, the share of the instance the matte confirms; below 0.3 it is marked `doubtful`. Both models raise false alarms on paintings: a flat grey test picture got a "person" (no matte agreement, flagged doubtful), and the example's black cat silhouette was a person with 0.99 confidence that the matte confirmed. Trust a person mask only after looking.

**Faces** (`faces`): boxes, Vision's roll/yaw/pitch, and landmarks (eyes, pupils, brows, nose, lips, face contour). Detection needs a fairly realistic face: a painterly realistic face only 24 px wide was found, with plausible landmarks; anime faces were mostly missed (one of several, and only after a 3x enlargement, with its eye landmarks on the nose); cartoon (emoji) and animal faces were not found at all. Small faces are missed more often, so try `--box` around them. `--face` fits landmarks inside a box you give: on a cartoon human face the brows, nose and mouth landed well and the eye landmarks hugged only the upper lid; on the watercolour rabbit everything collapsed to one point. Eyes are named by their side in the picture (`face-1-eye-left` is the one on the left as you look), whatever Vision or the pose models call them.

**Eyes, as blink warps** (`eyes`): built from face landmarks, body-pose and animal-pose eye joints, and `--eyes`. For each eye the tool finds the painted mark around the seed (paint that differs from the surrounding surface, holes such as an eye white filled), then writes a blink entry in the player's frame:

```json
{"type": "blink", "at": [547.0, 271.5], "r": [5.0, 5.5], "mask": [[546.5, 267.0], [547.5, 267.0], [551.0, 270.5], [551.0, 272.5], [547.5, 276.0], [546.5, 276.0], [543.0, 272.5], [543.0, 270.5]]}
```

The mask polygon follows the pixel edges of the mark grown by one pixel (its soft edge) and cuts each corner half-way between pixel centres, so the player moves exactly those pixels: the whole mark and the ring round it, nothing else (`checks.mask_px` counts them with the player's own test). `r` leaves 1.25 px of observed paint round the polygon, so the domain holds a full ring of untouched surface. When two eye domains would collide, each eye involved drops once to 1 px, the player's minimum. When the marks are only a few pixels apart and they still collide, the eye in most collisions keeps the mark alone, without its soft edge (`soft_edge_left`: look at the closed eye for a faint ring). `rot` is added when the eye is tilted more than 4 degrees (the eye line of a face, otherwise the corner-to-corner axis of the mark; a tall cartoon oval closes along its length).

`checks` repeats the player's own tests: the domain inside the picture with a one-pixel halo (the player tests against the face layer's own rect, so cut that layer wide enough around the eyes), eye domains not overlapping (`overlaps` names the others; the player refuses overlapping eye domains), at most 256 px across. It also reports `margin_px` (the margin actually left), `spills` (the mark ran into a brow, hair or an outline), `off_landmarks` (the mark is not where, or not the size, the landmarks put the eye) and `mark_left` (part of the mark falls outside the polygon and would stay when the eye closes). `ok` means none of these failed and nothing was left out; it does not mean the mark is the eye.

Paste the `blink` object into the `warps` of the layer that holds the face, after its head `pose` warp. Then look at the enlarged eye in `review.jpg` and at frames with the eye closed: a copy of the scene with the blink channel held at `{"type": "const", "value": 1}` shows the fully closed state. On the example's cat (`--eyes`) the entries closed to a clean lid line in such a frame; the same masks half a pixel off left yellow paint and a smear. On the watercolour rabbit (`--eyes`) both eyes were ok and closed cleanly. On small cartoon figures the pose eye joints were often a few pixels off and found nothing or a highlight (flagged, or visible in the tile). Where nothing is found, the entry says so: place the eye yourself (`measure --blobs`), and keep a mask you write in the player's frame.

**Heads, as pose warps** (`heads`): for each face, figure and animal, a `pose` warp to start a head turn from:

```json
{"type": "pose", "pivot": [585.1, 171.7], "region": {"c": [602.7, 131.3], "r": [28.0, 31.8], "core": 0.6}, "angle": "tilt"}
```

The pivot sits below the chin at the neck (the body-pose neck joint when one is near), the ellipse takes in the face and hair. Check the ellipse in `review.jpg` against the actual head and hair, and that the region does not reach a shoulder or the background you do not want to bend; the channel name `tilt` is a placeholder.

**Joints** (`pose`): 19 body joints per human figure and 25 per animal (cats and dogs; `animal_labels` names them), each `[x, y, confidence]`. Left and right are the figure's own sides, so a figure facing you has its left on the picture's right. Each figure also gets `limbs`: pose warps that turn an upper arm, forearm, thigh, shin, ear, tail or leg about its joint (pivot at the joint, an ellipse along the bone), as pivot hints for those parts. Body pose worked on cartoon people (emoji figures, 16–17 joints each) and partly on illustrated posters; animal pose worked on cartoon cat and dog drawings with visible eyes and legs, not on the example's black cat silhouette or the watercolour rabbit.

**Where the eye goes** (`saliency`): two heat maps (`attention.png`, `objectness.png`) and their `peaks`, each a point, a value and the box of the region above half its value. Attention found the focal character every time it was tried: the rabbit in the shop window, the cat on the roof, the faces of figures. Use it to check your reading of the picture, to put `camera.focus` at the depth of the layer under the strongest attention peak, and to keep released marks out of that box (choose `sprites.region` and release areas so pieces do not cross it, then check frames). Objectness marks separable things; with `--box` on the shop window it isolated the rabbit, a cup and the banner. The heat maps are coarse (68 by 68 cells): boxes, not outlines.

**Lettering** (`text`, `text_marks`): every line read, with its string, confidence, rotated quad, and two snippets:

- `protect`: `{"poly": [...]}` (the quad grown by 15% of the text height, at least 3 px), for the `protect` list of any layer that must not take part of the lettering.
- `fixed`: a `plan_layer` (`"mode": "exact"`, `"feather": 0`, so its alpha is binary) and a `scene_layer` (`"fixed": true`). Put the plan layer first in plan.json, so it is peeled before everything else and the layers behind lose their copy of the lettering; the scene layer then keeps the original pixels still on top. Fixed needs an opaque picture; `bundle` verifies it.

Choose by what the lettering belongs to. A signature, seal, watermark or caption on the picture plane is fixed. Lettering painted on a sign or a shop front belongs to that surface and moves with it if it moves: keep it inside that surface's layer and use `protect` only on the neighbouring layers.

`doubtful` marks lines read with confidence below 0.5 or with no letters or digits (a leaf read as "•"). `text_marks` are text-like regions that could not be read (scrawled signatures, half-hidden lettering); they carry a `protect` polygon too, and also fire on non-text (a whole dancing figure in one test). Read on the test pictures: painted shop lettering (a short word at confidence 1.0; a longer, smaller sign line boxed right but with several letters misread), a brush-script title, a cursive rotated signature, a red seal ("山水", "之印"). Missed: a vertical column of Chinese characters (Vision reads horizontal lines only; mark vertical writing by eye).

## Contours

Vision's contour detection is not offered: on the watercolour it returned 583 contours (376 top-level) of brush texture, and `measure --outline` and the cut modes already outline what passes a stated rule.

## Checking

Read `review.jpg` first: tinted masks with each piece outlined, magenta protect boxes with the strings read, yellow face boxes, orange head ellipses and pivots, red and green skeletons, green (ok) or red eye domains, and enlarged eyes with their mask polygons, which should run one pixel round the painted mark on every side. Then check each entry you use at native size (`picture.py look --box ... --poly ...`), `picture.py inspect` the layers cut from it, and look at rendered frames as for anything else. A missing result means nothing about the picture; an empty `errors` means only that Vision ran.
