"""On-device Apple Vision for living-picture (macOS only): `picture.py vision`.

Runs scripts/macos/lpvision.swift (compiled on demand with xcrun swiftc into
~/Library/Caches/living-picture) on the working picture and turns what Vision
reports into starting points in the skill's own vocabulary: mask parts for
plan.json, blink and head-pose warps, protect polygons and fixed-layer snippets
for lettering, saliency peaks. Vision was trained on photographs; on paintings
it is often wrong or silent. Every entry is for the agent to check by looking
(review.jpg first, then the place itself at native size), never an answer.
Nothing leaves the machine.

Coordinates: polygons meant for plan.json (outlines, snap regions, protect and
fixed-layer polygons) follow the cut tools, which rasterise with PIL (pixel x at
x). Blink entries are read by the player, which counts pixel x as the span
[x, x+1] with its centre at x+0.5, so they are written in that frame.
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from . import imgops as io
from . import tools

CACHE = os.path.join(os.path.expanduser("~"), "Library", "Caches", "living-picture")
MACOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macos")
ALL = ("subjects", "persons", "faces", "pose", "saliency", "text")
MARGIN = 1.25          # px of observed paint between a blink mask and its domain edge: a full pixel, and slack
MARGIN_MIN = 1.0       # the player's minimum: one observed pixel round the mask inside the domain
# Every file a run writes into --out; masks are the ones plan.json may point at.
OURS = re.compile(r"^(vision\.json|review\.jpg|attention\.png|objectness\.png|persons-matte\.png|"
                  r"(subject|person)-\d+\.png|\.DS_Store)$")
MASKS = re.compile(r"^(persons-matte|(subject|person)-\d+)\.png$")


class Unavailable(RuntimeError):
    """Vision cannot run here (not macOS, no Swift compiler, no writable cache); the message says what to do."""


# ------------------------------------------------------------------ the Swift helper

def _info_plist(stem):
    """The Info.plist linked into a helper. macOS names a command-line tool's caches after its bundle
    id, or after the executable when it has none: without it every build left a Core ML cache in
    ~/Library/Caches/<stem>-<hash>/. With the id "living-picture" they all go into CACHE."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict><key>CFBundleIdentifier</key><string>living-picture</string>'
            '<key>CFBundleName</key><string>%s</string></dict></plist>\n' % stem).encode("utf-8")


def swift_tool(stem, log=None):
    """Path to the compiled scripts/macos/<stem>.swift, compiling it once per source version.

    The binary is named after the hash of the source and its Info.plist and compiled under a temporary
    name, then renamed into place: parallel first runs never see a half-written file (both compile;
    the last rename wins)."""
    if sys.platform != "darwin":
        raise Unavailable("%s needs macOS (Apple's on-device frameworks); this system is %s" % (stem, sys.platform))
    src = os.path.join(MACOS, stem + ".swift")
    plist = _info_plist(stem)
    with open(src, "rb") as f:
        exe = os.path.join(CACHE, "%s-%s" % (stem, hashlib.sha256(f.read() + b"\0" + plist).hexdigest()[:12]))
    if os.access(exe, os.X_OK):
        return exe
    need = "compiling %s needs the Xcode command line tools (swiftc): run  xcode-select --install" % stem
    # Probe without the xcrun shim: on a Mac without developer tools the shim pops up an installer.
    dev = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True) if shutil.which("xcode-select") else None
    if dev is None or dev.returncode != 0 or not os.path.isdir(dev.stdout.strip()) or not shutil.which("xcrun"):
        raise Unavailable(need)
    tmp = "%s.tmp-%d-%s" % (exe, os.getpid(), uuid.uuid4().hex[:8])
    info = tmp + ".plist"
    # A sandbox often keeps ~/Library read-only: say so instead of a traceback.
    nocache = lambda e: Unavailable("cannot write the helper cache %s (%s): allow writes there, or run outside "
                                    "the sandbox" % (CACHE, e.strerror or e))
    try:
        try:
            os.makedirs(CACHE, exist_ok=True)
            with open(info, "wb") as f:
                f.write(plist)
        except OSError as e:
            raise nocache(e)
        if log:
            log("compiling %s once for this version (a few seconds)" % os.path.basename(src))
        p = subprocess.run(["xcrun", "swiftc", "-O", src, "-o", tmp, "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
                            "-Xlinker", "__info_plist", "-Xlinker", info], capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(tmp):
            err = (p.stderr or p.stdout or "").strip()
            if "unable to find utility" in err or "invalid active developer path" in err:
                raise Unavailable(need)
            raise RuntimeError("compiling %s failed:\n%s" % (src, "\n".join(err.splitlines()[-25:])))
        try:
            os.chmod(tmp, 0o755)
            os.replace(tmp, exe)
        except OSError as e:
            raise nocache(e)
    finally:
        for f in (tmp, info):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass
    return exe


def run(image, what=ALL, out=".", box=None, zoom=None, faces=None, langs=None, log=None):
    """Run lpvision and return its JSON (picture pixels, top-left origin; file names relative to `out`)."""
    exe = swift_tool("lpvision", log)
    cmd = [exe, ",".join(what), os.path.abspath(image), "--out", os.path.abspath(out)]
    if box:
        cmd += ["--box"] + ["%r" % float(v) for v in box]
    if zoom is not None:                     # 0 is a value to refuse, not "the default"
        cmd += ["--zoom", "%r" % float(zoom)]
    for b in faces or []:
        cmd += ["--face"] + ["%r" % float(v) for v in b]
    if langs:
        cmd += ["--langs", ",".join(langs)]
    ready = exe + ".text-ready"
    if "text" in what and not os.path.exists(ready) and log:
        log("first text pass of this lpvision build: macOS compiles its text model for it once (about a minute)")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError("lpvision did not finish within 15 minutes")
    try:
        res = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError("lpvision failed (exit %d): %s" % (p.returncode, (p.stderr or p.stdout).strip()[-800:]))
    if p.returncode != 0:
        raise RuntimeError("lpvision: %s" % res.get("error", "exit %d" % p.returncode))
    if isinstance(res.get("text"), dict) and "error" not in res["text"]:
        try:                                 # the marker only decides whether to print the hint above
            open(ready, "a").close()
        except OSError:
            pass
    return res


# ------------------------------------------------------------------ small helpers

def _r(v):
    return round(float(v), 2)


def _pts(a):
    return [[_r(x), _r(y)] for x, y in a]


def _rel(path, base):
    return os.path.relpath(path, base).replace(os.sep, "/")


def _job_dir(image):
    d = os.path.dirname(os.path.abspath(image))
    return os.path.dirname(d) if os.path.basename(d) == "work" else d


def _outline(mask, eps=1.5, cap=120, at=(0, 0)):
    """Outer outline of the largest blob, simplified to at most about `cap` points, offset by `at`."""
    pts = tools._trace(mask)
    if len(pts) < 3:
        return []
    if len(pts) > 1500:                     # keep the simplifier's recursion shallow
        pts = pts[::int(math.ceil(len(pts) / 1500.0))]
    s = tools._simplify(pts, eps)
    while len(s) > cap and eps < 40:
        eps *= 1.6
        s = tools._simplify(pts, eps)
    return [[float(x + at[0]), float(y + at[1])] for x, y in s]


def _pieces(hard, cap=12):
    """The separate pieces of a mask (8-connected), largest first, each with its outline (for look
    --poly) and a rough region for snap. A Vision mask can hold several things (two figures in one
    person instance): one snap part per piece, so none is dropped. Specks are only counted."""
    lab, n = ndi.label(hard, structure=np.ones((3, 3)))
    if n == 0:
        return [], None
    sizes = np.asarray(ndi.sum(hard, lab, index=np.arange(1, n + 1)))
    order = [int(i) for i in np.argsort(-sizes, kind="stable")]
    big = [i for i in order if sizes[i] >= max(16.0, 0.002 * sizes.sum())][:cap]
    boxes = ndi.find_objects(lab)
    out = []
    for i in big:
        sl = boxes[i]
        piece = lab[sl] == i + 1
        at = (sl[1].start, sl[0].start)
        e = {"bbox": [sl[1].start, sl[0].start, sl[1].stop, sl[0].stop], "px": int(sizes[i]),
             "outline": _outline(piece, at=at)}
        # Vision's masks are upsampled from a coarse analysis: soft and a few px loose on crisp
        # paint. The outline lets snap find the painted edge instead (or clips the mask part).
        rough = _outline(piece, eps=2.0, cap=40, at=at)
        if len(rough) >= 3:
            e["snap_part"] = {"mode": "snap", "poly": rough}
        out.append(e)
    rest = [i for i in order if i not in big]
    specks = {"count": len(rest), "px": int(sizes[rest].sum())} if rest else None
    return out, specks


def _load_mask(path):
    with Image.open(path) as im:
        return np.asarray(im.convert("L"), np.float32) / 255.0


# ------------------------------------------------------------------ eyes -> blink warps

def _mark(rgb, seed, R):
    """The painted mark around `seed`: pixels that differ from the surrounding surface.

    The surface colour is the median of a ring 1.5-2.3 R out; strong paint near the seed starts the
    mark, which then takes in its soft edge (a lower threshold), with holes (an eye white) filled."""
    H, W = rgb.shape[:2]
    cx, cy = seed
    pad = int(math.ceil(2.4 * R)) + 2
    x0, y0 = max(0, int(cx) - pad), max(0, int(cy) - pad)
    x1, y1 = min(W, int(cx) + pad + 1), min(H, int(cy) + pad + 1)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    lab = io.rgb_to_lab(rgb[y0:y1, x0:x1])
    yy, xx = np.mgrid[y0:y1, x0:x1]
    d = np.hypot(xx - cx, yy - cy)
    ring = (d > 1.5 * R) & (d < 2.3 * R)
    if ring.sum() < 12:
        return None
    ref = np.median(lab[ring], axis=0)
    diff = np.sqrt(((lab - ref) ** 2).sum(-1))
    spread = float(np.percentile(diff[ring], 75))          # how busy the surrounding surface is
    hi, lo = max(0.10, 2.5 * spread), max(0.05, 1.5 * spread)
    cand = (diff > lo) & (d <= 1.3 * R)
    # strokes a pixel or two apart (lashes, a lid line) belong to the same eye
    comp, n = ndi.label(ndi.binary_dilation(cand, iterations=1), structure=np.ones((3, 3)))
    comp = np.where(cand, comp, 0)
    if n == 0:
        return None
    # the piece with the most strong paint, discounted by its distance from the seed (a highlight
    # beside a dark eye loses to the eye; a seed a few pixels off still finds it)
    idx = np.arange(1, n + 1)
    mass = ndi.sum(np.where(diff > hi, diff, 0), comp, idx)
    near = ndi.minimum(np.where(comp > 0, d, 1e9), comp, idx)
    score = mass / (1 + (np.asarray(near) / (0.5 * R)) ** 2)
    if not len(score) or score.max() <= 0:
        return None
    m = ndi.binary_fill_holes(comp == idx[int(np.argmax(score))])
    full = np.zeros((H, W), bool)
    full[y0:y1, x0:x1] = m
    return {"mask": full, "spills": bool((m & (d > 1.2 * R)).any())}


def _find_eye(rgb, seed, R):
    """Re-centre on the mark and resize the search to it until it settles."""
    found = None
    for _ in range(4):
        f = _mark(rgb, seed, R)
        if f is None:
            break
        found = f
        ys, xs = np.nonzero(f["mask"])
        if len(xs) < 4:
            return None
        nseed = (float(xs.mean()), float(ys.mean()))
        half = 0.5 * max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)
        nR = max(3.0, 1.3 * half)
        settled = abs(nR - R) < 0.1 * R and math.hypot(nseed[0] - seed[0], nseed[1] - seed[1]) < 1.0
        seed, R = nseed, nR
        if settled:
            break
    if found is None or found["mask"].sum() < 4:
        return None
    return found


