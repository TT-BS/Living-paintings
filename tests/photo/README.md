# Casual-photo benchmark for `picture.py rectify`

An independent test of the step that turns a casual phone photo of a painting into the painting itself:
found and cropped, perspective and lens undone, light evened out and colour cast removed only against real
neutral evidence, nothing restyled, a flat scan left alone, a reshoot asked for when that is the honest
answer. It was built to the contract alone (CLI, JSON keys, exit codes), without reading the rectify code,
so neither side can be tuned to the other by accident.

Two scripts:

- `make_photos.py` renders simulated phone photos with exact ground truth (seeded, reproducible).
- `bench.py` runs rectify on every case, measures it against the truth, and exits non-zero when an
  acceptance threshold fails. `--self-test` first proves the benchmark with stand-ins.

Everything runs locally with Python 3, numpy, scipy and Pillow. Nothing is downloaded or uploaded.

## Running it

```sh
SKILL=~/.claude/skills/living-picture
python3 "$SKILL/tests/photo/make_photos.py" --out /tmp/rectify-cases                 # 32 cases, 3-7 min
python3 "$SKILL/tests/photo/bench.py" /tmp/rectify-cases --out /tmp/rectify-report   # 3-6 min, one case at a time
python3 "$SKILL/tests/photo/bench.py" /tmp/rectify-cases --out /tmp/rectify-self --self-test   # 15-20 min
```

The times are for an M2 Max shared with other work; the 48 MP case takes about a minute of each.

Both refuse to write inside the skill folder. Useful options:

| option | what it does |
|---|---|
| `make_photos.py --seed N` | another draw of every random parameter (default 1) |
| `make_photos.py --only GLOB,...` | regenerate some cases; the index keeps the others |
| `make_photos.py --extra PATH ...` | a local flat picture as extra ground truth: a no-op case (the file must pass unchanged) and the same picture hung on a wall |
| `make_photos.py --real PATH ...` | a real casual photo with no ground truth: run and shown, not scored |
| `bench.py --cases GLOB,...` | run some cases only |
| `bench.py --jobs N` | cases in parallel; the run time and memory are then reported, never gated |
| `bench.py --tool "CMD ..."` | benchmark another command with the same CLI |
| `bench.py --standin NAME` | benchmark one stand-in (below) |
| `bench.py --timeout S` | seconds per run before it counts as failed (default 300) |

`--extra` and `--real` cases are for local checks only: never commit them or any generated folder.

