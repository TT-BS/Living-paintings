# Motion vocabulary: the scene file

`scene.json` describes how the cut layers are stacked in depth and how each is built to move. `bundle` joins it with the cut layers (it fills in each layer's image and rectangle from `work/layers.json`, matched by `id`). All coordinates are pixels of `work/picture.png`. Angles are degrees. Times are seconds. Any range `[a, b]` means "a new random value in this range each time".

Everything here is a starting vocabulary, not a required catalogue of object actions. Choose the leading dynamic from the picture: a supported action or a distributed change in light, air or water. Example numbers explain available parameters; they are not cross-picture aesthetic defaults. Where the vocabulary runs out, use `hooks` (section 11).

## Contents

1. Shape of the file
2. Camera and depth
3. Wind
4. Rooted things: `sway`
5. Living things: channels and warps
6. Whole-layer motion: `swing`, `bob`, `pose`, `swim`
7. Loose marks: `sprites`
8. Flowing paint, ripples
9. Light
10. Families: parent and attach
11. Your own code: `hooks`
12. Original image, first frame and rest
13. Parameter selection
14. Sprite region and fixed coverage limits

## 1. Shape of the file

```json
{
  "work": "work",
  "seed": 7,
  "camera": { "travel": 0.04, "focus": 0.5 },
  "wind":   { "dir": [1, 0.1], "breeze": 0.14, "gust": { "every": [5, 10], "strength": [0.45, 0.85] } },
  "light":  { "shade": { "amount": 0.3 } },
  "channels": { "blink": { "type": "blink" } },
  "layers": [
    { "id": "sky",  "depth": 0.95 },
    { "id": "tree", "depth": { "on": "ground", "at": [412, 880] },
      "motion": [ { "type": "sway", "preset": "tree", "root": [412, 880] } ] }
  ]
}
```

`layers` run back to front (the reverse of the cutting plan). The same `seed` always plays the same film; change it to get different weather.

Per-layer fields: `id`; `from` (use the cut layer of another id); `format` (`"png"` keeps this one layer lossless whatever `bundle` would choose); `depth`; `motion` (list of deformers); `warps`; `rigid`; `sprites`; `flow`; `ripples`; `light`; `parent` / `attach`; `opacity`; `fade` (a channel, 0..1); `mesh` (cell size in px, default 26; smaller follows tight bends better); `relight` (true or 0..1: paint that moves takes the light where it now is, from the scene's `lightMap`, section 9); `blend` (`"multiply"` is set by the cut for pigment glazes; a scene cannot contradict its asset).

## 2. Camera and depth

Depth runs from 0 (nearest the viewer) to 1 (farthest). When the viewpoint shifts, each point slides in proportion to its distance from the `focus` depth, in opposite directions on either side of it. That is what makes the view feel like turning rather than panning.

```json
"camera": { "travel": 0.04, "focus": 0.5, "drift": 0.55, "pointer": 1, "scroll": 0.5 }
```

- `travel`: how far a point one full depth-unit from the focus slides at the extreme of the view, as a fraction of picture width. Its perceived strength depends on depth differences, picture geometry and display size. Larger travel uncovers more behind each layer; choose it from the intended space and validate the joint reveal band.
- `focus`: the depth that does not move. Put it on the subject.
- `drift`: slow idle wandering of the view (0 turns it off). This is what shows depth on a phone, where there is no pointer.
- `pointer`, `scroll`: how much the pointer position and page scroll move the view.
- `edge`: `"hold"` (default) keeps the picture's own border where it is and lets the shift grow inward from it over a margin (`hold`, default 0.07 of the shorter side), to reduce border reveal. This does not prove signature protection or remove moving duplicates; inspect border distortion and fixed content. `"mirror"` shifts the whole plane and fills the gap at the border with mirrored paint: more depth right up to the edge, but the mirrored strip can show.

Ways to give a layer its depth:

| Form | Meaning |
|---|---|
| `0.7` | one flat plane |
| `{"ramp": [[x1,y1,d1],[x2,y2,d2]]}` | changes steadily from one point to another (a floor running back, a wall seen at an angle) |
| `{"points": [[x,y,d], ...]}` | a depth sketch: a handful of anchors, blended smoothly. Give several anchors the same depth to keep a wall flat. |
| `{"on": "ground", "at": [x,y]}` | the depth of layer `ground` at that point: for anything standing on something. This shares the sampled depth at its footing; separate local motion, varying surfaces and approximate attachment still need contact checks. |
| `{"on": "ground", "perRoot": true}` | for a `sway` layer with many roots: each root takes the depth of `ground` at its own root point |

`"bulge": 0.04` on a cut-out layer brings its middle slightly nearer than its rim, so that when the view shifts it seems to turn a little instead of sliding like a card. This is optional geometric depth variation, not inferred 3D. Check medium, silhouette and attachment consistency before using it; parented bulge combinations may be rejected by the current runtime.

A child with a `parent` takes its parent's motion (section 10); give it the same depth as the parent.

Full-frame layers may receive mirrored margins. This can hide a gap but may expose repeated marks; verify the permitted viewpoints rather than treating the extension as valid reconstructed background.

## 3. Wind

One shared wind field may drive the scene. Breeze can be zero; use continuous wind or gust fronts according to the painted event. Gust fronts enter from upwind and travel across, so different positions can be reached in order.

```json
"wind": { "dir": [1, 0.1], "breeze": 0.14,
          "gust": { "every": [5, 10], "strength": [0.45, 0.85], "speed": 0.5, "length": [1.4, 2.8], "first": [1.2, 2] },
          "pointer": 0.8 }
```

- `dir`: where it blows toward. Take it from the picture if anything is painted mid-flutter.
- `breeze`: the constant background (0.1 to 0.2 is a calm day).
- `gust.strength`: 0.3 to 0.6 a stir, 0.6 to 0.9 a real gust, above 1 a storm. Match the weather of the picture.
- `gust.speed`: picture-widths per second that a front travels. `gust.length`: how long it blows at a place. `gust.first`: when the first one comes. `"gust": false` leaves only the breeze.
- `pointer`: a pointer swept quickly across the picture makes a local puff, like someone walking past (0 disables). Puffs add to whatever gust is blowing and no layer has a cap of its own, so a strong pointer gain can push cloth past the reveal you prepared.
- A touch or click: with no `"tap"` at the top of the scene, every tap is a 0.9-strength puff, with no throttle. Set `"tap": "none"`, `"tap": "ripple"`, or write `poke(world, x, y, LP)` in hooks for a gentler or throttled answer. Verify taps and sweeps explicitly (`capture.mjs --tap "t,x,y"`, `--puff "t,x,y,dx,dy"` at the strength a fast sweep reaches): `--pointer corners` only sets the view and never makes a puff, and seeked captures run without the live pointer handler.
- `opening`: for a picture painted in the middle of a gust (a skirt blown sideways, a bent tree, streaming hair). `"opening": {"strength": 0.8, "length": 2.2}` starts the film with that gust already blowing everywhere, then lets it die away; later gusts arrive as usual. Pair it with `painted` on the things it holds (section 4), so that they are in their painted pose while it blows and relax when it drops. Without this, a wind-blown picture would sit frozen until the first gust travelled in, and its cloth would never hang.

Anything can read the wind: `sway` does by default; a channel of type `wind` gives a creature or a custom effect its own reading.

## 4. Rooted things: `sway`

For anything fixed at one end and free at the other, or held all along and merely trembling: trees, bushes, stems, grass, hanging vines, hair, cloth, tails, ribbons.

```json
"motion": [ { "type": "sway", "preset": "plant", "root": [236, 962] } ]
"motion": [ { "type": "sway", "preset": "plant", "roots": [[236,962],[338,958],[598,958]] } ]
"motion": [ { "type": "sway", "preset": "cloth", "root": [310, 420], "tip": [470, 560] } ]
```

The layer bends about its root: nothing at the root, most at the tip, the tip arriving a moment late. With several `roots`, every bit of paint belongs to the root it is connected to (walking through the painted shape), and each root gets its own spring, so plants in one layer move independently.

| Preset | For | bend | freq | damp | lag | flutter |
|---|---|---|---|---|---|---|
| `tree` | trunks with a crown | 2.0 | 0.45 | 0.25 | 0.08 | 3.4 |
| `bush` | shrubs, topiary | 4.4 | 0.9 | 0.3 | 0.1 | 4.0 |
| `plant` | potted plants, flowers on stems | 8 | 1.25 | 0.28 | 0.12 | 3.0 |
| `grass` | grass, reeds, single stalks | 15 | 1.8 | 0.3 | 0.1 | 1.6 |
| `branch` | a bough with leaves | 3.2 | 0.75 | 0.22 | 0.15 | 4.0 |
| `vine` | something hanging free | 10 | 0.65 | 0.14 | 0.3 | 2.5 |
| `ivy` | foliage held along its length (climbers on a wall, a hedge) | 0 | | | | 3.6 |
| `field` | a field of grass or grain, seen as a surface | 0 | | | | 2.0, lean [10, 3] |
| `cloth` | skirts, curtains, laundry | 10 | 0.9 | 0.35 | 0.3 | ripple 7 |
| `flag` | flags, ribbons, scarves | 14 | 1.2 | 0.25 | 0.3 | ripple 10 |
| `hair` | hair, manes, fringes | 7 | 1.0 | 0.32 | 0.3 | ripple 3 |
| `tail` | tails (drive it with a channel) | 12 | 0.8 | 0.4 | 0.3 | |
| `hang` | a rigid thing on a string (prefer `swing`) | 12 | 0.8 | 0.08 | 0.1 | |

- `bend`: degrees at the tip under a full gust. **Tip travel is bend times length**, so a 400 px tree at 2 degrees already moves 14 px. Stiff and long means small numbers. If a thing looks like rubber, bend is too high.
- `freq`: its own sway rate (Hz). Big and heavy is slow (a tree 0.3 to 0.5), small and light is quick (a flower 1 to 2).
- `damp`: 0.1 keeps swinging after the gust, 0.5 stops at once.
- `lag`: seconds by which the tip trails the root. Soft things (cloth, hair, hanging vines) have it; wood barely does.
- `flutter`: px of local trembling while a gust is on it. It is a smooth field, so neighbouring leaves move together and brushwork is not crumpled. `flutterScale` (default 44 px) is the size of a clump that moves as one.
- `ripple`, `ripples`, `rippleSpeed`: a wave travelling from root to tip (px at the tip, number of waves along the length, speed). This is what makes cloth read as cloth.
- `power`: larger keeps the base stiffer.
- `tip`: with a single root, gives the direction and length explicitly.
- `weight`: `"geo"` (default with several roots: distance walked through the paint), `"axis"` (along root-to-tip), `"radial"`, `"none"` (no bending, flutter only).
- `oneSided` (default true): whatever lies behind the root, such as a pot or the flare of a trunk, stays put. `stem` is how many px beyond the root the motion takes to start.
- `lean` (with `weight: "none"`): px that each bit leans downwind under a full gust, as `[near, far]` (bottom of the layer to its top). Each bit reads the wind where it stands, so a gust is seen crossing a field as a moving band.
- `painted`: the wind strength at which the painted pose is the true pose (default 0: painted in calm). With `"painted": 0.8` the thing is as painted while a gust of 0.8 blows, sinks back toward hanging when the air drops, and lifts further in a stronger gust. Use with `wind.opening` of the same strength.
- `within` (an outline `[[x,y], ...]`) and `feather` (px, default 14): act only inside this outline, fading out across its edge. This is how one layer carries several different motions: a figure cut as one piece can have a skirt, a scarf and a plume each swaying from its own root, while the body between them stays put. List one `sway` per part.
- `drive`: instead of wind, follow a channel (value in degrees), e.g. a tail that wags when the creature is pleased.
- A root may be an object to override per root: `{"at": [x,y], "bend": 2, "freq": 0.6, "depth": 0.4}`.

Also in `motion`:

- `{"type": "billow", "pins": [[x1,y1],[x2,y2]], "amount": 14, "wave": 4, "within": [[x,y], ...]}`: cloth held at two or more points and free between them: a canopy held overhead, a sail, a banner, a hammock, washing on a line. It does not swing from a root; it fills. Nothing moves at the pins; the belly between them swells by `amount` px under a full gust (toward `out`, by default downwind and a little up) while a wave of `wave` px runs from the first pin to the last. `painted` works as for `sway` (the wind strength at which the painted fullness is true). Use `within` to keep it to the cloth when the layer holds more than the cloth.

- `{"type": "breathe", "at": [x,y], "radius": [rx,ry], "amount": 0.02, "lift": 1.5, "ch": "breath"}`: a chest or body that swells from its centre. Needs a channel (an `osc`).
- `{"type": "undulate", "head": [x,y], "tail": [x,y], "amp": 8, "waves": 0.9, "stiff": 0.25, "beat": 1.6}`: a body wave from head to tail; used with `swim`, or alone for an eel, a snake, a streamer (`"ch"` supplies effort 0..1 when there is no `swim`).

## 5. Living things: channels and warps

A **channel** is a named number that says what something is doing right now: how far the head is tilted, how closed the eyes are. Channels are where a creature's behaviour lives. **Warps** bend a small region inside a layer according to channels. Keeping features in one layer avoids separate layer-depth drift, but stacked warps can still distort relationships, expose edge samples or leave subpixel artifacts. Check the actual composed motion, including closure of small features.

```json
"channels": {
  "notice": { "type": "pointer", "at": [462, 650], "radius": 420, "amp": 3.5 },
  "tilt":   { "type": "wander", "range": [-4.5, 4.5], "every": [3.5, 8], "home": 0.3, "plus": "notice" },
  "blink":  { "type": "blink", "every": [2.4, 6.5], "with": "tilt" },
  "earL":   { "type": "follow", "of": "tilt", "gain": 1.5, "freq": 2.1, "damp": 0.22 },
  "breath": { "type": "osc", "period": [2.6, 3.4] }
}
```

| Type | What it does | Main fields |
|---|---|---|
| `wander` | goes somewhere, settles, stays, goes again; often returns home. For head tilts, glances, shifting weight. | `range`, `every`, `home` (chance of returning to `rest`, default 0.35), `freq`, `damp` |
| `blink` | 0 open to 1 closed: quick down, brief hold, slower up; sometimes double. | `every` (default [2.5, 7]), `with` (blink when that channel starts a move, as real eyes do on a gaze shift), `chance`, `double`, `close`, `hold`, `open` |
| `pulse` | one brief action: a twitch, a flick, a tap. | `every`, `amp`, `attack`, `hold`, `release`, `repeat` ([1,3] taps), `gap`, `sign: "random"`; or `when: {"wind": 0.5, "at": [x,y]}` to fire when a gust reaches that place (`delay`, `refractory`) |
| `follow` | trails another channel with weight: late, overshooting, settling. For ears after a head, a tassel after a hat. | `of`, `gain`, `freq`, `damp`, `delay`, `plus` |
| `osc` | breathing: in a little faster than out, period re-drawn each breath. | `period`, `amp`, `inhale` |
| `wind` | the wind at a place, felt through a spring. | `at`, `gain`, `freq`, `damp`, `axis` (`"x"`, `"y"`, `"abs"`) |
| `pointer` | notices the visitor: -1..1 toward the pointer, 0 when none. | `at`, `radius`, `amp`, `axis` |
| `noise` | slow aimless variation. `rate` is random knots per second, not Hz: a smooth noise at rate 1.3 undulates mostly below 0.7 Hz, so a flicker needs a rate several times the frequency you want. Measure with `capture.mjs --channels`. | `rate`, `amp` |
| `const` | a fixed `value`. | |

Any channel may add others (`"plus": "name"` or a list) and be scaled by one (`"times": "name"`). Define a channel before the ones that refer to it.

A `pulse` or a `blink` can also answer the visitor: `"on": "tap"` fires it when the picture is touched, `"on": "approach", "at": [x, y], "radius": 200` when the pointer comes near that point. With `on` and no `every`, a pulse happens only then. From a hook, `world.ch.fire("name")` does the same.

Mind the units when wiring a channel to something. An `osc` runs 0 to `amp` (default 1). A warp's `scale` takes the channel's value as the fraction itself, so use `{"ch": "breath", "gain": 0.014}` for a chest that swells 1.4%. The `breathe` deformer multiplies its own `amount` by the channel, so give it the plain 0-to-1 channel. Angles (`angle`, a `sway` with `drive`) take the channel's value as degrees.

Warps (up to six active per layer; a seventh active warp is an explicit failure and original-image fallback, not silently omitted):

```json
"warps": [
  { "type": "pose", "pivot": [462, 690], "region": { "c": [455, 630], "r": [70, 90], "core": 0.6 }, "angle": "tilt" },
  { "type": "pose", "pivot": [444, 628], "region": { "c": [426, 598], "r": [30, 46], "rot": -30 }, "angle": "earL" },
  { "type": "blink", "at": [447, 646], "r": [5, 7], "mask": "ellipse" }
]
```

- `pose` turns, shifts or scales a region about a `pivot`. `region` is an ellipse (`c` centre, `r` radii, `rot` its tilt, `core` the inner fraction that moves fully; the rest eases to nothing at the rim). `angle`, `x`, `y`, `scale` each take a number, a channel name, or `{"ch": name, "gain": g, "add": a}`. List outer before inner: head first, then the ears and eyes on it, so features ride the head.
- `blink` compresses the eye's original painted marks inside a bounded source domain. It reconstructs the underlying surface only inside that domain and retains the surrounding source pixels; it does not stretch cheek or forehead texture into the eye. `at` is the centre, `r` the half-width and half-height, `rot` the eye-axis rotation, `line` (-1..1) the meeting position, and `closed` the channel (default `"blink"`). A required `mask: [[x,y], ...]` in picture coordinates declares the original eye-paint ownership; `mask: "ellipse"` is an explicit alternative only when the entire rotated ellipse is the intended painted mark. Do not include surrounding skin merely to get a larger region. The agent authors this from the picture; it is not an extra user input. The mask must include its soft source edge, have an observed one-pixel surrounding surface, and stay within the sampling domain. Missing ownership, overlapping eye domains, an unconverged local background reconstruction, and unavailable shader derivatives fail explicitly to the original image. Earlier configurations without `mask` must be inspected and migrated; adding `"ellipse"` mechanically would defeat the ownership contract. This is a painted-mark operation, not a general anatomical eyelid model. Check whether the resulting closed state fits this drawing.

Validate the local operation against an otherwise identical pose, camera and time with `closed=0`: pixels outside its declared source domain and necessary sampling footprint must not change. Test fractional closure, full closure, rotation and target-size sampling, and verify that the original eye mark remains coherent rather than flickering into a dot. The operation uses the head's mesh and depth; assigning a shared channel alone does not establish that inheritance.

Place transition regions where the material and connections can support them. Choose amplitudes from the image and displayed size, not universal angle defaults. Where surroundings would be carried into the deformation, first isolate the correct subject surface and prepare the reveal plate.

Head motion must carry its features consistently. Anatomically or stylistically justified local motion such as a blink remains possible; choose its support and neighbourhood from the drawing and validate the composed result. Do not forbid every local feature action or infer that sharing a channel guarantees attachment.

## 6. Whole-layer motion

`"rigid"` moves the layer as a piece.

- **`swing`**: a single-pivot rigid approximation where the depicted support permits it. `{"type": "swing", "pivot": [x,y], "period": 3.2, "amp": 3}` swings on its own, its reach varying a little, its period constant as a pendulum's is. `"drive": "wind"` makes it hang still until the air moves it (`amp` is then degrees per unit wind, `damp` small so it keeps swinging). The pivot may lie outside the picture.
- **`bob`**: something afloat. `{"type": "bob", "x": 2, "y": 3, "rot": 2, "rate": 0.25}`.
- **`pose`**: driven by channels. `{"type": "pose", "at": [x,y], "angle": "ch", "x": "ch", "y": "ch", "scale": "ch"}`.
- **`swim`**: a fish. `{"type": "swim", "head": [x,y], "tail": [x,y], "region": [x0,y0,x1,y1], "speed": 0.55}`. It hovers where it was painted, then swims in bursts and glides, turns before the edge of `region`, and hovers again. `speed` is body lengths per second; `first`, `cruise`, `hover` are time ranges; `turn` is degrees per second. Pair it with an `undulate` deformer so the body works as it goes. The plate beneath must be clean wherever it can swim.

## 7. Loose marks: `sprites`

A layer of small separate marks (petals, leaves, snow, motes, birds, butterflies) becomes a set of free pieces. Cut them into one layer; the player splits it into pieces by itself.

```json
{ "id": "petals", "sprites": { "kind": "fall", "copies": 3, "fall": 0.035, "carry": 0.26,
                               "depth": [0.12, 0.55], "ground": 1010 } }
```

- `kind: "fall"`: each piece rests where it was painted until its moment (`release` range, or a gust above `threshold`), then drifts down, carried by the wind, rocking. With `ground` (a y value), pieces below that line are lying down: a gust slides them along, and falling pieces land and lie. Pieces that leave the picture come back in from upwind or above.
- `copies`: extra pieces made from the painted ones, entering over time. They are the picture's own marks, not stock shapes.
- `fall` (fraction of height per second), `carry` (fraction of width per second per unit wind), `rock`, `spin`, `depth` range (nearer pieces are drawn larger), `region`, `min` (ignore specks below this area).
- `kind: "flit"`: a butterfly or small bird. It flutters about its painted spot (`roam` as a fraction of width), settles, and goes again; `beat` is wing-beats per second.

Before releasing marks, decide what each mark is:

- **Object or state.** A comma with a fading tail, speed lines or a blurred trail depict the object *moving*; in place they already carry the motion. Moved as a piece, such a mark reads as slowly travelling blur. Release marks that depict the object's body, or take the form from a resting instance of the same element in the picture; leave state marks painted.
- **Pigment, not mixture.** In transparent media (watercolour, ink, glazes) the observed colour is pigment times ground. A mark that leaves its ground needs its pigment colour: estimate transmittance against the original ground, or match a same-kind element the painter laid on a neutral ground. The cut's ordinary foreground solve assumes opaque-over compositing, so where alpha is near 1 it returns the mixture: a petal painted over a blue wall stays bluish on every other ground. Use the cut's `pigment` option (cutting.md): `glaze` for transparent paint (multiply), `body` for paint that also covers, with its colour read where the same paint hides its ground.
- **Light where it goes.** A released mark keeps the light of where it was painted. When it crosses light and shade, give the scene a `lightMap` and the layer `relight` (section 9). A multiply glaze needs neither: it takes the light of whatever it darkens.
- **One kind of thing.** Flying, landed and copied pieces agree in colour, form and scale, and their source and destination agree with the picture.
- **Carry nothing of the ground.** A sprite crosses other grounds, so it gets no soft rim sampled from where it was painted and no neighbouring lines.
- **Motion from material.** Thin flakes tumble, side-slip and change projected width: `tumble` (0..0.9, how far the seen width narrows as a piece turns over), `flip` (turns per second at falling speed, default 0.7) and `windSpin` (radians of spin per unit of wind, default 2.5; small or 0 for streaks that should keep pointing along the wind) set this for the `fall` kind; `spin` 0 now really stops the rocking spin. Before adding a constraint to suppress a symptom (for example damping spin because a long piece twirls like a stick), trace the symptom upstream: usually the form is wrong. Do not let the constraint turn the motion into rigid translation.
- **Copies are bounded by distinct source shapes.** Judge how many identical shapes are visible at once and how often they recur; vary scale, flip, phase and depth; with few source shapes, release fewer pieces.

Then inspect every released piece on its own (checking.md, 释放元素逐件).

## 8. Flowing paint, ripples

For edges intentionally blended into the ground, preserve that painted continuity instead of cutting a new hard outline. An inseparable region may stay together for flow or light change. This is a modelling choice, not an automatic success: verify the whole region and its boundary, and still extract clear outlines completely where the source supports them.

- `"flow": {"dir": [1, 0], "speed": 6, "reach": 18, "box": [x0,y0,x1,y1]}` makes the layer's paint drift steadily along `dir` without ever running out (two copies of the paint cross-fade as they travel). For clouds, mist, smoke, dust in the air, slow water, where strokes already run along the flow. `reach` (px) is how far paint travels before it is renewed: choose it by actual stroke width, direction and displayed sampling; this method can blur or duplicate marks even at small travel. `box` confines it softly. `"gust": 1.5` makes it run faster while a gust is on it (air as a medium: dust and mist stream with the wind), sampled at `at` or the layer's centre.
- Flow across the grain of the strokes looks wrong. When directions differ from place to place (a swirling sky, a stream bending round a rock), describe them with a few arrows at the top of the scene and set `"map": true` in the layer's `flow`:

  ```json
  "flowMap": { "arrows": [[120, 80, 260, 70], [400, 140, 470, 210], [600, 90, 520, 40]],
               "areas": [[[0, 0], [1024, 0], [1024, 330], [0, 300]]], "feather": 24 }
  ```

  Each arrow `[x1, y1, x2, y2]` says "paint here drifts this way"; directions are blended between arrows; `areas` (optional outlines) limit where anything flows. `bundle` builds the map and writes `out/flow-preview.jpg`, the field drawn over the picture, so you can check that it follows the strokes.
- `"ripples": {"every": [3, 8], "at": [[x,y], ...], "speed": 46, "width": 9, "life": 4.5, "shine": 0.18}` sends rings across the layer from the given points (or anywhere in it), bending the paint as they pass with a faint highlight on the crest. Set `"tap": "ripple"` at the top of the scene and a touch makes a ring too.

## 9. Light

```json
"light": {
  "shade": { "amount": 0.3, "scale": 760, "speed": 0.05, "cover": [0.42, 0.62], "tint": [0.74, 0.77, 0.88],
             "horizon": 310, "near": 760 },
  "glows": [ { "at": [612, 540], "radius": 90, "color": [1, 0.8, 0.5], "power": "lamp", "on": "tram" } ],
  "exposure": "dusk", "flash": "bolt"
}
```

- **`shade`**: soft cloud-shadow drifting across. `amount` is how dark, `tint` its colour (cool for daylight shade), `scale` the size of a shadow, `speed` its travel (pattern-sizes per second), `cover` the range the sky wanders between (lower means more shadow). With `horizon` and `near` (two y values on the ground) the shadows lie in perspective: small and slow far away, large and fast up close. Without them the pattern is flat, right for a wall seen head-on or for dappled light.
  Per layer: `"light": {"shade": 0.25}` (how much it takes: indoors little, sky none), `"upright": true, "foot": y` (a standing thing takes the shade that falls at its foot, all of it at once).
- **A light or a shadow that passes.** Give a glow a `sweep` and it travels across now and then instead of sitting still, swelling in and fading out as it goes: `{"radius": 260, "color": [0.55, 0.55, 0.62], "mode": "multiply", "power": 0.5, "sweep": {"from": [-200, 500], "to": [1000, 520], "every": [25, 50], "duration": [5, 8], "first": [0.5, 1.5], "visitor": true}}` is someone walking between the lamp and the picture. With `"visitor": true` a quick sweep of the pointer brings one too. The same thing in `screen` mode is headlights crossing a wall at night, or a torch.
- **`lightMap` and `relight`**: the light on moving things across the picture. `"lightMap": {"base": 1, "feather": 40, "zones": [{"poly": [[x, y], ...], "light": 0.6}, {"rect": [x0, y0, x1, y1], "light": [0.7, 0.72, 0.86]}], "auto": {"strength": 1, "blur": 40}}`, or a path (beside the scene) to your own RGB map. It is the light *where the moving thing is*, read from the picture: a petal crossing in front of a dark window is still in the sun, so never derive it from the darkness of the ground alone. Colour the zones (cool shade, warm sun) when the picture does. `auto` multiplies in large-scale luminance and is valid only on a ground of one local colour, such as snow or a plain wall. A layer with `"relight": true` (or 0..1) multiplies its paint by light-here / light-where-painted; at rest the factor is exactly 1, so the first frame is unchanged. `bundle` writes `out/light-preview.jpg` (the picture darkened by the map, zones outlined). A fixed layer or a multiply glaze cannot relight.
- **`glows`** (up to four): a light inside the picture. `power` is a number or a channel (a slow `noise` for a lamp, a `pulse` for a flicker). `mode: "multiply"` makes it a pool of shadow instead. `on` ties it to a layer so it moves with that layer. Per layer `"light": {"glow": 0}` shields a layer from it.
- **`exposure`**: a channel or number scaling the whole light (a lamp switched off, dusk falling). `warmth`: a channel shifting warmer or cooler. `flash`: a channel lifting everything toward white for lightning (`flashColor`); choose a visible pattern only when required by the picture. This channel affects the whole picture at once; per layer `"light": {"flash": 0.3}` lets a layer take less of it. For a flash that belongs to one place (the bolt itself, a window, a muzzle), use a `screen` glow there whose `power` is a `pulse` with `repeat`. Do not add rapid repeated flashes as an ambient default; respect reduced motion and verify any flashing interaction separately rather than treating a numeric preset as a safety guarantee.

Light changes preserve geometry but can still flatten paint, alter material or violate fixed photographic reflections. Use them where the painted lighting supports them; they may be sufficient for a particular intention, but are not a default replacement for requested subject motion.

## 10. Families: parent and attach

`"parent": "id"` composes a child with its parent's whole-layer motion and inherits parent depth. Evaluation is topological while paint order stays as listed. For a deforming parent, explicit `"attach": [x,y]` uses a nearest-mesh-point position and a local orientation estimate; this is approximate, not a joint, rope-length or closed-loop solver. Check contact error at the target scale. Parent texture warps, flow, sprites and bulge cannot carry child attachment in this runtime; unsupported combinations fail explicitly. Do not assign a contradictory independent depth to a child.

Choose one-layer warps or child layers by the required relative motion and occlusion. Both require validation; there is no universal preference that makes body relationships automatically correct.

## 11. Your own code: `hooks`

Point `"hooks": "hooks.js"` at a file containing one object:

```js
({
  setup(world, LP) { /* after build or rewind; must be repeat-safe */ },
  tick(world, t, dt, LP) { /* every step: world.ch.v, world.wind.at(x, y), world.byId.name ... */ },
  poke(world, x, y, LP) { /* the visitor touched the picture here */ }
})
```

`setup` runs each time the scene is built, which includes every rewind made by the capture tool, so it must be safe to run again from a clean state. `world` holds `size`, `time`, `rng` (seeded), `wind`, `light`, `camera`, `ch` (channels: `world.ch.v.name` to read, `world.ch.get(ref)`), `layers`, `byId`, `pointer`, `ripple(x, y, strength)`. A layer exposes `rest` and `pos` (vertex arrays), `xf` (its 3x3 transform, set with `LP.setXf(layer, pivotX, pivotY, angle, dx, dy, scale)`), `motion` (push an object with `step(t, dt)` and `apply(pos, t)` to add a deformer of your own). `LP` has the helpers: `noise1`, `noise2`, `wander`, `Spring`, `smooth`, `clamp`.

Inside the player, for hooks that need to reach further: `world.scene` is the scene object (extra keys of your own are left alone and can carry settings for your hook); `LP.D2R` converts degrees; each `sway` in `layer.motion` has `o` (its options: `bend`, `flutter`, `ripple`, ...), `w` (per-vertex weight, 0 fixed to 1 free: overwrite it to shape exactly what moves), `label` (which root each vertex belongs to) and `roots` (each with `at`, `gain` in radians per unit wind, its spring `sp`). `layer.n` is the vertex count; vertex `i` is at `rest[2*i], rest[2*i+1]`. `layer.carry(x, y)` gives where the paint at a rest point is now; it follows deformers and whole-layer motion but not warps, so a part that must ride a moving head should be moved by a deformer, not hung on a warp.

Keep custom motion on the same wind, light and channels where you can, so it belongs to the same weather as everything else. Use `world.rng`, not `Math.random`, so that frames stay reproducible for checking.

## 12. Original image, first frame and rest

The working original is a lossless `picture.png`. Pause/reset, reduced-motion and error fallback show that image. `seek(0)` renders the layer stack and may have composition or sampling differences: it is not proof of exact rest. Verify first motion entry as well as return. A scene painted mid-event may use `wind.opening` and `painted`; treat them as modelling choices, not a guarantee of a zero-difference frame.

The player supports explicit rest windows; the interface is documented in [runtime-rest-contract.md](runtime-rest-contract.md). Follow its actual supported interface; do not guess JSON fields or infer rest from a visually quiet channel. Continuous motion does not have to be interrupted just to manufacture an idle interval.

## 13. Parameter selection

Read material, scale, support, weight and the painted moment before choosing rates and amplitudes. Examples in this reference demonstrate syntax only. Pendulum-like motion is appropriate only when the depicted support allows it; a pair of chains or coupled bodies may need constraints the current runtime does not solve. Blinks, breath and follow-through are optional, scene-dependent behaviours, not biological-frequency defaults. Preserve the leading dynamic and the user's intended space. For a genuinely quiet image or one without a single carrier, a small change can be readable when perceptible at the target display size; record the image-based reason. A clear depicted action still needs its intended expression. A smaller amplitude is not a cure for edge or structural defects. If model completion and alternative expressions have both been tried and a true conflict remains, follow step 8 of the main rules rather than inventing a new priority order.

## 14. Sprite region and fixed coverage limits

`sprites.region` checks the complete rotated/scaled/flapped quad after parent affine motion, before camera projection; sprites fade/hide when they cannot fit. It is not a screen-space face avoidance solver across different depths. Verify the moving focal subject and permitted camera envelope. Original sprites whose initial full footprint cannot fit the region are rejected rather than silently disappearing.

`fixed:true` is a restricted final overlay for opaque originals and binary-alpha masks matching original pixels at native integer rects, with lossless encoding and bundle evidence. It disables camera, light and deformation for that overlay. It does not remove duplicate source paint from lower moving layers; author that removal and inspect the reveal. Soft/transparent fixed coverage is unsupported, and scaled-edge sampling needs separate validation.