def _domain(at, r, rot):
    a = math.radians(rot)
    co, si = math.cos(a), math.sin(a)
    corners = [[at[0] + co * px - si * py, at[1] + si * px + co * py] for px, py in
               ((-r[0], -r[1]), (r[0], -r[1]), (r[0], r[1]), (-r[0], r[1]))]
    return corners, [(co, si), (-si, co)]


def _overlap(a, b):
    """The runtime's test: two blink domains must be separated by a two-sample halo along some axis."""
    for ax in a[1] + b[1]:
        p = [c[0] * ax[0] + c[1] * ax[1] for c in a[0]]
        q = [c[0] * ax[0] + c[1] * ax[1] for c in b[0]]
        halo = 2 * (abs(ax[0]) + abs(ax[1]))
        if max(p) + halo <= min(q) or max(q) + halo <= min(p):
            return False
    return True


def _up(v):
    """Round up to 0.01, so a margin written to two decimals is never less than intended."""
    return math.ceil(v * 100 - 1e-6) / 100


def _crack_outline(m):
    """Outline of the 8-connected piece holding the first pixel of `m` (row-major), through the
    midpoints of the pixel edges between it and the outside, in the player's frame (pixel (x, y)
    spans [x, x+1] x [y, y+1]). It runs half-way between the centres inside and outside, 0.35 px or
    more from each, so the player's centre test takes exactly these pixels. Runs are merged."""
    p = np.pad(m, 1)
    ys, xs = np.nonzero(p)
    y0 = int(ys.min())
    x0 = int(xs[ys == y0].min())
    x, y, dx, dy = x0, y0, 1, 0                # along that pixel's top edge, the piece on the right
    mids = []
    for _ in range(4 * p.size):
        mids.append((x + dx / 2.0 - 1, y + dy / 2.0 - 1))
        x, y = x + dx, y + dy
        if (x, y) == (x0, y0):
            break
        rx, ry = -dy, dx
        left = p[y + min(0, dy) + min(0, -ry), x + min(0, dx) + min(0, -rx)]
        right = p[y + min(0, dy) + min(0, ry), x + min(0, dx) + min(0, rx)]
        # a diagonal neighbour belongs to the piece too (8-connected, as tools._trace counts it)
        dx, dy = (dy, -dx) if left else (dx, dy) if right else (-dy, dx)
    n = len(mids)
    return [q for i, q in enumerate(mids) if (mids[i - 1][0] - q[0]) * (mids[(i + 1) % n][1] - q[1])
            != (mids[i - 1][1] - q[1]) * (mids[(i + 1) % n][0] - q[0])]


def _player_takes(poly, X, Y):
    """The player's even-odd test of a blink mask (03-layers.js, blinkPlateData) at points X, Y."""
    inside = np.zeros(np.shape(X), bool)
    for i in range(len(poly)):
        (ax, ay), (bx, by) = poly[i], poly[i - 1]
        with np.errstate(divide="ignore", invalid="ignore"):
            inside ^= ((ay > Y) != (by > Y)) & (X < (bx - ax) * (Y - ay) / (by - ay) + ax)
    return inside