Output, in the report folder: `report.json` (every number and check), `summary.md` (one row per case, notes
below), `sheets/<case>.jpg` (the photo with the true quad in green and the reported one in magenta, the
output, gt, a CIEDE2000 map, the tool's own review sheet underneath) and `overview.jpg`. One JSON summary is
printed. Exit 0 when every gating case passes, 1 when one fails, 2 on a usage error.

## The cases

Every case folder holds `photo.jpg` (what the phone saved; PNG for lossless no-op cases), `gt.png` (the
artwork exactly as rectify should return it), `meta.json` (every parameter, the true corners in the photo,
the gt-to-photo mapping, what must and must not happen), and where needed `ignore.png` (gt pixels nobody can
recover: glare, fingers, the part outside the photo), `glare.png`, and `truth.npz` (light and colour fields
for the oracle).

The artworks are drawn by the script or are the skill's own example, so nothing is copyrighted: a
watercolour wash on white paper whose margin is part of the work; an oil with impasto and a strong
deliberate vertical gradient (light sky, near-black foreground); a saturated red colour field with no
neutral pixel; a dark nocturne; a small ink figure on a large plain sheet; the night-house picture as a print
with a white border; a screenshot with a share-platform style watermark (a generic pill and ID line).

The forward model, in linear light: the work (with its paper margin, mat, frame, and the wall, table or
hands round it) lies on a plane; a pinhole camera at a plausible viewpoint (tilt 0 to 35 degrees, roll,
the work filling 22 to 86% of the frame) sees it through a homography; radial lens distortion moves the
points (k1 from -0.08 to 0.05); a lamp falloff, a broad hotspot and soft shadows light the plane, the lens
vignettes; a von Kries cast (a 2700-3200 K or 6800-8000 K blackbody lamp the white balance did not fully
remove) and auto exposure scale it; then optical blur or camera shake, sensor noise as a phone leaves it,
ISP chroma smoothing and sharpening, JPEG quality 82-93, EXIF focal length in some cases, EXIF orientation 6
in one.

| group | cases | what they test |
|---|---|---|
| no-op | `noop-scan-nighthouse`, `-scan-wash`, `-screenshot-watermark`, `-gradient`, `-red`, `-figure` | a flat scan or screenshot must pass unchanged (exit 4); no cropping of a paper margin or plain ground, no evening of a painted gradient, no neutralising of a red painting |
| correct | `wash-wall-warm` (12 MP), `print-stone-cool`, `oilgrad-gold-warm`, `red-wall-falloff`, `red-wall-warm`, `oil-tealwall-warm`, `dark-mat-cool`, `figure-hands-warm`, `print-wall-shadow`, `wash-plaster-barrel`, `oilgrad-frontal-fill`, `print-pincushion-tele`, `gallery-neighbour`, `exif-rotated` (12 MP, orientation 6), `wash-table-wood` | walls (plain, grey, teal, textured plaster), a stone and a wooden table, a room behind a sheet held in two hands, gold, black and wood frames with a mat and a frame shadow, a neighbouring painting in view, barrel and pincushion distortion, warm, cool and mild casts, falloff, hotspot and shadow bands up to 50% |
| perf | `perf-48mp` | a 48 MP photo (8064 x 6048): everything above, plus run time and peak memory |
| option | `opt-corners`, `opt-frame-keep`, `opt-off` | `--corners` must be used (white paper on a white wall), `--frame keep` returns mat and moulding, `--light off --color off` leave both as shot |
| stress | `stress-glare`, `-cutoff`, `-steep`, `-motionblur`, `-lowres` | a reshoot is the honest answer: the plain-words warning is required (for the steep view a refusal also passes), glare must not be painted over |
| stretch | `stretch-hard-shadow`, `-mixed-light` | beyond a global light and colour model: reported, never gating |

What a case expects follows from its own evidence, with the same yardsticks the bench judges by. Light
must be corrected when it varies over the work by more than the 4% gate and some neutral evidence is in view
(the paper margin, a mat, or a wall or table whose chroma is at most 3); colour when the cast leaves a mean
CIEDE2000 above 3. Without evidence the correction is forbidden: gt then keeps the cast or the falloff, and
the warnings or note must say colour was left as shot. A light correction is only gating when a smooth
surface fitted to the evidence predicts the light over the work to 2.5%; otherwise the case becomes a
stretch case. The aspect ratio tolerance is 1% when EXIF has the focal length, else what an ideal method
reaches (vanishing points, or assuming the usual 26 mm phone lens) with slack.

## What the numbers mean

The thresholds live in `bench.py`'s docstring, which is the authority; in short:

| check | measured how | passes at |
|---|---|---|
| corners | the output is aligned to gt by a direct coarse-to-fine homography fit on locally normalised luminance; where the output's four corners land in gt, against gt's corners | <= 0.8% of the diagonal |
| aspect | width over height of the work in the output against gt | <= 1% (or the case's tolerance) |
| straight | the lens bow left after that homography (matched to the case's own distortion pattern) or the largest local shift of a textured patch | <= 0.35% of the diagonal |
| quad | the corners rectify reports, against the work's true corners in the photo (or its mat's or frame's) | <= 1.5% |
| resolution | output long side over the work's long side in the photo, capped at 4096 | 0.7 to 2.0 |
| colour | CIEDE2000 on a comparison grid of at most 400 px after one global exposure factor, each pixel against the best of its 3 x 3 gt neighbours | mean <= 3, P95 <= 10 |
| light | low-frequency variation left in output / gt luminance after per-channel gains (P98 - P2 of the log ratio of local means, Gaussian of 4% of the diagonal) | <= 4% |
| chroma | mean C* output / gt, where gt's mean C* >= 10 | 0.85 to 1.15 |
| contrast | std of L* output / gt, smoothed by one grid pixel, where gt's is >= 5 | 0.88 to 1.12 |
| sharpen | fine-detail energy of each log channel in the output over the photo resampled through the true mapping with a cubic spline (gains cannot read as sharpening) | <= 1.2 |
| no-op | exit 4 with nothing written; an output must match to corners 0.5%, CIEDE2000 mean 1.5 / P95 4 without exposure refit, light 2%, and the restyle limits | |
| words | every expected warning topic in `warnings` (English or Chinese plain words; the note does not count), no reshoot warning where none is due, colour left as shot said in the warnings or the note | |
| perf | `perf-48mp` with `--jobs 1`: run time and peak resident memory of the largest process | <= 30 s, <= 3 GB |

Why alignment cannot hide a geometric error: the homography only lines up the content. The output's own
corners must land on gt's corners, so an over-crop, a kept frame or a wrong corner shows as a corner error,
a stretched output as an aspect error, and a bow (which no homography removes) as a straightness error.
Exposure is refitted because a casual photo cannot know it; per-channel gains are not, so a cast cannot hide.
A painting's own gradient is in gt, so flattening it reads as residual light.

When the boundary is off, the summary notes say how far each side is inside the work (+, an inner edge
taken for the work's) or beyond it (-, a frame, mat or wall kept), and whether the output is a
watercolour's painted area without its margin, a print without its border, or a figure without its sheet.

## Stand-ins: proving the benchmark

`--self-test` runs every stand-in below on every case and checks a matrix of expectations. Each stand-in
other than oracle, identity and copy is the oracle with exactly one fault, reporting the oracle's flags and
warnings, so a measurement and not a flag must catch it. For every case the self-test knows whether the
stand-in must pass, must fail, or has no expectation (its fault too small to show on that work); where it
must fail, the failure has to come from the right check and from nothing else.

| stand-in | its fault | must fail where | and by |
|---|---|---|---|
| oracle | none: inverts the exact forward model | nowhere | |
| identity | exits 4 everywhere | every photo needing correction; stress cases without the warning | exit; the warning |
| copy | returns the photo | every photo | corners, alignment |
| geometry | leaves light and colour, claims both corrected | where light or colour must be corrected | light, colour, chroma |
| nolens | a plain homography from the true corners | where the lens bow exceeds 0.35% | straightness, corners |
| nolight | leaves the light, claims it corrected | where light must be corrected | light |
| grayworld | grey world instead of the evidence | where grey world visibly changes the work (predicted on gt); where colour must stay as shot | colour, chroma; or the flag and the missing "said so" |
| flatten | divides out the picture's own low-frequency light | where that visibly changes the work (predicted on gt) | light, contrast |
| overcrop | stops 1.5% short of every edge | every photo | corners |
| stretch | output 3% too wide, corners exact | every photo | aspect |
| sharpen | a mild unsharp mask (0.5, 1 px) | everywhere, flat no-op pictures included | sharpen |
| enhance | +15% saturation and contrast | where that visibly changes the work (predicted on gt) | a restyle check |
| overcorrect | all four restyles, everywhere, no warnings | everywhere | anything but the contract |

The self-test also checks CIEDE2000 against Sharma's published pairs and the camera model against every
case's stored corners. Its matrix is written to `self-test.txt` in the report folder, with
`predictions.json` (how much of each limit the work-dependent restyles use on gt).

Result on the default set (seed 1, 2026-11-16): the self-test passes. 13 stand-ins on 32 cases make 392
cells: 241 where a stand-in must fail (every one caught, each by its own check and nothing outside its
allowed set), 133 where it must pass (all pass), 18 without expectation. CIEDE2000 agrees with Sharma's
pairs to 4e-5. It took 17 minutes with 6 jobs on a shared M2 Max.

How far apart right and wrong land, over the gating cases:

| quantity | oracle (worst) | gate | the faulty stand-in (best) |
|---|---|---|---|
| corners | 0.03% | 0.8% | overcrop 1.50% |
| aspect | 0.05% | 1% | stretch 2.9% |
| straightness | 0.13% | 0.35% | nolens 0.56% (on the 3 cases with a real bow) |
| light | 2.0% | 4% | nolight 9.0% (dark nocturne) to 51% |
| colour, mean CIEDE2000 | 2.3 (sensor noise in the near-black nocturne) | 3 | geometry 3.6 to 14.9 |
| sharpen | 1.07 | 1.2 | sharpen 1.27 (12 MP watercolour) to 1.73 |
| chroma, contrast | 1.04, 1.02 | 0.85-1.15, 0.88-1.12 | enhance, flatten, grayworld: fail where predicted |

The oracle's colour margin on the dark nocturne (2.3 against 3) is the tightest: a real tool's own colour
error there has about 0.7 of room on top of the photo's noise.

## Limits

- The photos are rendered, not shot: one plane, a pinhole with one radial term, smooth lamp fields, a
  global von Kries cast. Real paper cockles, real lenses have more terms, real light can be mixed and
  hard (the stretch cases), and real phones tone-map. The real-photo path (`--real`) only checks that the
  tool runs.
- Warnings are judged by vocabulary (`TOPICS` and `ALARMS` in bench.py, English and Chinese): it can tell a
  glare warning from none, not a good one from a clumsy one. A warning counts when it names the problem
  or the remedy in plain words; "part of the picture reached white" names neither the glare nor a reshoot.
- The contrast check skips works without tonal range (a few ink lines on a white sheet), and chroma skips
  near-neutral ones; there the colour and sharpen checks still apply.
- The sharpen gate sits between honest resampling (0.9-1.1, the core and the oracle) and a mild unsharp mask
  of 0.5 at 1 px (1.27 on the softest photo, a 12 MP watercolour); a weaker mask can pass there.
- The perf gate is only meaningful with `--jobs 1` on an otherwise quiet machine; the brief's targets
  (30 s, 3 GB on an M2 Max) are the limits. Peak memory is the largest single process (wait4), not a sum.
- HEIC input is not covered: Pillow can neither write nor read it, macOS's `sips` drops the EXIF orientation
  on conversion, and the bench must also run off macOS. EXIF orientation is covered on JPEG.

## Known blind spots (found by independent reviewers, not fixed yet)

A pass here is weaker evidence than the numbers suggest. Read results with these in mind:

- **No-op cases accept exit 0.** `noop_meta` allows exit 0 as well as 4, so a tool that crops a flat
  picture by about 0.35% per corner, even with a slight warm tint, passes all six no-op cases. The intended
  rule is exit 4 only.
- **The corner gate passes visible surroundings.** 0.8% of the diagonal lets about 10 px of frame, wall or mat
  survive at 1600 px; the signed per-side offset the bench computes for its notes is not gated.
- **Stress cases never gate geometry.** A stress case that exits 0 with no boundary (the whole photo,
  recoloured against the wall) or with a side cut into the work still gets "pass".
- **Exposure is hidden by the refit.** Colour is scored after one global exposure refit, so a 1.35-1.6x
  brightening (a grey mat mapped to paper white, or exposure applied under `--light off --color off`)
  scores as perfect.
- **The straightness gate is near its noise.** On fresh seeds the matched filter's error on the bow is close
  to the 0.35% gate (the self-test failed on seed 31: the oracle read 0.142% where the truth was 0.153%);
  lens verdicts close to the gate are not reliable.
- **The resolution gate allows 2x upsampling** (`TH['res'] = (0.7, 2.0)`), although rectify must never enlarge.
- **Missing scenes.** No brick, tile or gridded cutting-mat backgrounds; no light or white frames, no frame
  without a mat under a crisp lamp shadow, no tinted mats (cream, pale blue) or tinted walls (beige, peach,
  sky blue) under neutral light, no pale painting without a margin, no unframed work with a cast shadow, no
  coloured wall with strong lamp falloff, no two equal works side by side, and no flat picture carrying camera
  EXIF or containing a slightly tilted painted rectangle. On those scenes the core's boundary or colour went
  wrong in the reviewers' tests (see `references/photo.md`, "Known failure modes").