def _blink(mask, rot, grow=1, margin=MARGIN):
    """Blink warp for a painted mark, in the player's frame. The mask polygon takes exactly the mark
    and `grow` pixels round it (its soft edge); r leaves `margin` px of observed paint round the
    polygon; `rot` follows the eye axis. Returns the warp, coverage checks and the polygon's half extents."""
    grown = ndi.binary_dilation(mask, iterations=grow) if grow else mask
    lab, n = ndi.label(grown, structure=np.ones((3, 3)))
    if n > 1:                                   # pieces more than a pixel apart: outline the largest
        grown = lab == 1 + int(np.argmax(ndi.sum(grown, lab, index=np.arange(1, n + 1))))
    ys, xs = np.nonzero(grown)
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    poly = [(x + x0, y + y0) for x, y in _crack_outline(grown[y0:y1, x0:x1])]
    rot = _r(rot)
    a = math.radians(rot)
    co, si = math.cos(a), math.sin(a)
    P = np.asarray(poly, float)
    qx, qy = co * P[:, 0] + si * P[:, 1], -si * P[:, 0] + co * P[:, 1]
    mx, my = (qx.max() + qx.min()) / 2, (qy.max() + qy.min()) / 2
    at = [_r(co * mx - si * my), _r(si * mx + co * my)]
    # half extents about the centre as written, so the margin still holds after rounding
    half = (float(np.abs(qx - (co * at[0] + si * at[1])).max()), float(np.abs(qy - (co * at[1] - si * at[0])).max()))
    warp = {"type": "blink", "at": at, "r": [_up(half[0] + margin), _up(half[1] + margin)]}
    if rot:
        warp["rot"] = rot
    warp["mask"] = _pts(poly)
    # what the player will take as eye paint, by its own test (pixel centres at +0.5)
    H, W = mask.shape
    bx0, by0, bx1, by1 = max(0, x0 - 2), max(0, y0 - 2), min(W, x1 + 2), min(H, y1 + 2)
    yy, xx = np.mgrid[by0:by1, bx0:bx1]
    took = np.zeros(mask.shape, bool)
    took[by0:by1, bx0:bx1] = _player_takes(poly, xx + 0.5, yy + 0.5)
    checks = {"mask_px": int(took.sum())}
    left = int((mask & ~took).sum())
    if left:
        checks["mark_left"] = "%d px of the mark are outside the mask polygon" % left
    return warp, checks, half


def _domain_checks(warp, half, size):
    """The player's own tests on a blink domain, and the margin actually left round the mask."""
    corners, _ = _domain(warp["at"], warp["r"], warp.get("rot", 0))
    W, H = size
    c = {"margin_px": _r(min(warp["r"][0] - half[0], warp["r"][1] - half[1])),
         "inside_picture": all(1 <= x <= W - 1 and 1 <= y <= H - 1 for x, y in corners),
         "axis_samples": int(math.ceil(2 * max(warp["r"])))}
    if c["axis_samples"] > 256:
        c["too_large"] = "the runtime samples at most 256 px across a blink axis"
    return c


def _axis_deg(mask):
    """The corner-to-corner axis of an elongated mark, or None when it is roundish or tiny.

    A wide mark's long axis is that axis; a tall oval (a cartoon eye) closes along its length,
    so its axis is the short one."""
    ys, xs = np.nonzero(mask)
    if len(xs) < 20:
        return None
    ev, evec = np.linalg.eigh(np.cov(np.stack([xs, ys]).astype(float)))
    if ev[0] <= 0 or ev[1] / ev[0] < 1.96:          # axis ratio below 1.4
        return None
    a = (math.degrees(math.atan2(evec[1, 1], evec[0, 1])) + 90) % 180 - 90
    return a - 90 if a > 45 else a + 90 if a < -45 else a


def _norm_deg(a):
    a = (a + 90) % 180 - 90                          # an eye axis has no direction
    return 0.0 if abs(a) < 4 else a                  # keep near-level eyes axis-aligned, like measure --blobs


def _eyes(rgb, seeds, size):
    """seeds: dicts with id, source, at, R, tilt (deg or None), contour (optional). Returns eye entries."""
    out, taken, halves, marks = [], [], {}, {}
    for s in seeds:
        if any(math.hypot(s["at"][0] - t[0], s["at"][1] - t[1]) < max(3.0, 0.5 * t[2]) for t in taken):
            continue                                 # the same eye from a weaker source
        taken.append((s["at"][0], s["at"][1], s["R"]))
        e = {"id": s["id"], "source": s["source"], "seed": _pts([s["at"]])[0]}
        if s.get("contour"):
            e["landmark_contour"] = _pts(s["contour"])
        f = _find_eye(rgb, s["at"], s["R"])
        if f is None:
            e["found"] = "no painted mark here: place the eye yourself (measure --blobs)"
            out.append(e)
            continue
        rot = s.get("tilt")
        if rot is None:
            rot = _axis_deg(f["mask"])
        e["blink"], e["checks"], half = _blink(f["mask"], _norm_deg(rot or 0.0))
        halves[len(out)], marks[len(out)] = half, (f["mask"], _norm_deg(rot or 0.0))
        e["found"] = "painted mark"
        e["area_px"] = int(f["mask"].sum())
        if f["spills"]:
            e["checks"]["spills"] = "the mark runs into neighbouring paint (brow, hair, outline): trim the mask by hand"
        if s.get("contour"):
            # Landmarks fitted to a stylised face can sit on the nose or a brow: the mark found there
            # must at least be near the landmark eye and of a comparable size.
            c = np.asarray(s["contour"], float)
            ys, xs = np.nonzero(f["mask"])
            lw, mw = max(2.0, float(np.ptp(c[:, 0]))), float(xs.max() - xs.min() + 1)
            if (math.hypot(xs.mean() - c[:, 0].mean(), ys.mean() - c[:, 1].mean()) > max(3.0, 0.8 * lw)
                    or not 0.3 <= mw / lw <= 3.0):
                e["checks"]["off_landmarks"] = "the mark found is not where or the size the landmarks put the eye"
        out.append(e)
    # Overlapping domains fail in the player. Every eye in a clash drops to the 1 px minimum, once.
    # Marks only a few px apart can still clash: then the eye in most clashes (ties together) leaves
    # out its soft edge, once, until none clash or none is left to tighten. What still overlaps is reported.
    ix = sorted(halves)
    dom = lambda i: _domain(out[i]["blink"]["at"], out[i]["blink"]["r"], out[i]["blink"].get("rot", 0))
    pairs = [(i, j) for k, i in enumerate(ix) for j in ix[k + 1:]]
    clash = lambda: {i: [j for p in pairs if i in p and _overlap(dom(p[0]), dom(p[1])) for j in p if j != i] for i in ix}
    for i, others in clash().items():
        if others:
            out[i]["blink"]["r"] = [_up(h + MARGIN_MIN) for h in halves[i]]
    tight = set()
    while True:
        now = {i: v for i, v in clash().items() if v and i not in tight}
        most = max([len(v) for v in now.values()] or [0])
        if not most:
            break
        for i, others in now.items():
            if len(others) == most:
                tight.add(i)
                kept = {k: v for k, v in out[i]["checks"].items() if k in ("spills", "off_landmarks")}
                out[i]["blink"], out[i]["checks"], halves[i] = _blink(*marks[i], grow=0, margin=MARGIN_MIN)
                out[i]["checks"].update(kept, soft_edge_left="the mask covers the painted mark only, to keep clear of "
                                        "%s: look at the closed eye for a faint ring" % ", ".join(out[j]["id"] for j in others))
    for i, j in pairs:
        if _overlap(dom(i), dom(j)):
            out[i]["checks"].setdefault("overlaps", []).append(out[j]["id"])
            out[j]["checks"].setdefault("overlaps", []).append(out[i]["id"])
    for i in ix:                            # ok: none of the checks failed; whether it is the eye, only a look can say
        c = out[i]["checks"]
        c.update(_domain_checks(out[i]["blink"], halves[i], size))
        c["ok"] = bool(c["inside_picture"] and c["margin_px"] >= MARGIN_MIN and not any(
            k in c for k in ("overlaps", "too_large", "spills", "off_landmarks", "mark_left", "soft_edge_left")))
    return out


# ------------------------------------------------------------------ heads -> pose warps

def _head_warp(c, r, rot, pivot):
    region = {"c": [_r(c[0]), _r(c[1])], "r": [_r(r[0]), _r(r[1])], "core": 0.6}
    if abs(rot) >= 2:
        region["rot"] = _r(rot)
    return {"type": "pose", "pivot": [_r(pivot[0]), _r(pivot[1])], "region": region, "angle": "tilt"}


def _face_head(face, eyes_lr, neck=None):
    x0, y0, x1, y1 = face["box"]
    w, h = x1 - x0, y1 - y0
    tilt = 0.0
    if eyes_lr:
        (lx, ly), (rx, ry) = eyes_lr
        tilt = math.degrees(math.atan2(ry - ly, rx - lx))
    a = math.radians(tilt)
    up, down = (math.sin(a), -math.cos(a)), (-math.sin(a), math.cos(a))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    contour = face.get("landmarks", {}).get("face_contour") or []
    if contour:                                     # the chin: the contour point farthest "down"
        chin = max(contour, key=lambda p: (p[0] - cx) * down[0] + (p[1] - cy) * down[1])
    else:
        chin = (cx + down[0] * h / 2, cy + down[1] * h / 2)
    if face.get("given"):            # a box drawn by hand usually holds the whole head, hair included
        drop, c, r = 0.1, (cx, cy), (0.55 * w, 0.55 * h)
    else:                            # Vision's box runs from the brows to the chin: lift and widen it for the hair
        drop, c, r = 0.25, (cx + up[0] * 0.15 * h, cy + up[1] * 0.15 * h), (0.75 * w, 0.8 * h)
    pivot = (chin[0] + down[0] * drop * h, chin[1] + down[1] * drop * h)
    source = "chin"
    if neck and math.hypot(neck[0] - pivot[0], neck[1] - pivot[1]) < 0.6 * h:
        pivot, source = neck[:2], "neck joint"
    return _head_warp(c, r, tilt, pivot), source


def _pose_head(j, minc=0.3):
    """Head from body joints when no face was found (cartoon figures)."""
    g = lambda k: j[k][:2] if k in j and j[k][2] >= minc else None
    pts = [p for p in (g("nose"), g("left_eye"), g("right_eye"), g("left_ear"), g("right_ear")) if p]
    neck = g("neck")
    if len(pts) < 2 or not neck:
        return None
    c = np.mean(pts, axis=0)
    s = max([math.hypot(c[0] - neck[0], c[1] - neck[1])] +
            [math.hypot(p[0] - q[0], p[1] - q[1]) for p in pts for q in pts])
    tilt = 0.0
    le, re = g("left_eye"), g("right_eye")
    if le and re and math.hypot(re[0] - le[0], re[1] - le[1]) > 0.15 * s:
        (lx, ly), (rx, ry) = sorted([le, re])
        tilt = math.degrees(math.atan2(ry - ly, rx - lx))
    a = math.radians(tilt)
    up = (math.sin(a), -math.cos(a))
    cc = (c[0] + up[0] * 0.2 * s, c[1] + up[1] * 0.2 * s)
    return _head_warp(cc, (0.75 * s, 0.85 * s), tilt, neck)


HUMAN_LIMBS = [("left upper arm", "left_shoulder", "left_elbow"), ("left forearm", "left_elbow", "left_wrist"),
               ("right upper arm", "right_shoulder", "right_elbow"), ("right forearm", "right_elbow", "right_wrist"),
               ("left thigh", "left_hip", "left_knee"), ("left shin", "left_knee", "left_ankle"),
               ("right thigh", "right_hip", "right_knee"), ("right shin", "right_knee", "right_ankle")]
ANIMAL_LIMBS = [("left ear", "left_ear_bottom", "left_ear_top"), ("right ear", "right_ear_bottom", "right_ear_top"),
                ("tail", "tail_top", "tail_bottom"), ("left front leg", "left_front_elbow", "left_front_paw"),
                ("right front leg", "right_front_elbow", "right_front_paw"), ("left back leg", "left_back_elbow", "left_back_paw"),
                ("right back leg", "right_back_elbow", "right_back_paw")]


def _limbs(j, table, minc=0.3):
    """Pose warps that turn a limb about its joint: pivot at the joint, an ellipse along the bone."""
    out = []
    for name, a, b in table:
        if a in j and b in j and j[a][2] >= minc and j[b][2] >= minc:
            (ax, ay), (bx, by) = j[a][:2], j[b][:2]
            n = math.hypot(bx - ax, by - ay)
            if n < 4:
                continue
            region = {"c": [_r((ax + bx) / 2), _r((ay + by) / 2)], "r": [_r(0.6 * n), _r(max(3.0, 0.25 * n))],
                      "rot": _r(math.degrees(math.atan2(by - ay, bx - ax))), "core": 0.5}
            out.append({"part": name, "warp": {"type": "pose", "pivot": [_r(ax), _r(ay)], "region": region,
                                               "angle": name.replace(" ", "_")}})
    return out


def _animal_head(j, minc=0.3):
    names = ("nose", "left_eye", "right_eye", "left_ear_top", "right_ear_top", "left_ear_middle", "right_ear_middle",
             "left_ear_bottom", "right_ear_bottom")
    pts = np.array([j[k][:2] for k in names if k in j and j[k][2] >= minc])
    neck = j.get("neck")
    if len(pts) < 3 or not neck or neck[2] < minc:
        return None
    lo, hi = pts.min(0), pts.max(0)
    c = (lo + hi) / 2
    r = np.maximum((hi - lo) / 2 * 1.3, 6)
    return _head_warp(c, r, 0.0, neck[:2])


# ------------------------------------------------------------------ text, saliency

def _grow_quad(q, m):
    q = np.asarray(q, float)
    u, v = q[1] - q[0], q[3] - q[0]
    u, v = u / (np.linalg.norm(u) or 1), v / (np.linalg.norm(v) or 1)
    return [_pts([p + su * m * u + sv * m * v])[0] for p, (su, sv) in zip(q, ((-1, -1), (1, -1), (1, 1), (-1, 1)))]


def _box_quad(b):
    return [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]]


def _covered(b, boxes):
    """Share of box b inside the union of `boxes` (approximated by the largest single overlap)."""
    area = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    best = 0.0
    for o in boxes:
        w, h = min(b[2], o[2]) - max(b[0], o[0]), min(b[3], o[3]) - max(b[1], o[1])
        if w > 0 and h > 0:
            best = max(best, w * h / area)
    return best


def _peaks(path, n=3):
    """Hot spots of a saliency heat map: the peak and the region above half its value."""
    heat = _load_mask(path)
    sm = ndi.gaussian_filter(heat, max(2.0, min(heat.shape) / 100.0))
    top = float(sm.max())
    if top <= 0.02:
        return []
    size = max(5, int(min(heat.shape) / 8)) | 1
    ys, xs = np.nonzero((sm == ndi.maximum_filter(sm, size=size)) & (sm >= 0.4 * top))
    parts, peaks = [], []
    for i in np.argsort(-sm[ys, xs]):
        y, x = ys[i], xs[i]
        comp, _ = ndi.label(sm >= 0.5 * sm[y, x])
        part = comp == comp[y, x]
        if any((part & p).sum() > 0.5 * min(part.sum(), p.sum()) for p in parts):
            continue                                    # the same hot region seen from a lower peak
        parts.append(part)
        peaks.append({"at": [int(x), int(y)], "value": _r(sm[y, x]), "box": list(io.bbox(part)),
                      "share": round(float(part.mean()), 4)})
        if len(peaks) >= n:
            break
    return peaks


# ------------------------------------------------------------------ review sheet

HUMAN_BONES = [("nose", "neck"), ("neck", "left_shoulder"), ("neck", "right_shoulder"), ("left_shoulder", "left_elbow"),
               ("left_elbow", "left_wrist"), ("right_shoulder", "right_elbow"), ("right_elbow", "right_wrist"),
               ("neck", "root"), ("root", "left_hip"), ("root", "right_hip"), ("left_hip", "left_knee"),
               ("left_knee", "left_ankle"), ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
               ("nose", "left_eye"), ("nose", "right_eye"), ("left_eye", "left_ear"), ("right_eye", "right_ear")]
ANIMAL_BONES = [("nose", "left_eye"), ("nose", "right_eye"), ("left_eye", "left_ear_bottom"),
                ("left_ear_bottom", "left_ear_middle"), ("left_ear_middle", "left_ear_top"),
                ("right_eye", "right_ear_bottom"), ("right_ear_bottom", "right_ear_middle"),
                ("right_ear_middle", "right_ear_top"), ("nose", "neck"), ("neck", "left_front_elbow"),
                ("left_front_elbow", "left_front_knee"), ("left_front_knee", "left_front_paw"),
                ("neck", "right_front_elbow"), ("right_front_elbow", "right_front_knee"),
                ("right_front_knee", "right_front_paw"), ("neck", "tail_top"), ("tail_top", "tail_middle"),
                ("tail_middle", "tail_bottom"), ("tail_top", "left_back_elbow"), ("left_back_elbow", "left_back_knee"),
                ("left_back_knee", "left_back_paw"), ("tail_top", "right_back_elbow"),
                ("right_back_elbow", "right_back_knee"), ("right_back_knee", "right_back_paw")]
TINTS = [(230, 60, 60), (60, 140, 240), (60, 200, 90), (240, 180, 40), (190, 80, 230), (40, 200, 200)]


def _ellipse_pts(c, r, rot, n=48):
    a = math.radians(rot)
    return [(c[0] + r[0] * math.cos(t) * math.cos(a) - r[1] * math.sin(t) * math.sin(a),
             c[1] + r[0] * math.cos(t) * math.sin(a) + r[1] * math.sin(t) * math.cos(a))
            for t in np.linspace(0, 2 * math.pi, n, endpoint=False)]


def _review(rgb, res, out, path):
    H, W = rgb.shape[:2]
    k = min(1.0, 1000.0 / max(W, H))
    S = lambda p: (p[0] * k, p[1] * k)
    base = rgb.copy()
    for i, e in enumerate(res.get("subjects", []) + res.get("persons", [])):
        m = _load_mask(os.path.join(out, e["file"]))[..., None]
        base = base * (1 - 0.38 * m) + np.array(TINTS[i % len(TINTS)], np.float32) / 255 * 0.38 * m
    left = Image.fromarray(io.to_u8(base)).resize((max(1, int(W * k)), max(1, int(H * k))), Image.LANCZOS)
    d = ImageDraw.Draw(left)
    fs = max(11, int(14 * max(k, 0.6)))
    for i, e in enumerate(res.get("subjects", []) + res.get("persons", [])):
        for piece in e["pieces"]:
            if len(piece["outline"]) > 2:
                q = [S(p) for p in piece["outline"]]
                d.line(q + q[:1], fill=TINTS[i % len(TINTS)], width=2)
        if e["bbox"]:
            X, Y = S(e["bbox"][:2])
            io.label(d, (X + 2, Y + 2 + i * (fs + 6)), e["id"] + (" (doubtful)" if e.get("doubtful") else ""),
                     size=fs, bg=TINTS[i % len(TINTS)])
    for t in res.get("text", []):
        q = [S(p) for p in t["protect"]["poly"]]
        d.line(q + q[:1], fill=(255, 0, 255) if not t["doubtful"] else (255, 150, 255), width=2)
        io.label(d, (q[0][0], max(0, q[0][1] - fs - 4)), "%s %s %.2f" % (t["id"], t["string"][:24], t["confidence"]),
                 size=fs, bg=(150, 0, 150))
    for t in res.get("text_marks", []):
        q = [S(p) for p in t["protect"]["poly"]]
        d.line(q + q[:1], fill=(0, 230, 230), width=1)
        io.label(d, (q[0][0], max(0, q[0][1] - fs - 4)), t["id"], size=fs, bg=(0, 110, 110))
    for f in res.get("faces", []):
        x0, y0, x1, y1 = f["box"]
        d.rectangle([S((x0, y0)), S((x1, y1))], outline=(255, 230, 0), width=2)
        io.label(d, S((x0, y1 + 2)), "%s%s" % (f["id"], " (given)" if f.get("given") else " %.2f" % f.get("confidence", 0)),
                 size=fs, bg=(120, 100, 0))
        for name, pts in f.get("landmarks", {}).items():
            for p in pts:
                X, Y = S(p)
                d.point((X, Y), fill=(255, 255, 160))
    for figs, bones, col in ((res.get("pose", {}).get("humans", []), HUMAN_BONES, (255, 90, 90)),
                             (res.get("pose", {}).get("animals", []), ANIMAL_BONES, (90, 255, 140))):
        for fig in figs:
            j = {n: v for n, v in fig["joints"].items() if v[2] >= 0.1}
            for a, b in bones:
                if a in j and b in j:
                    d.line([S(j[a]), S(j[b])], fill=col, width=2)
            for v in j.values():
                X, Y = S(v)
                d.ellipse([X - 3, Y - 3, X + 3, Y + 3], outline=col, width=1)
            if j:
                top = min(j.values(), key=lambda v: v[1])
                io.label(d, S((top[0] + 4, top[1] - 18 / max(k, 1e-3))), fig["id"], size=fs, bg=tuple(c // 2 for c in col))
    for h in res.get("heads", []):
        g = h["warp"]["region"]
        rot = g.get("rot", 0)
        q = [S(p) for p in _ellipse_pts(g["c"], g["r"], rot)]
        d.line(q + q[:1], fill=(255, 170, 0), width=2)
        q = [S(p) for p in _ellipse_pts(g["c"], [v * g["core"] for v in g["r"]], rot)]
        d.line(q + q[:1], fill=(255, 210, 120), width=1)
        X, Y = S(h["warp"]["pivot"])
        d.line([X - 6, Y, X + 6, Y], fill=(255, 170, 0), width=2)
        d.line([X, Y - 6, X, Y + 6], fill=(255, 170, 0), width=2)
        io.label(d, (X + 6, Y + 2), h["id"] + " pivot", size=fs, bg=(140, 90, 0))
    for e in res.get("eyes", []):
        X, Y = S(e["seed"])
        if "blink" in e:
            b = e["blink"]
            corners, _ = _domain(b["at"], b["r"], b.get("rot", 0))
            q = [S(p) for p in corners]
            d.line(q + q[:1], fill=(0, 255, 0) if e["checks"]["ok"] else (255, 60, 60), width=1)
        else:
            d.ellipse([X - 4, Y - 4, X + 4, Y + 4], outline=(255, 60, 60), width=2)
    peaks = res.get("saliency", {}).get("attention", {}).get("peaks", [])
    for i, p in enumerate(peaks):
        x0, y0, x1, y1 = p["box"]
        d.rectangle([S((x0, y0)), S((x1, y1))], outline=(255, 255, 0), width=1)
        io.label(d, S((x0 + 2, y0 + 2)), "attention %d" % (i + 1), size=fs, bg=(110, 110, 0))
    io.label(d, (4, 4), "vision: masks tinted+outlined, text magenta, marks cyan, faces yellow, heads orange, "
                        "joints red/green, eye domains green(ok)/red", size=fs)
    # right: where the eye goes (this run's heat map only)
    panels = [left]
    att = res.get("saliency", {}).get("attention", {}).get("file")
    if att:
        heat = _load_mask(os.path.join(out, att))[..., None]
        heat = heat / max(float(heat.max()), 1e-6)
        col = np.concatenate([np.ones_like(heat), 0.55 + 0.45 * heat, 0.15 * (1 - heat)], -1)
        tint = io.luma(rgb)[..., None] * 0.45 * (1 - 0.7 * heat) + col * 0.7 * heat
        right = Image.fromarray(io.to_u8(tint)).resize(left.size, Image.LANCZOS)
        dr = ImageDraw.Draw(right)
        for o in res.get("saliency", {}).get("objectness", {}).get("peaks", []):
            x0, y0, x1, y1 = o["box"]
            dr.rectangle([S((x0, y0)), S((x1, y1))], outline=(80, 200, 255), width=1)
        io.label(dr, (4, 4), "attention heat; objectness peaks blue", size=fs)
        panels.append(right)
    # bottom: every eye blown up with its blink domain (green/red) and mask (magenta)
    tiles = []
    for e in res.get("eyes", []):
        if "blink" not in e:
            continue
        b = e["blink"]
        corners, _ = _domain(b["at"], b["r"], b.get("rot", 0))
        cs = np.asarray(corners)
        pad = 4 + 0.5 * float(np.ptp(cs[:, 0]))
        x0, y0 = int(max(0, cs[:, 0].min() - pad)), int(max(0, cs[:, 1].min() - pad))
        x1, y1 = int(min(W, cs[:, 0].max() + pad + 1)), int(min(H, cs[:, 1].max() + pad + 1))
        z = max(1, int(180 / max(1, max(x1 - x0, y1 - y0))))
        t = Image.fromarray(io.to_u8(rgb[y0:y1, x0:x1])).resize(((x1 - x0) * z, (y1 - y0) * z), Image.NEAREST)
        dt = ImageDraw.Draw(t)
        Z = lambda p: ((p[0] - x0) * z, (p[1] - y0) * z)
        q = [Z(p) for p in corners]
        dt.line(q + q[:1], fill=(0, 255, 0) if e["checks"]["ok"] else (255, 60, 60), width=1)
        q = [Z(p) for p in b["mask"]]
        dt.line(q + q[:1], fill=(255, 0, 255), width=1)
        io.label(dt, (2, 2), e["id"], size=11)
        tiles.append(t)
    Wd = sum(p.width for p in panels) + 6 * (len(panels) - 1)
    th = max([t.height for t in tiles] or [0])
    sheet = Image.new("RGB", (max(Wd, sum(t.width + 6 for t in tiles)), left.height + (th + 6 if tiles else 0)), (24, 24, 24))
    x = 0
    for p in panels:
        sheet.paste(p, (x, 0))
        x += p.width + 6
    x = 0
    for t in tiles:
        sheet.paste(t, (x, left.height + 6))
        x += t.width + 6
    sheet.save(path, quality=90)


# ------------------------------------------------------------------ the whole pass

def _finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _given_eyes(eyes):
    """--eyes as eye seeds: [x, y] or [x, y, r] (r a rough radius), one or a list. Checked before
    anything runs, so a typo costs nothing."""
    if eyes is None:
        return []
    if isinstance(eyes, list) and eyes and all(_finite(v) for v in eyes):
        eyes = [eyes]                                   # one eye given as [x, y]
    if not isinstance(eyes, list) or not all(isinstance(p, list) and len(p) in (2, 3) and all(_finite(v) for v in p)
                                             and (len(p) == 2 or p[2] > 0) for p in eyes):
        raise ValueError("--eyes needs JSON [[x, y], ...] or [[x, y, r], ...]: finite picture pixels, r > 0")
    return [{"id": "given-eye-%d" % (i + 1), "source": "given", "at": (float(p[0]), float(p[1])),
             "R": float(p[2]) if len(p) > 2 else 6.0, "tilt": None} for i, p in enumerate(eyes)]


def _check_options(box, zoom, faces):
    for flag, b in [("--box", box)] + [("--face", f) for f in faces or []]:
        if b is not None and not (len(b) == 4 and all(_finite(v) for v in b) and b[2] > b[0] and b[3] > b[1]):
            raise ValueError("%s needs x0 y0 x1 y1 in picture pixels, with x1 > x0 and y1 > y0" % flag)
    if zoom is not None and not (_finite(zoom) and 0 < zoom <= 8):
        raise ValueError("--zoom must be in (0, 8]")


def _earlier(out):
    """What an earlier run left in `out`: its vision.json (None when there was no run, {} when it
    cannot be read) and its masks. A folder holding anything else is refused: replacing it would
    delete those files."""
    if not os.path.lexists(out):
        return None, []
    if not os.path.isdir(out):
        raise ValueError("--out %s exists and is not a folder" % out)
    names = os.listdir(out)
    foreign = sorted(n for n in names if not OURS.match(n))
    if foreign:
        raise ValueError("--out %s holds files vision does not write (%s): give a new or empty folder"
                         % (out, ", ".join(foreign[:4]) + (", ..." if len(foreign) > 4 else "")))
    masks = sorted(n for n in names if MASKS.match(n))
    if "vision.json" not in names:
        return ({} if masks else None), masks
    try:
        with open(os.path.join(out, "vision.json")) as f:
            prev = json.load(f)
    except (OSError, ValueError):
        prev = {}
    return (prev if isinstance(prev, dict) else {}), masks


def _conflict(prev, masks, sha, box, zoom, what):
    """Why an earlier run may not be replaced without --force, or None. plan.json may point at its
    masks, so they must come out the same: same picture, region and enlargement, and made again.
    Everything else in a run is copied into plan.json or scene.json as data, never pointed at."""
    if prev is None or not masks:
        return None
    if prev.get("picture_sha256") != sha:
        return "a run on another picture" if prev.get("picture_sha256") else "an earlier run"
    opts = prev.get("options") or {}
    if opts.get("box") != box or opts.get("zoom") != zoom:
        return "a run with another --box or --zoom"
    gone = [m for m in masks if ("subjects" if m.startswith("subject-") else "persons") not in what]
    if gone:
        return "masks this run would not make again (%s)" % ", ".join(gone)
    return None


def _same(a, b):
    with open(a, "rb") as f, open(b, "rb") as g:
        if f.read() == g.read():
            return True
    try:
        with Image.open(a) as x, Image.open(b) as y:
            return x.size == y.size and np.array_equal(np.asarray(x.convert("L")), np.asarray(y.convert("L")))
    except OSError:
        return False


def _publish(work, out, masks, force):
    """Put the finished run in `work` in place of `out`, which held `masks` from an earlier run."""
    changed = [m for m in masks if not os.path.exists(os.path.join(work, m)) or not _same(os.path.join(out, m), os.path.join(work, m))]
    if changed and not force:                # Vision itself answered differently (another macOS, say)
        raise ValueError("this run's masks differ from the earlier run's in %s (%s), and plan.json may point at them: "
                         "write this run to another --out, or pass --force to replace it" % (out, ", ".join(changed)))
    if not os.path.lexists(out):
        os.rename(work, out)
        return
    aside = "%s.old-%d-%s" % (work, os.getpid(), uuid.uuid4().hex[:8])
    os.rename(out, aside)
    try:
        os.rename(work, out)
    except OSError:
        os.rename(aside, out)
        raise
    shutil.rmtree(aside, ignore_errors=True)


def vision(image, out, what=ALL, box=None, zoom=None, faces=None, eyes=None, langs=None, plan_dir=None,
           force=False, log=None):
    """Run Vision on the working picture; write vision.json, masks and review.jpg into `out`; return a summary.

    Arguments, the picture and what `out` already holds are checked before Vision runs. One folder holds
    one run: it is written to a temporary folder next to `out` and moved into place only when complete,
    so a failure leaves an earlier run as it was, and nothing stale survives a new one. An earlier run is
    replaced only when its masks come out the same (plan.json may point at them), or with `force`."""
    what = [w for w in what if w]
    bad = [w for w in what if w not in ALL]
    if bad:
        raise ValueError("vision: unknown analysis %s (have %s)" % (", ".join(bad), ", ".join(ALL)))
    given = _given_eyes(eyes)
    box = [float(v) for v in box] if box is not None else None
    faces = [[float(v) for v in b] for b in faces] if faces else None
    zoom = float(zoom) if _finite(zoom) else zoom
    _check_options(box, zoom, faces)
    langs = [s.strip() for s in langs or [] if s.strip()] or None
    rgb, _ = io.load_rgb(image)                 # a damaged picture fails here, before Vision runs
    with open(image, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    out = os.path.abspath(out)
    real = os.path.realpath(out)                # move folders where they are, keeping a symlinked --out
    prev, old = _earlier(real)
    why = None if force else _conflict(prev, old, sha, box, zoom, what)
    if why:
        raise ValueError("%s holds %s, and plan.json may point at its masks: write this run to another --out "
                         "(say one folder per --box), or pass --force to replace it" % (out, why))
    swift_tool("lpvision", log)                 # not a Mac, no compiler: say so before touching any folder
    os.makedirs(os.path.dirname(real), exist_ok=True)
    work = os.path.join(os.path.dirname(real), ".%s.tmp-%d-%s" % (os.path.basename(real), os.getpid(), uuid.uuid4().hex[:8]))
    os.mkdir(work)
    try:
        raw = run(image, what, work, box=box, zoom=zoom, faces=faces, langs=langs, log=log)
        res = _results(raw, rgb, image, work, out, what, given, os.path.abspath(plan_dir or _job_dir(image)))
        res["picture_sha256"] = sha
        res["options"] = {"what": what, "box": box, "zoom": zoom, "faces": faces, "eyes": eyes, "langs": langs}
        _review(rgb, res, work, os.path.join(work, "review.jpg"))
        res["review"] = os.path.join(out, "review.jpg")
        with open(os.path.join(work, "vision.json"), "w") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        _publish(work, real, old, force)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _summary(res, os.path.join(out, "vision.json"))


def _results(raw, rgb, image, work, out, what, seeds, base):
    """vision.json's content from lpvision's answer; files are read from `work`, named as in `out`."""
    H, W = rgb.shape[:2]
    if [W, H] != raw["size"]:
        raise RuntimeError("vision: Vision saw %s but the picture is %dx%d" % (raw["size"], W, H))
    res = {"picture": os.path.abspath(image), "size": [W, H], "paths_relative_to": base,
           "analysed": raw.get("analysed"), "macos": raw.get("macos"), "errors": {}, "timings_ms": raw.get("timings_ms", {})}
    for w in what:
        if isinstance(raw.get(w), dict) and "error" in raw[w]:
            res["errors"][w] = raw[w]["error"]

    def masks(key, stem):
        items = []
        block = raw.get(key) if isinstance(raw.get(key), dict) else {}
        for i, e in enumerate(block.get("instances", [])):
            hard = _load_mask(os.path.join(work, e["mask"])) >= 0.5
            item = {"id": "%s-%d" % (stem, i + 1), "file": e["mask"], "bbox": e["bbox"], "share": e["share"],
                    "soft_edge_px": e["soft_edge_px"], "part": {"mode": "mask", "image": _rel(os.path.join(out, e["mask"]), base)}}
            item["pieces"], specks = _pieces(hard)
            if specks:
                item["specks"] = specks
            items.append(item)
        return items, block

    if "subjects" in what:
        res["subjects"], _ = masks("subjects", "subject")
    if "persons" in what:
        res["persons"], block = masks("persons", "person")
        if block.get("matte"):
            name = block["matte"]["mask"]
            res["persons_matte"] = {"file": name, "bbox": block["matte"]["bbox"],
                                    "part": {"mode": "mask", "image": _rel(os.path.join(out, name), base)},
                                    "note": "all persons in one matte, finer at hair and hands; clip it with a poly to one figure"}
            # The two person models disagree on false alarms (a flat grey picture gets an instance, no matte).
            matte = _load_mask(os.path.join(work, name)) >= 0.5
            for e in res["persons"]:
                inst = _load_mask(os.path.join(work, e["file"])) >= 0.5
                e["matte_agreement"] = round(float((inst & matte).sum()) / max(1, int(inst.sum())), 3)
                e["doubtful"] = e["matte_agreement"] < 0.3

    seeds = list(seeds)
    humans = raw.get("pose", {}).get("humans", []) if isinstance(raw.get("pose"), dict) else []
    animals = raw.get("pose", {}).get("animals", []) if isinstance(raw.get("pose"), dict) else []
    res["heads"] = []
    if "faces" in what and isinstance(raw.get("faces"), list):
        res["faces"] = []
        for i, f in enumerate(raw["faces"]):
            fid = "face-%d" % (i + 1)
            lm = f.get("landmarks", {})
            face = {"id": fid, "box": f["box"], "given": f.get("given", False)}
            for k in ("confidence", "landmarks_confidence", "roll_deg", "yaw_deg", "pitch_deg"):
                if k in f:
                    face[k] = f[k]
            face["landmarks"] = lm
            cs = [(n, np.asarray(lm[n], float)) for n in ("left_eye", "right_eye") if len(lm.get(n, [])) >= 3]
            cs.sort(key=lambda t: t[1][:, 0].mean())            # by position in the picture, not by name
            centres = [tuple(c.mean(0)) for _, c in cs]
            tilt = None
            if len(cs) == 2:
                tilt = math.degrees(math.atan2(centres[1][1] - centres[0][1], centres[1][0] - centres[0][0]))
            for (n, c), side in zip(cs, ("left", "right") if len(cs) == 2 else ("one",)):
                half = 0.5 * float(np.ptp(c[:, 0]) or 4)
                seeds.append({"id": "%s-eye-%s" % (fid, side), "source": "face landmarks", "at": tuple(c.mean(0)),
                              "R": max(4.0, 1.25 * half), "tilt": tilt, "contour": c.tolist()})
            neck = None
            x0, y0, x1, y1 = f["box"]
            for hm in humans:
                n = hm["joints"].get("neck")
                if n and n[2] >= 0.3 and x0 - (x1 - x0) < n[0] < x1 + (x1 - x0) and y0 < n[1] < y1 + 1.5 * (y1 - y0):
                    neck = n
            warp, src = _face_head(face, centres if len(cs) == 2 else None, neck)
            res["heads"].append({"id": fid + "-head", "source": "face, pivot at the " + src, "warp": warp})
            res["faces"].append(face)
    if "pose" in what and isinstance(raw.get("pose"), dict):
        res["pose"] = {"humans": [], "animals": [], "animal_labels": raw["pose"].get("animal_labels", [])}
        for i, hm in enumerate(humans):
            hid = "human-%d" % (i + 1)
            j = hm["joints"]
            res["pose"]["humans"].append({"id": hid, "confidence": hm["confidence"], "joints": j,
                                          "limbs": _limbs(j, HUMAN_LIMBS)})
            le, re = j.get("left_eye"), j.get("right_eye")
            ok = [v for v in (le, re) if v and v[2] >= 0.3]
            in_face = any(f["box"][0] <= v[0] <= f["box"][2] and f["box"][1] <= v[1] <= f["box"][3]
                          for v in ok for f in res.get("faces", []))
            if ok and not in_face:
                # pose eyes are a few pixels off on small figures: search a little wider than the eye
                if len(ok) == 2:
                    R = max(3.0, 0.45 * math.hypot(ok[0][0] - ok[1][0], ok[0][1] - ok[1][1]))
                elif "nose" in j:
                    R = max(3.0, 0.45 * math.hypot(ok[0][0] - j["nose"][0], ok[0][1] - j["nose"][1]))
                else:
                    R = 4.0
                for v, side in zip(sorted(ok), ("left", "right") if len(ok) == 2 else ("one",)):
                    seeds.append({"id": "%s-eye-%s" % (hid, side), "source": "body pose", "at": (v[0], v[1]),
                                  "R": R, "tilt": None})
                warp = _pose_head(j)
                if warp:
                    res["heads"].append({"id": hid + "-head", "source": "body pose", "warp": warp})
        for i, an in enumerate(animals):
            aid = "animal-%d" % (i + 1)
            j = an["joints"]
            res["pose"]["animals"].append({"id": aid, "confidence": an["confidence"], "joints": j,
                                           "limbs": _limbs(j, ANIMAL_LIMBS)})
            ok = sorted(v for v in (j.get("left_eye"), j.get("right_eye")) if v and v[2] >= 0.3)
            nose = j.get("nose")
            for v, side in zip(ok, ("left", "right") if len(ok) == 2 else ("one",)):
                R = max(2.5, 0.3 * math.hypot(v[0] - nose[0], v[1] - nose[1])) if nose else 4.0
                seeds.append({"id": "%s-eye-%s" % (aid, side), "source": "animal pose", "at": (v[0], v[1]), "R": R,
                              "tilt": None})
            warp = _animal_head(j)
            if warp:
                res["heads"].append({"id": aid + "-head", "source": "animal pose", "warp": warp})
    res["eyes"] = _eyes(rgb, seeds, (W, H)) if seeds else []

    if "text" in what and isinstance(raw.get("text"), dict) and "lines" in raw["text"]:
        res["text"], res["text_marks"] = [], []
        for i, t in enumerate(raw["text"]["lines"]):
            q = t.get("quad") or _box_quad(t["box"])
            hgt = math.hypot(q[3][0] - q[0][0], q[3][1] - q[0][1])
            poly = _grow_quad(q, max(3.0, 0.15 * hgt))
            tid = "text-%d" % (i + 1)
            res["text"].append({
                "id": tid, "string": t["string"], "confidence": t["confidence"],
                "doubtful": t["confidence"] < 0.5 or not any(ch.isalnum() for ch in t["string"]),
                "quad": t.get("quad"), "protect": {"poly": poly},
                "fixed": {"plan_layer": {"id": tid, "parts": [{"mode": "exact", "poly": poly}], "feather": 0},
                          "scene_layer": {"id": tid, "fixed": True}}})
        lines = [t["box"] for t in raw["text"]["lines"]]
        for t in raw["text"].get("marks", []):
            if _covered(t["box"], lines) < 0.5:
                b = t["box"]
                res["text_marks"].append({"id": "mark-%d" % (len(res["text_marks"]) + 1), "box": b, "chars": t["chars"],
                                          "protect": {"poly": _grow_quad(_box_quad(b), 3.0)}})
    if "saliency" in what and isinstance(raw.get("saliency"), dict):
        res["saliency"] = {}
        for name, block in raw["saliency"].items():
            if "heatmap" in block:
                res["saliency"][name] = {"file": block["heatmap"], "max": block["max"], "objects": block["objects"],
                                         "peaks": _peaks(os.path.join(work, block["heatmap"]))}
    res["note"] = ("Starting points, not answers. Look at review.jpg, then check each item at native size "
                   "(picture.py look --box ... --poly ...) before pasting it into plan.json or scene.json.")
    return res


def _summary(res, path):
    s = {"vision": path, "review": res["review"], "size": res["size"]}
    for key in ("subjects", "persons"):
        if key in res:
            s[key] = [dict({"id": e["id"], "bbox": e["bbox"], "share": e["share"], "pieces": len(e["pieces"])},
                           **({"doubtful": True} if e.get("doubtful") else {})) for e in res[key]]
    if "faces" in res:
        s["faces"] = [{"id": f["id"], "box": f["box"], "confidence": f.get("confidence")} for f in res["faces"]]
    s["eyes"] = [{"id": e["id"], "source": e["source"], "blink": "blink" in e,
                  "ok": e.get("checks", {}).get("ok", False)} for e in res["eyes"]]
    s["heads"] = [h["id"] for h in res["heads"]]
    if "pose" in res:
        s["pose"] = {"humans": len(res["pose"]["humans"]), "animals": len(res["pose"]["animals"])}
    if "text" in res:
        s["text"] = [{"id": t["id"], "string": t["string"], "confidence": t["confidence"]} for t in res["text"]]
        s["text_marks"] = len(res["text_marks"])
    if "saliency" in res and "attention" in res["saliency"]:
        s["attention_peaks"] = [{"at": p["at"], "box": p["box"]} for p in res["saliency"]["attention"]["peaks"]]
    s["errors"] = res["errors"]
    s["timings_ms"] = res["timings_ms"]
    s["note"] = "starting points: look at the review sheet, then copy entries from vision.json after checking them"
    return s
