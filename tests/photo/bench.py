#!/usr/bin/env python3
"""Benchmark `picture.py rectify` on simulated casual photos with exact ground truth (make the cases with
make_photos.py; README.md explains the numbers).

  python3 bench.py DIR --out REPORT_DIR [--tool "CMD ..."] [--standin NAME] [--cases GLOB,...] [--jobs N]
  python3 bench.py DIR --out REPORT_DIR --self-test       prove the benchmark with stand-ins before it judges

Each case runs  <tool> PHOTO --out REPORT_DIR/runs/<case>/out.png --review REPORT_DIR/runs/<case>/review.jpg
[the case's args], <tool> being `python3 <skill>/scripts/picture.py rectify` unless --tool or --standin says
otherwise. Writes REPORT_DIR/report.json (every number and check), summary.md (one row per case),
sheets/<case>.jpg (photo with the true quad in green and the reported one in magenta | output | gt |
CIEDE2000 map, the tool's review sheet below) and overview.jpg. Prints one JSON summary. Exit 0 when every
gating case passes, 1 when one fails, 2 on a usage error.

Stand-ins (--standin, and all of them under --self-test) prove the benchmark before it judges anyone. oracle
inverts the exact forward model and must pass everything; identity exits 4 everywhere; copy returns the photo.
The others are the oracle with exactly one fault, flags and warnings as the oracle's (so a measurement, not a
flag, has to catch them): geometry leaves light and colour (yet claims to have corrected them); nolens maps
the true corners with a plain homography; nolight leaves the light (claims it); grayworld balances colour by
grey world instead of evidence; flatten divides out the picture's own low-frequency light; overcrop cuts 1.5%
off every side; stretch makes the output 3% too wide (corners exact); sharpen adds a mild unsharp mask;
enhance adds 15% saturation and contrast. grayworld, flatten, sharpen and enhance also "fix" the flat no-op
pictures; overcorrect does all four restyles at once, everywhere, without warnings. --self-test checks that
each one fails exactly where it should and only for its own reason (README.md has the matrix), and that
CIEDE2000 and the camera model are right.

ACCEPTANCE THRESHOLDS (a case passes when every gating check that applies to it passes)
 Geometry, where a correction is expected and an output was written:
  corners    the output is aligned to gt by a direct, coarse-to-fine homography fit on locally normalised
             luminance; the largest distance between where the output's corners land and gt's corners,
             in % of the gt diagonal: <= 0.8 (visible corners only when one is outside the photo)
  aspect     width/height of the work inside the output against gt: <= meta's aspect_tol_pct (1.0 with
             the focal length in EXIF; otherwise what an ideal method can know, see make_photos.py)
  straight   what still bows after that homography, in % of the gt diagonal: <= 0.35. Local shifts are
             measured on a grid of patches (one that only crosses an edge gives the shift across it); the
             share of the case's own lens bow left in the output is fitted to them together with a small
             homography change (a matched filter: one number, little noise) and multiplied by that bow;
             the largest shift of any patch textured both ways stands for other non-projective errors;
             the larger of the two counts
  quad       the corners rectify reports, TL TR BR BL, against the work's true corners in the photo (or its
             mat's or frame's), in % of the work's diagonal in the photo: <= 1.5
  resolution the output's long side over the work's longest side in the photo, capped at 4096 (--max's
             default): 0.7 .. 2.0
 Colour and light, over the work on a comparison grid of at most 400 px (gt area-averaged in linear light;
 ignore.png and a 1.5% border left out):
  colour     CIEDE2000 after one global exposure factor (absolute exposure is unknowable from a casual
             photo, and one scalar cannot hide a cast), each pixel scored against the best of its 3 x 3 gt
             neighbours (registration is judged above): mean <= 3.0 and P95 <= 10
  light      how much the light still varies over the work: after removing global per-channel gains,
             exp(P98 - P2) - 1 of log(G(Y_out) / G(Y_gt)), local means under a Gaussian of 4% of the
             diagonal (blur, noise and resampling keep local means), over pixels with gt luminance
             above 0.01 (darker shadows sit on the photo's noise floor), strong gt edges down-weighted
             (registration is judged above): <= 0.04. A painting's own gradient flattened away fails.
  chroma     mean C*ab output / gt where gt's mean C* >= 10: 0.85 .. 1.15 (no saturation boost, no
             neutralising a coloured painting)
  contrast   standard deviation of L* output / gt, both smoothed by one grid pixel (resampling blur on thin
             lines is not a tone change), where gt's is at least 5: 0.88 .. 1.12 (no contrast boost)
  sharpen    fine-detail energy (Laplacian of Gaussian, 1 px) of each log channel scaled to its median, in
             the output over that of the photo resampled onto the output's grid through the true mapping
             with a cubic spline, channels weighted by their signal (so light, exposure and white balance,
             all gains, cannot read as sharpening): <= 1.2 (honest cubic or Lanczos resampling scores
             0.9-1.1; a mild unsharp mask, 0.5 at 1 px, 1.27-1.8; a contrast boost raises it too)
 No-op cases (already a flat scan or screenshot): exit 4 with nothing written; an output is accepted only
  with corners <= 0.5, colour mean <= 1.5 and P95 <= 4.0 with no exposure refit, light <= 0.02, and the
  chroma, contrast and sharpen limits above (restyling is never accepted, wherever an output is written).
 Flags and words, every case: the exit code is in meta's set (2 is always a failure); the JSON has the
  contract's keys; size_in is the photo's size after EXIF orientation; exit 4 writes nothing; light and
  colour report applied=true where a correction is required and applied=false where it is forbidden;
  quad.source is "given" with --corners; frame found/removed as meta says; every expected warning topic
  is present (TOPICS); no reshoot warning where none is due (ALARMS); when colour must stay as shot, the
  warnings or the note say so.
 Stress cases gate on the exit code, the expected warning (for the steep view a refusal, exit 4, also
  passes), the contract, no sharpening and, for glare, that the glare is kept rather than painted over;
  their geometry and colour are reported only. Stretch cases (beyond a global light and colour model) are
  reported, never gating. Real photos (no gt) only have to run: exit 0 or 4 with valid JSON.
 Run time and the tool's peak resident memory (largest process, where the OS reports it) are reported per
  case; a run longer than --timeout (default 300 s) fails. The perf-48mp case (a 48 MP phone photo) also
  gates on the brief's targets: <= 30 s and <= 3 GB, measured with --jobs 1 (parallel runs share the
  machine and are reported only).
"""
import argparse
import concurrent.futures as cf
import fnmatch
import json
import math
import os
import re
import shlex
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from scipy import ndimage as ndi

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import make_photos as mp  # noqa: E402

SKILL = mp.SKILL
TOOL = [sys.executable, os.path.join(SKILL, "scripts", "picture.py"), "rectify"]
TH = {"corners": 0.8, "straight": mp.STRAIGHT_TOL, "quad": 1.5, "res": (0.7, 2.0), "de_mean": mp.DE_TOL, "de_p95": 10.0,
      "light": mp.LIGHT_TOL, "chroma": (0.85, 1.15), "contrast": (0.88, 1.12), "tonal_min": 5.0, "sharpen": 1.2,
      "ncc": 0.35, "noop_corners": 0.5, "noop_de_mean": 1.5, "noop_de_p95": 4.0, "noop_light": 0.02,
      "perf_time_s": 30.0, "perf_rss_gb": 3.0}
STANDINS = ("oracle", "identity", "copy", "geometry", "nolens", "nolight", "grayworld", "flatten", "overcrop", "stretch",
            "sharpen", "enhance", "overcorrect")
RESTYLERS = ("grayworld", "flatten", "sharpen", "enhance", "overcorrect")    # these also "fix" flat no-op pictures

# Plain words that make a warning count for a topic (English and Chinese), and the stricter words that
# make an unexpected warning a false alarm.
TOPICS = {
    "glare": r"glare|reflect|specular|shiny|gloss|blown|burn|clipp|washed out|反光|眩光|过曝",
    "cut_off": r"cut off|cut-off|cutoff|outside the (photo|frame|picture|image)|beyond the (edge|photo|frame)|"
               r"not (fully|entirely|completely|all) (in|visible|inside)|partly (out|visible)|missing|cropped|"
               r"truncat|incomplete|off the edge|(whole|all of the|entire) (work|painting|picture|artwork) in (view|the "
               r"frame)|被裁|不完整|超出",
    "steep": r"steep|oblique|foreshorten|extreme angle|grazing|straight[- ]on|head[- ]on|角度太|太斜|倾斜过",
    "blur": r"blur|shake|motion|out of focus|smear|soft focus|not sharp|模糊|抖动",
    # the problem (few pixels) or the remedy (closer, fill the frame) in plain words
    "resolution": r"resolution|too small|few pixels|low[- ]res|too low|tiny|pixels across|\d+ ?[x×] ?\d+ ?px|\bpx\b|"
                  r"(move|get|come) closer|fill more of the (frame|photo|picture)|分辨率|像素|靠近",
    "no_color_reference": r"(\bno\b|without|\bnot\b|n't|\bleft\b|\bkept\b|unchanged|skipp|nothing).{0,48}"
                          r"(colou?r|white balance|cast|neutral)|(colou?r|white balance|cast).{0,48}"
                          r"(\bleft\b|\bkept\b|unchanged|as shot|as it is|not (corrected|changed|applied|touched)|"
                          r"skipp)|未.{0,8}(校|改|纠)|保留.{0,6}(颜色|色)",
}
ALARMS = {
    "glare": r"glare|specular|reflection|反光|眩光",
    "cut_off": r"cut off|cut-off|outside the (photo|frame|picture|image)|not (fully|entirely|completely) (in|visible)|"
               r"off the edge|被裁|超出",
    "blur": r"blur|camera shake|out of focus|模糊|抖动",
    "resolution": r"resolution|too small|few pixels|low[- ]res|分辨率",
    "steep": r"steep|oblique|foreshorten|太斜",
}
CONTRACT = {"changed": None, "written": None, "review": None, "size_in": None, "size_out": None,
            "quad": ("corners", "source", "confidence"),
            "lens": ("applied", "k1", "straightness_px_before", "straightness_px_after"),
            "perspective": ("applied", "aspect", "aspect_method", "tilt_deg"),
            "light": ("applied", "reference", "variation_before", "variation_after"),
            "color": ("applied", "reference", "gains", "cast_before", "cast_after"),
            "frame": ("found", "removed"), "warnings": None, "note": None}


# --------------------------------------------------------------------------- images

def load_rgb(path):
    """sRGB floats after EXIF orientation, and the alpha (or None)."""
    im = ImageOps.exif_transpose(Image.open(path))
    alpha = None
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        alpha = np.asarray(im, np.float32)[..., 3] / 255
    return np.asarray(im.convert("RGB"), np.float32) / 255, alpha


def luma(rgb):
    return rgb @ np.array([0.299, 0.587, 0.114], np.float32)


def local_norm(a, sigma):
    """Zero local mean, unit local contrast: alignment that does not care about light or colour."""
    m = ndi.gaussian_filter(a, sigma)
    d = a - m
    return d / (np.sqrt(ndi.gaussian_filter(d * d, sigma)) + 0.015)


def norm_mat(w, h):
    r = max(w, h) / 2.0
    return np.array([[1 / r, 0, -w / 2.0 / r], [0, 1 / r, -h / 2.0 / r], [0, 0, 1.0]])


# --------------------------------------------------------------------------- alignment

def _warp(img, Ml, shape):
    """Sample img (index grid) at level-grid pixel centres mapped by Ml (level px -> img px, continuous)."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w] + 0.5
    x, y, _ = mp.hom(Ml, xs, ys)
    valid = (x >= 0) & (y >= 0) & (x <= img.shape[1]) & (y <= img.shape[0])
    v = ndi.map_coordinates(img, [y - 0.5, x - 0.5], order=1, mode="nearest")
    return v, valid


def _ncc(a, b, m):
    if m.sum() < 50:
        return -1.0
    a, b = a[m] - a[m].mean(), b[m] - b[m].mean()
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))


def align(out_l, gt_l, init=None):
    """Homography M (gt px -> output px) that best aligns the output to gt, found by a coarse search over
    scale and shift and refined coarse-to-fine by inverse-compositional Lucas-Kanade with robust weights.
    Returns M, the NCC at the finest level, and the level images for the straightness check."""
    Hg, Wg = gt_l.shape
    Ho, Wo = out_l.shape
    Ng, No = norm_mat(Wg, Hg), norm_mat(Wo, Ho)
    P0 = No @ np.diag([Wo / Wg, Ho / Hg, 1.0]) @ np.linalg.inv(Ng) if init is None else No @ init @ np.linalg.inv(Ng)
    ratio = math.sqrt((Wg / Wo) * (Hg / Ho))
    longs = [L for L in (96, 192, 384, 640) if L < max(Wg, Hg)] + [min(640, max(Wg, Hg))]
    longs = sorted(set(longs))
    P = P0
    lvl = None
    for li, L in enumerate(longs):
        k = L / float(max(Wg, Hg))
        wl, hl = max(8, round(Wg * k)), max(8, round(Hg * k))
        wo, ho = max(8, round(Wo * k * ratio)), max(8, round(Ho * k * ratio))
        T = local_norm(mp.resize_lin(gt_l, (wl, hl)), max(1.5, 0.02 * L))
        I = local_norm(mp.resize_lin(out_l, (wo, ho)), max(1.5, 0.02 * L))
        Dg = np.diag([wl / Wg, hl / Hg, 1.0])
        Do = np.diag([wo / Wo, ho / Ho, 1.0])
        A = Do @ np.linalg.inv(No)                 # normalised out -> out level px
        B = Ng @ np.linalg.inv(Dg)                 # gt level px -> normalised gt
        if li == 0:
            best = (-2, P)
            for s in (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.25):
                for tx in np.linspace(-0.15, 0.15, 7):
                    for ty in np.linspace(-0.15, 0.15, 7):
                        Pc = P @ np.array([[s, 0, tx], [0, s, ty], [0, 0, 1.0]])
                        v, m = _warp(I, A @ Pc @ B, (hl, wl))
                        if m.mean() < 0.5:
                            continue
                        sc_ = _ncc(v, T, m) * min(1.0, m.mean() / 0.8)
                        if sc_ > best[0]:
                            best = (sc_, Pc)
            P = best[1]
        ys, xs = np.mgrid[0:hl, 0:wl] + 0.5
        xn, yn, _ = mp.hom(B, xs, ys)
        gy, gx = np.gradient(T)
        sx, sy = wl / Wg / Ng[0, 0], hl / Hg / Ng[1, 1]     # level px per normalised unit
        Tx, Ty = gx * sx, gy * sy
        SD = np.stack([Tx * xn, Tx * yn, Tx, Ty * xn, Ty * yn, Ty, -(Tx * xn * xn + Ty * xn * yn),
                       -(Tx * xn * yn + Ty * yn * yn)], -1).reshape(-1, 8)
        Tf = T.ravel()
        for it in range(60):
            v, m = _warp(I, A @ P @ B, (hl, wl))
            e = (v.ravel() - Tf)
            mf = m.ravel()
            if mf.sum() < 100:
                break
            sig = 1.4826 * np.median(np.abs(e[mf])) + 1e-6
            kh = 1.345 * sig
            w = np.where(np.abs(e) <= kh, 1.0, kh / np.maximum(np.abs(e), 1e-9)) * mf
            Hs = SD.T @ (SD * w[:, None])
            b = SD.T @ (w * e)
            try:
                dp = np.linalg.solve(Hs + 1e-9 * np.eye(8), b)
            except np.linalg.LinAlgError:
                break
            dW = np.array([[1 + dp[0], dp[1], dp[2]], [dp[3], 1 + dp[4], dp[5]], [dp[6], dp[7], 1.0]])
            P = P @ np.linalg.inv(dW)
            P /= P[2, 2]
            if np.abs(dp).max() < 2e-5:
                break
        lvl = (T, I, A, B, (hl, wl), Dg)
    T, I, A, B, shp, Dg = lvl
    v, m = _warp(I, A @ P @ B, shp)
    M = np.linalg.inv(No) @ P @ Ng
    return M / M[2, 2], _ncc(v, T, m), (T, v, m, Dg)


def patch_shifts(lvl, Wg, Hg):
    """Local shifts between the aligned output and gt on a grid of patches. A patch with texture both ways
    gives two constraints, one that only crosses an edge (a painted border, a paper edge) gives the shift
    across it. Returns constraints [(x, y, nx, ny, shift along n, weight)] in gt px and the largest full
    shift of a two-way patch."""
    T, V, m, Dg = lvl
    hl, wl = T.shape
    nx_, ny_ = (8, 6) if wl >= hl else (6, 8)
    cons, full = [], []
    for j in range(ny_):
        for i in range(nx_):
            x0, x1 = int(i * wl / nx_), int((i + 1) * wl / nx_)
            y0, y1 = int(j * hl / ny_), int((j + 1) * hl / ny_)
            t, mm = T[y0:y1, x0:x1], m[y0:y1, x0:x1]
            if mm.mean() < 0.9:
                continue
            gy, gx = np.gradient(t)
            J = np.array([[(gx * gx).mean(), (gx * gy).mean()], [(gx * gy).mean(), (gy * gy).mean()]])
            lams, vecs = np.linalg.eigh(J)               # ascending
            lam2, lam1 = lams
            if lam1 < 0.03:
                continue                                 # flat: nothing to measure
            d = np.zeros(2)
            yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float64)
            for _ in range(12):
                vs = ndi.map_coordinates(V, [yy + d[1], xx + d[0]], order=1, mode="nearest")
                vy, vx = np.gradient(vs)
                Am = np.stack([vx.ravel(), vy.ravel()], -1)
                N = Am.T @ Am
                step = np.linalg.solve(N + 0.05 * np.trace(N) * np.eye(2), Am.T @ (t - vs).ravel())
                d += step                                # damped: an edge-only patch does not drift along its edge
                if np.abs(step).max() < 1e-3:
                    break
            dg = np.array([d[0] * Wg / wl, d[1] * Hg / hl])
            cx, cy = (x0 + x1) / 2 * Wg / wl, (y0 + y1) / 2 * Hg / hl
            v1 = vecs[:, 1]
            cons.append((cx, cy, v1[0], v1[1], float(v1 @ dg), min(lam1, 0.5)))
            if lam2 >= 0.03:
                v2 = vecs[:, 0]
                cons.append((cx, cy, v2[0], v2[1], float(v2 @ dg), min(lam2, 0.5)))
                full.append(math.hypot(*dg))
    return cons, (max(full) if full else None)


def lens_pattern(meta, Wg, Hg, pts):
    """The displacement a rectifier that ignores the lens would leave, after the best homography (gt px), at
    pts, and its largest value over the work: a matched filter for residual lens bow."""
    gc = np.array([[0, 0], [Wg, 0], [Wg, Hg], [0, Hg]], float)
    Hc = mp.fit_homography(gc, np.asarray(meta["corners"], float))
    Gi = np.linalg.inv(np.array(meta["camera"]["gt_to_plane"]))

    def through(p):
        px, py, _ = mp.hom(Hc, p[:, 0], p[:, 1])
        X, Y, _ = mp.photo_to_plane(meta, px, py)
        u, v, _ = mp.hom(Gi, X, Y)
        return np.c_[u, v]
    uu, vv = np.meshgrid(np.linspace(0, Wg, 17), np.linspace(0, Hg, 17))
    grid = np.c_[uu.ravel(), vv.ravel()]
    A = mp.fit_homography(grid, through(grid))

    def resid(p):
        ax, ay, _ = mp.hom(A, p[:, 0], p[:, 1])
        return through(p) - np.c_[ax, ay]
    pg = resid(grid)
    return resid(np.asarray(pts, float)), float(np.hypot(pg[:, 0], pg[:, 1]).max())


def straightness(lvl, meta, Wg, Hg):
    """How far the output still bows (% of the gt diagonal): the share of the case's own lens bow left in it
    (matched filter over every patch constraint, so one parameter and little noise) times that bow, or the
    largest local shift of any two-way patch (other non-projective errors), whichever is larger."""
    cons, full = patch_shifts(lvl, Wg, Hg)
    diag = math.hypot(Wg, Hg)
    if len(cons) < 6:
        return None, None, None, len(cons)
    c = np.asarray(cons)
    raw = None if full is None else full / diag * 100
    left = 0.0
    if meta.get("camera") and abs(meta["camera"].get("k1", 0)) > 0:
        P, maxP = lens_pattern(meta, Wg, Hg, c[:, :2])
        nx_, ny_, s, w = c[:, 2], c[:, 3], c[:, 4], c[:, 5]
        cols = [nx_ * P[:, 0] + ny_ * P[:, 1]]
        if len(cons) >= 24:
            # fitted together with a small homography change: the alignment is the best homography over the
            # textured part of the work, not over all of it, and may absorb part of the bow
            R = max(Wg, Hg) / 2.0
            x, y = (c[:, 0] - Wg / 2.0) / R, (c[:, 1] - Hg / 2.0) / R
            cols += [nx_ * x, nx_ * y, nx_, ny_ * x, ny_ * y, ny_, -(nx_ * x * x + ny_ * x * y), -(nx_ * x * y + ny_ * y * y)]
        A = np.stack(cols, -1) * np.sqrt(w)[:, None]
        sol = np.linalg.lstsq(A, s * np.sqrt(w), rcond=None)[0]
        left = abs(float(sol[0])) * maxP / diag * 100
    return max(left, raw or 0.0), left, raw, len(cons)


# --------------------------------------------------------------------------- colour and light

def lab_slack_de(lab_o, lab_g, mask):
    """CIEDE2000 of each output pixel against the best of its 3 x 3 gt neighbours."""
    h, w = mask.shape
    pad = np.pad(lab_g, ((1, 1), (1, 1), (0, 0)), mode="edge")
    best = None
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            d = mp.delta_e2000(lab_o, pad[dy:dy + h, dx:dx + w])
            best = d if best is None else np.minimum(best, d)
    return best


def grid_sample(src_lin, src_alpha, mapfn, q, wc, hc, Wg, Hg):
    """A source (gt, an output or the photo) on the wc x hc comparison grid over gt: prefiltered to the grid's
    scale q (source px per grid px), then sampled through mapfn (gt px -> source px). gt goes through the same
    path, so an unchanged picture compares exactly equal."""
    src = src_lin
    if q > 1.3:
        src = mp.resize_lin(src_lin, (max(8, round(src_lin.shape[1] / q)), max(8, round(src_lin.shape[0] / q))))
    ky, kx = src.shape[0] / src_lin.shape[0], src.shape[1] / src_lin.shape[1]
    ys, xs = np.mgrid[0:hc, 0:wc] + 0.5
    x, y = mapfn(xs * Wg / wc, ys * Hg / hc)
    valid = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (y >= 0) & (x <= src_lin.shape[1]) & (y <= src_lin.shape[0])
    xi, yi = np.where(valid, x * kx, 0) - 0.5, np.where(valid, y * ky, 0) - 0.5
    o = np.stack([ndi.map_coordinates(src[..., c], [yi, xi], order=1, mode="nearest") for c in range(3)], -1)
    if src_alpha is not None:
        al = ndi.map_coordinates(src_alpha, [np.where(valid, y, 0) - 0.5, np.where(valid, x, 0) - 0.5], order=1, mode="nearest")
        valid &= al > 0.5
    return o, valid


def cmp_grid(meta, case_dir, gt=None, ign=None):
    """gt (sRGB) and, on the comparison grid, gt in linear light, the ignore mask and the grid's size."""
    if gt is None:
        gt = mp.load_flat(os.path.join(case_dir, meta["gt"]))
    Hg, Wg = gt.shape[:2]
    if ign is None:
        ign = np.zeros((Hg, Wg), bool)
        if meta.get("ignore"):
            ign = np.asarray(Image.open(os.path.join(case_dir, meta["ignore"])).convert("L")) > 127
    cap = mp.CMP_LONG
    if meta.get("apparent_long_side"):                  # no finer than the photo resolves the work
        cap = min(cap, max(96, 0.6 * meta["apparent_long_side"]))
    wc, hc = mp.cmp_size(Wg, Hg, cap)
    gt_c, _ = grid_sample(mp.srgb_to_lin(gt), None, lambda u, v: (u, v), Wg / wc, wc, hc, Wg, Hg)
    return gt, gt_c, mp.resize_lin(ign.astype(np.float32), (wc, hc)) > 0.01, wc, hc


def compare(src_lin, src_alpha, mapfn, q, gt_lin_c, ign_c, refit, wc, hc, Wg, Hg):
    """Sample a source onto the gt comparison grid (grid_sample) and score it against gt."""
    o, valid = grid_sample(src_lin, src_alpha, mapfn, q, wc, hc, Wg, Hg)
    border = max(1, int(round(0.015 * math.hypot(wc, hc))))
    inner = np.zeros((hc, wc), bool)
    inner[border:hc - border, border:wc - border] = True
    mask = valid & inner & ~ign_c
    res = {"valid_fraction": float(valid.mean()), "scored_fraction": float(mask.mean())}
    if mask.sum() < 50:
        res["error"] = "too little of the work to score"
        return res, None, o, mask
    Yg, Yo = gt_lin_c @ mp.LUM.astype(np.float32), o @ mp.LUM.astype(np.float32)
    good = mask & (Yg > 0.03) & (Yo > 0.03) & (o.max(-1) < 0.98)
    expo = float(np.median(Yg[good] / Yo[good])) if refit and good.sum() > 30 else 1.0
    oe = np.clip(o * expo, 0, 1)
    lab_o, lab_g = mp.lin_to_lab(oe), mp.lin_to_lab(gt_lin_c)
    de = lab_slack_de(lab_o, lab_g, mask)
    res.update(exposure=expo, de_mean=float(de[mask].mean()), de_p95=float(np.percentile(de[mask], 95)))
    # light: per-channel global gains out, then what still varies at low frequency
    g = []
    for c in range(3):
        okc = mask & (gt_lin_c[..., c] > 0.03) & (o[..., c] > 0.005) & (o[..., c] < 0.98)
        g.append(float(np.median(o[..., c][okc] / gt_lin_c[..., c][okc])) if okc.sum() > 30 else 1.0)
    Yop = (o / np.maximum(np.array(g, np.float32), 1e-6)) @ mp.LUM.astype(np.float32)
    region = ndi.binary_erosion(mask, iterations=max(1, border))
    sig = mp.LIGHT_SIGMA * math.hypot(wc, hc)
    # judged where the work is lit (below 0.01 the photo's noise floor lifts the shadows) and away from strong
    # edges (a sub-pixel registration error there moves local means; registration is judged on its own)
    edge = np.hypot(*np.gradient(ndi.gaussian_filter(np.log(Yg + 0.01), 1.0)))
    lit = (mask & (Yg > 0.01)) / (1 + (edge / 0.15) ** 2)
    res["light"] = mp.light_variation(Yop, Yg, lit, sig, region if region.sum() > 100 else mask) \
        if (lit > 0.5).mean() > 0.05 else None
    Cg = np.hypot(lab_g[..., 1], lab_g[..., 2])[mask].mean()
    res["chroma_gt"] = float(Cg)
    res["chroma_ratio"] = float(np.hypot(lab_o[..., 1], lab_o[..., 2])[mask].mean() / Cg) if Cg >= 10 else None
    # contrast on L* smoothed by one grid pixel: resampling blur on thin lines is not a tone change. A few ink
    # lines on a white sheet give no meaningful ratio, so only works with some tonal range are judged.
    core = ndi.binary_erosion(mask, iterations=2)
    Ls_o, Ls_g = smooth_in(lab_o[..., 0], mask), smooth_in(lab_g[..., 0], mask)
    sg = float(Ls_g[core].std()) if core.sum() > 50 else 0.0
    res["tonal_std_gt"] = sg
    res["contrast_ratio"] = float(Ls_o[core].std() / sg) if sg >= TH["tonal_min"] else None
    res["gains"] = g
    return res, de, oe, mask


def smooth_in(a, m, s=1.0):
    """Gaussian of s grid px over the masked pixels only (normalised convolution)."""
    mf = m.astype(np.float64)
    return ndi.gaussian_filter(a * mf, s) / np.maximum(ndi.gaussian_filter(mf, s), 1e-6)


def hf_energy(ch):
    """Fine-detail energy of one log channel, scaled to its own median first: a gain on the channel (exposure,
    a light or white-balance correction) leaves it unchanged, so only a change of detail reads as sharpening."""
    ch = np.clip(ch, 0, None)
    l = np.log(ch / max(float(np.median(ch)), 1e-6) + 0.05)
    return float((ndi.gaussian_laplace(l, 1.0) ** 2).mean())


def sharpen_ratio(out_lin, ref_lin):
    """Fine-detail energy of the output over the reference's, per channel, weighted by each channel's signal
    in the reference (a near-empty blue channel in a red painting is mostly noise)."""
    w = np.maximum(ref_lin.reshape(-1, 3).mean(0, dtype=np.float64), 1e-6)
    r = [hf_energy(out_lin[..., c]) / max(hf_energy(ref_lin[..., c]), 1e-12) for c in range(3)]
    return float(np.dot(w, r) / w.sum())


# --------------------------------------------------------------------------- one case

def _run(full, timeout):
    """Run a command; returns exit code (None on timeout), stdout, stderr and the peak resident memory in bytes
    of the largest process (the child, or a helper it waited for), where the OS reports it (wait4)."""
    if not hasattr(os, "wait4"):
        try:
            p = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout, p.stderr, None
        except subprocess.TimeoutExpired as e:
            so = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return None, so, "timeout after %ds" % timeout, None
    p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    bufs = {}
    readers = [threading.Thread(target=lambda k=k, f=f: bufs.__setitem__(k, f.read()), daemon=True)
               for k, f in (("out", p.stdout), ("err", p.stderr))]
    for t in readers:
        t.start()
    t0, code, rss = time.perf_counter(), None, None
    while True:
        pid, status, ru = os.wait4(p.pid, os.WNOHANG)
        if pid:
            code = os.waitstatus_to_exitcode(status)
            rss = ru.ru_maxrss * (1 if sys.platform == "darwin" else 1024)     # bytes on macOS, KiB on Linux
            break
        if time.perf_counter() - t0 > timeout:
            p.kill()
            os.wait4(p.pid, 0)
            bufs["timeout"] = "timeout after %ds" % timeout
            break
        time.sleep(0.02)
    p.returncode = code if code is not None else -9     # reaped here: keep Popen from waiting again
    for t in readers:
        t.join(10)
    return code, bufs.get("out", ""), bufs.get("timeout") or bufs.get("err", ""), rss


def run_tool(cmd, photo, out, review, args, timeout):
    for p in (out, review):
        if os.path.exists(p):
            os.remove(p)
    full = list(cmd) + [photo, "--out", out, "--review", review] + list(args)
    t0 = time.perf_counter()
    code, so, se, rss = _run(full, timeout)
    dt = time.perf_counter() - t0
    js = None
    txt = (so or "").strip()
    for cand in [txt] + txt.splitlines()[::-1]:
        try:
            v = json.loads(cand)
            if isinstance(v, dict):
                js = v
                break
        except ValueError:
            continue
    return {"cmd": " ".join(shlex.quote(c) for c in full), "exit": code, "json": js, "time_s": round(dt, 3),
            "peak_rss_mb": None if rss is None else round(rss / 2 ** 20, 1), "stderr_tail": (se or "")[-1200:]}


def chk(checks, name, ok, value=None, limit=None, gating=True, note=None):
    checks[name] = {"pass": bool(ok), "value": value, "limit": limit, "gating": bool(gating)}
    if note:
        checks[name]["note"] = note


def contract_issues(js, code, meta, out, review=None):
    bad = []
    if not isinstance(js, dict):
        return ["no JSON object on stdout"]
    if code == 2:
        return ["error: %s" % js.get("error")]
    for k, sub in CONTRACT.items():
        if k not in js:
            bad.append("missing %s" % k)
        elif sub and code == 0:
            if not isinstance(js[k], dict):
                bad.append("%s is not an object" % k)
            else:
                bad += ["missing %s.%s" % (k, s) for s in sub if s not in js[k]]
    if "warnings" in js and not (isinstance(js["warnings"], list) and all(isinstance(w, str) for w in js["warnings"])):
        bad.append("warnings is not a list of strings")
    if js.get("size_in") is not None and list(js["size_in"]) != list(meta["photo_size"]):
        bad.append("size_in %s is not the photo's size after EXIF orientation %s" % (js["size_in"], meta["photo_size"]))
    if code == 0:
        if not os.path.exists(out):
            bad.append("exit 0 but nothing written")
        else:
            with open(out, "rb") as f:
                if f.read(8) != b"\x89PNG\r\n\x1a\n":
                    bad.append("output is not a PNG")
            if js.get("size_out") is not None:
                with Image.open(out) as im:
                    if list(js["size_out"]) != list(im.size):
                        bad.append("size_out %s but the file is %s" % (js["size_out"], list(im.size)))
        if not js.get("written"):
            bad.append("exit 0 but written is empty")
        if review and not os.path.exists(review):
            bad.append("exit 0 but no review sheet at --review")
    if code == 4 and os.path.exists(out):
        bad.append("exit 4 but an output was written")
    return bad


def corner_errors(rep, true, diag):
    rep, true = np.asarray(rep, float), np.asarray(true, float)
    return np.hypot(*(rep - true).T) / diag * 100


def judge_quad(js, meta, checks, gating):
    q = (js.get("quad") or {}).get("corners") if isinstance(js.get("quad"), dict) else None
    vis = meta["expect"].get("visible_corners", [0, 1, 2, 3])
    if not q or len(q) != 4:
        chk(checks, "quad", False, None, TH["quad"], gating, "no quad reported")
        return None
    best, which = None, None
    for name, tq in meta.get("quads", {"gt": meta["corners"]}).items():
        if name == "neighbour":
            continue
        tq = np.asarray(tq, float)
        diag = 0.5 * (np.hypot(*(tq[2] - tq[0])) + np.hypot(*(tq[3] - tq[1])))
        e = corner_errors(q, tq, diag)[vis].max()
        if best is None or e < best:
            best, which = float(e), name
    tq = np.asarray(meta["corners"], float)
    diag = 0.5 * (np.hypot(*(tq[2] - tq[0])) + np.hypot(*(tq[3] - tq[1])))
    perms = [np.roll(np.arange(4), r) for r in range(4)] + [np.roll(np.arange(4)[::-1], r) for r in range(4)]
    errs = [corner_errors(np.asarray(q, float)[p], tq, diag).max() for p in perms]
    order_ok = errs[0] <= min(errs) + 0.5 or best <= TH["quad"]
    chk(checks, "quad", best <= TH["quad"] and order_ok, round(best, 3), TH["quad"], gating,
        ("matches the %s quad" % which) + ("" if order_ok else "; corners not in TL TR BR BL order"))
    return which


def case_report(case_dir, run_dir, cmd, timeout, sheet_dir, honest=True):
    """Run the tool on one case and judge it. honest: one case at a time on the real tool, so the run time and
    memory can be held to the perf targets (otherwise they are reported only)."""
    with open(os.path.join(case_dir, "meta.json")) as f:
        meta = json.load(f)
    name, kind = meta["case"], meta["kind"]
    os.makedirs(run_dir, exist_ok=True)
    out, review = os.path.join(run_dir, "out.png"), os.path.join(run_dir, "review.jpg")
    photo = os.path.join(case_dir, meta["photo"])
    r = run_tool(cmd, photo, out, review, meta.get("args", []), timeout)
    js, code = r["json"], r["exit"]
    exp = meta.get("expect", {})
    gating = kind in ("noop", "correct", "option", "stress", "real")
    checks, metrics, notes = {}, {}, []
    rep = {"case": name, "kind": kind, "what": meta.get("what", ""), "gating": gating, "exit": code,
           "expected_exit": exp.get("exit"), "time_s": r["time_s"], "peak_rss_mb": r["peak_rss_mb"], "cmd": r["cmd"],
           "metrics": metrics, "checks": checks, "notes": notes}
    if js is not None:
        rep["json"] = {k: js.get(k) for k in list(CONTRACT) + ["error"] if k in js}
    if code is None:
        chk(checks, "time", False, r["time_s"], timeout, True, "timed out")
    else:
        chk(checks, "time", True, r["time_s"], timeout, False)
    if exp.get("perf") and code is not None:
        why = None if honest else "parallel run or stand-in: reported only"
        chk(checks, "perf_time", r["time_s"] <= TH["perf_time_s"], r["time_s"], TH["perf_time_s"], gating and honest, why)
        if r["peak_rss_mb"] is not None:
            lim = TH["perf_rss_gb"] * 1024
            chk(checks, "perf_memory", r["peak_rss_mb"] <= lim, r["peak_rss_mb"], lim, gating and honest, why)
    if code not in (0, 4) or js is None:
        rep["stderr_tail"] = r["stderr_tail"]
    if kind == "real":
        chk(checks, "runs", code in (0, 4) and js is not None, code, [0, 4], True)
        rep["verdict"] = "pass" if checks["runs"]["pass"] else "fail"
        sheet(meta, case_dir, out if code == 0 else None, review, None, None, rep, os.path.join(sheet_dir, name + ".jpg"))
        return rep
    allowed = exp.get("exit", [0])
    chk(checks, "exit", code in allowed, code, allowed, gating)
    issues = contract_issues(js, code, meta, out, review)
    chk(checks, "contract", not issues, issues or None, None, gating)
    warns = [w for w in (js or {}).get("warnings", []) or [] if isinstance(w, str)]
    wtext = " ".join(warns)
    alltext = wtext + " " + str((js or {}).get("note") or "")
    for t in exp.get("warnings", []):
        ok = re.search(TOPICS[t], wtext, re.I) is not None
        if exp.get("warn_or_refuse") and code == 4:
            ok = True
        chk(checks, "warn_" + t, ok, warns or None, t, gating)
    for t in exp.get("say_so", []):
        chk(checks, "say_" + t, re.search(TOPICS[t], alltext, re.I) is not None, alltext.strip() or None, t, gating)
    alarms = [t for t, rx in ALARMS.items() if t not in exp.get("warnings", []) and re.search(rx, wtext, re.I)]
    chk(checks, "false_alarm", not alarms, alarms or None, [], gating and kind != "stress")
    jsd = js if isinstance(js, dict) else {}
    if code == 0 and isinstance(jsd.get("light"), dict) and isinstance(jsd.get("color"), dict):
        forced = exp.get("applied", {})
        for part, key in (("light", "light"), ("color", "color")):
            want = forced.get(part)
            if want is None:
                want = {"correct": True, "keep": False}.get(exp.get(key))
            if want is not None:
                got = jsd[part].get("applied")
                chk(checks, "applied_" + part, bool(got) == want, got, want, gating and kind != "stress")
    if exp.get("quad_source") and code == 0:
        src = (jsd.get("quad") or {}).get("source") if isinstance(jsd.get("quad"), dict) else None
        chk(checks, "quad_source", src == exp["quad_source"], src, exp["quad_source"], gating)
    if exp.get("frame") and code == 0 and isinstance(jsd.get("frame"), dict):
        fr = jsd["frame"]
        ok = bool(fr.get("found")) == exp["frame"]["found"] and bool(fr.get("removed")) == exp["frame"]["removed"]
        chk(checks, "frame", ok, {"found": fr.get("found"), "removed": fr.get("removed")}, exp["frame"], gating)

    de_map = None
    if code == 0 and os.path.exists(out) and meta.get("gt"):
        try:
            de_map = measure(meta, case_dir, out, photo, js, checks, metrics, notes, gating)
        except Exception as e:                           # a broken output is the tool's failure, not a crash here
            chk(checks, "measure", False, None, None, gating, "could not measure the output: %r" % e)
    elif exp.get("unchanged") and code == 4:
        metrics["unchanged"] = True
    fails = [k for k, v in checks.items() if v["gating"] and not v["pass"]]
    rep["failures"] = fails
    rep["verdict"] = ("pass" if not fails else "fail") if gating else \
        ("info: " + (", ".join(k for k, v in checks.items() if not v["pass"]) or "all within limits"))
    sheet(meta, case_dir, out if code == 0 and os.path.exists(out) else None, review, de_map, js, rep,
          os.path.join(sheet_dir, name + ".jpg"))
    return rep


def measure(meta, case_dir, out, photo, js, checks, metrics, notes, gating):
    kind, exp = meta["kind"], meta["expect"]
    noop = bool(exp.get("unchanged"))
    geo_gate = gating and kind in ("correct", "option")
    gt = mp.load_flat(os.path.join(case_dir, meta["gt"]))
    Hg, Wg = gt.shape[:2]
    o_srgb, o_alpha = load_rgb(out)
    Ho, Wo = o_srgb.shape[:2]
    ign = np.zeros((Hg, Wg), bool)
    if meta.get("ignore"):
        ign = np.asarray(Image.open(os.path.join(case_dir, meta["ignore"])).convert("L")) > 127
    # geometry
    M, ncc, lvl = align(luma(o_srgb), luma(gt))
    if not (np.all(np.isfinite(M)) and abs(np.linalg.det(M)) > 1e-12 and np.isfinite(ncc)):
        # a blank or garbled output: nothing to align, so nothing else to measure
        chk(checks, "aligned", False, None, TH["ncc"], gating and kind != "stress", "the output does not look like the work")
        return None
    metrics["align_ncc"] = round(ncc, 4)
    aligned = ncc >= TH["ncc"]
    chk(checks, "aligned", aligned, round(ncc, 3), TH["ncc"], gating and kind != "stress",
        None if aligned else "the output does not look like the work")
    Mi = np.linalg.inv(M)
    oc = np.array([[0, 0], [Wo, 0], [Wo, Ho], [0, Ho]], float)
    gx, gy, _ = mp.hom(Mi, oc[:, 0], oc[:, 1])
    gc = np.array([[0, 0], [Wg, 0], [Wg, Hg], [0, Hg]], float)
    vis = exp.get("visible_corners", [0, 1, 2, 3])
    cerr = np.hypot(gx - gc[:, 0], gy - gc[:, 1]) / math.hypot(Wg, Hg) * 100
    metrics["corner_err_pct"] = [round(float(v), 3) for v in cerr]
    corner = float(cerr[vis].max())
    qx, qy, _ = mp.hom(M, gc[:, 0], gc[:, 1])
    q = np.c_[qx, qy]
    wq = 0.5 * (np.hypot(*(q[1] - q[0])) + np.hypot(*(q[2] - q[3])))
    hq = 0.5 * (np.hypot(*(q[3] - q[0])) + np.hypot(*(q[2] - q[1])))
    aspect_err = abs((wq / hq) / (Wg / float(Hg)) - 1) * 100
    metrics["aspect_err_pct"] = round(aspect_err, 3)
    st, st_lens, st_raw, ncons = straightness(lvl, meta, Wg, Hg)
    metrics["straight_pct"] = None if st is None else round(st, 3)
    metrics["lens_bow_left_pct"] = None if st_lens is None else round(st_lens, 3)
    metrics["largest_patch_shift_pct"] = None if st_raw is None else round(st_raw, 3)
    metrics["straight_constraints"] = ncons
    for nm in ("mat", "frame"):
        if nm in meta.get("quads", {}) and corner > TH["corners"]:
            notes.append("output corners off by %.1f%%; compare the drawn quads: the %s may have been kept" % (corner, nm))
    if corner > (TH["noop_corners"] if noop else TH["corners"]):
        # which way each side is off: an inner edge taken for the work's (+), or some surroundings kept (-)
        side = {"top": (gy[0] + gy[1]) / 2 / Hg, "right": 1 - (gx[1] + gx[2]) / 2 / Wg,
                "bottom": 1 - (gy[2] + gy[3]) / 2 / Hg, "left": (gx[0] + gx[3]) / 2 / Wg}
        notes.append("output edges against the work's, + inside it, - beyond it (%% of its width or height): %s%s"
                     % (", ".join("%s %+.1f" % (k, v * 100) for k, v in side.items()),
                        "" if aligned else " (alignment weak: rough)"))
        iq = meta.get("inner_quad")
        if iq:
            x0, y0, x1, y1 = iq["rect"]
            di = np.hypot(gx - np.array([x0, x1, x1, x0]), gy - np.array([y0, y0, y1, y1])) / math.hypot(Wg, Hg) * 100
            if di[vis].max() <= 1.0:
                notes.append("the output is %s, not the whole work" % iq["what"])
    if noop:
        chk(checks, "corners", aligned and corner <= TH["noop_corners"], round(corner, 3), TH["noop_corners"], gating)
    elif exp.get("geometry"):
        chk(checks, "corners", aligned and corner <= TH["corners"], round(corner, 3), TH["corners"], geo_gate)
        tol = exp.get("aspect_tol_pct", 1.0)
        chk(checks, "aspect", aligned and aspect_err <= tol, round(aspect_err, 3), tol, geo_gate)
        if st is not None:
            chk(checks, "straight", aligned and st <= TH["straight"], round(st, 3), TH["straight"], geo_gate)
        long_ratio = max(Wo, Ho) / float(meta.get("expected_long_side") or meta.get("apparent_long_side") or max(Wo, Ho))
        metrics["resolution_ratio"] = round(long_ratio, 3)
        chk(checks, "resolution", TH["res"][0] <= long_ratio <= TH["res"][1], round(long_ratio, 3), TH["res"], geo_gate)
        if js:
            judge_quad(js, meta, checks, geo_gate)
    # colour and light on the comparison grid
    _, gt_c, ign_c, wc, hc = cmp_grid(meta, case_dir, gt, ign)
    o_lin = mp.srgb_to_lin(o_srgb)
    cx, cy, _ = mp.hom(M, np.array([Wg / 2.0, Wg / 2.0 + 1]), np.array([Hg / 2.0, Hg / 2.0]))
    r_out = math.hypot(cx[1] - cx[0], cy[1] - cy[0])            # output px per gt px
    q_out = r_out * Wg / wc
    res, de, oe, mask = compare(o_lin, o_alpha, lambda u, v: mp.hom(M, u, v)[:2], q_out, gt_c, ign_c,
                                not noop, wc, hc, Wg, Hg)
    metrics["after"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items()}
    before = None
    if not noop:
        ph, _ = load_rgb(photo)
        px, py = mp.gt_to_photo(meta, np.array([Wg / 2.0, Wg / 2.0 + 1]), np.array([Hg / 2.0, Hg / 2.0]))
        q_ph = math.hypot(px[1] - px[0], py[1] - py[0]) * Wg / wc
        before, _, r0, _ = compare(mp.srgb_to_lin(ph), None, lambda u, v: mp.gt_to_photo(meta, u, v), q_ph, gt_c, ign_c,
                                   True, wc, hc, Wg, Hg)
        metrics["before"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in before.items()}
    if "error" in res:
        chk(checks, "colour", False, None, None, gating and kind != "stress", res["error"])
        return de
    cgate = gating and kind != "stress" and aligned
    if noop:
        chk(checks, "colour", res["de_mean"] <= TH["noop_de_mean"] and res["de_p95"] <= TH["noop_de_p95"],
            [round(res["de_mean"], 3), round(res["de_p95"], 3)], [TH["noop_de_mean"], TH["noop_de_p95"]], gating)
        if res.get("light") is not None:
            chk(checks, "light", res["light"] <= TH["noop_light"], round(res["light"], 4), TH["noop_light"], gating)
    else:
        chk(checks, "colour", res["de_mean"] <= TH["de_mean"] and res["de_p95"] <= TH["de_p95"],
            [round(res["de_mean"], 3), round(res["de_p95"], 3)], [TH["de_mean"], TH["de_p95"]], cgate)
        if res.get("light") is not None:
            chk(checks, "light", res["light"] <= TH["light"], round(res["light"], 4), TH["light"], cgate)
    # restyling is never accepted, wherever an output was written (no-op cases included)
    if res.get("chroma_ratio") is not None:
        chk(checks, "chroma", TH["chroma"][0] <= res["chroma_ratio"] <= TH["chroma"][1],
            round(res["chroma_ratio"], 3), TH["chroma"], cgate)
    if res.get("contrast_ratio") is not None:
        chk(checks, "contrast", TH["contrast"][0] <= res["contrast_ratio"] <= TH["contrast"][1],
            round(res["contrast_ratio"], 3), TH["contrast"], cgate)
    # sharpening: the output against the photo resampled onto the output's own grid (only means something
    # where the output is the work)
    if aligned:
        ph_lin = mp.srgb_to_lin(load_rgb(photo)[0])
        # a central window at the output's full resolution: subsampling would hide a 1 px unsharp mask
        ww, wh = min(int(0.6 * Wo), 1200), min(int(0.6 * Ho), 1200)
        x0, y0 = (Wo - ww) // 2, (Ho - wh) // 2
        ys, xs = np.mgrid[y0:y0 + wh, x0:x0 + ww] + 0.5
        gu, gv, _ = mp.hom(Mi, xs, ys)
        pxs, pys = mp.gt_to_photo(meta, gu, gv)
        # a sharp honest resampler (no penalty for cubic or Lanczos), on the part of the photo it needs only
        bx0, by0 = max(0, int(np.nanmin(pxs)) - 8), max(0, int(np.nanmin(pys)) - 8)
        bx1, by1 = min(ph_lin.shape[1], int(np.nanmax(pxs)) + 9), min(ph_lin.shape[0], int(np.nanmax(pys)) + 9)
        if bx1 - bx0 >= 16 and by1 - by0 >= 16:
            crop = ph_lin[by0:by1, bx0:bx1]
            refimg = np.stack([ndi.map_coordinates(crop[..., c], [pys - 0.5 - by0, pxs - 0.5 - bx0], order=3,
                                                   mode="nearest") for c in range(3)], -1)
            ratio = sharpen_ratio(o_lin[y0:y0 + wh, x0:x0 + ww], refimg)
            metrics["sharpen_ratio"] = round(ratio, 3)
            chk(checks, "sharpen", ratio <= TH["sharpen"], round(ratio, 3), TH["sharpen"], gating)
    if meta.get("glare_file") and before is not None and "error" not in before:
        core = mp.resize_lin((np.asarray(Image.open(os.path.join(case_dir, meta["glare_file"])).convert("L")) > 127)
                             .astype(np.float32), (wc, hc)) > 0.5
        core &= mask
        if core.sum() > 5:
            Lg = mp.lin_to_lab(gt_c)[..., 0][core].mean()
            Lo = mp.lin_to_lab(oe)[..., 0][core].mean()
            Lp = mp.lin_to_lab(np.clip(r0, 0, 1))[..., 0][core].mean()
            kept = (Lo - Lg) >= 0.4 * (Lp - Lg) if Lp - Lg >= 10 else True
            metrics["glare_L"] = {"gt": round(float(Lg), 1), "photo": round(float(Lp), 1), "output": round(float(Lo), 1)}
            chk(checks, "glare_kept", kept, round(float(Lo - Lg), 1), round(float(0.4 * (Lp - Lg)), 1), gating,
                None if kept else "the glare was painted over: a fake fix")
    return de


# --------------------------------------------------------------------------- sheets and summary

def _fit(im, h):
    return im.resize((max(1, int(im.width * h / im.height)), h), Image.LANCZOS)


def heat(de):
    t = np.clip(de / 10.0, 0, 1)
    rgb = np.stack([np.clip(t * 2.2, 0, 1), np.clip(1.6 - np.abs(t - 0.45) * 3.2, 0, 1) * (t > 0.08), np.clip(0.5 - t * 2, 0, 1)], -1)
    return Image.fromarray(mp.u8(rgb * np.clip(t * 6, 0.15, 1)[..., None]))


def sheet(meta, case_dir, out, review, de, js, rep, path):
    h = 300
    ph = ImageOps.exif_transpose(Image.open(os.path.join(case_dir, meta["photo"]))).convert("RGB")
    k = h / float(ph.height)
    p = _fit(ph, h)
    d = ImageDraw.Draw(p)
    if meta.get("corners") and meta["kind"] != "noop":
        qq = [(x * k, y * k) for x, y in meta["corners"]]
        d.line(qq + qq[:1], fill=(40, 230, 60), width=2)
    q = (js or {}).get("quad", {}).get("corners") if isinstance((js or {}).get("quad"), dict) else None
    if q and len(q) == 4:
        qq = [(float(x) * k, float(y) * k) for x, y in q]
        d.line(qq + qq[:1], fill=(255, 0, 255), width=1)
    panels = [("photo", p)]
    if out:
        panels.append(("output", _fit(Image.open(out).convert("RGB"), h)))
    else:
        blank = Image.new("RGB", (int(h * 0.75), h), (90, 90, 90))
        ImageDraw.Draw(blank).text((10, h // 2 - 8), "exit %s: nothing written" % rep.get("exit"), fill=(255, 255, 255))
        panels.append(("output", blank))
    if meta.get("gt"):
        panels.append(("gt", _fit(Image.open(os.path.join(case_dir, meta["gt"])).convert("RGB"), h)))
    if de is not None:
        panels.append(("CIEDE2000 0..10", _fit(heat(de), h)))
    W = sum(im.width for _, im in panels) + 8 * (len(panels) - 1)
    rows = [panels]
    revim = None
    if review and os.path.exists(review):
        try:
            revim = Image.open(review).convert("RGB")
            revim = revim.resize((min(W, revim.width), max(1, int(revim.height * min(W, revim.width) / revim.width))))
        except OSError:
            revim = None
    head = 54
    sh = Image.new("RGB", (max(W, 900), head + h + 22 + (revim.height + 8 if revim else 0)), (250, 250, 248))
    d = ImageDraw.Draw(sh)
    v = rep.get("verdict", "")
    col = (20, 130, 40) if v == "pass" else ((190, 30, 30) if v == "fail" else (90, 90, 90))
    d.text((8, 6), "%s  [%s]  %s" % (meta["case"], meta["kind"], v), fill=col, font=mp.font(16))
    m = rep.get("metrics", {})
    aft, bef = m.get("after", {}), m.get("before", {})
    bits = ["exit %s (want %s)" % (rep.get("exit"), rep.get("expected_exit")), "%.1fs" % (rep.get("time_s") or 0)]
    if rep.get("peak_rss_mb"):
        bits.append("%.0f MB" % rep["peak_rss_mb"])
    if "corner_err_pct" in m:
        bits.append("corners %.2f%%  aspect %.2f%%  straight %s" % (max(m["corner_err_pct"]), m["aspect_err_pct"],
                                                                     "-" if m.get("straight_pct") is None else "%.2f%%" % m["straight_pct"]))
    if "de_mean" in aft:
        bits.append("dE %.2f/%.1f (before %s)" % (aft["de_mean"], aft["de_p95"], "%.1f" % bef["de_mean"] if "de_mean" in bef else "-"))
    if aft.get("light") is not None:
        bits.append("light %.1f%% (before %s)" % (aft["light"] * 100, "%.0f%%" % (bef["light"] * 100) if bef.get("light") is not None else "-"))
    d.text((8, 28), "   ".join(bits), fill=(40, 40, 40), font=mp.font(12))
    x = 0
    for label, im in panels:
        sh.paste(im, (x, head))
        d.text((x + 4, head + h + 4), label, fill=(60, 60, 60), font=mp.font(11))
        x += im.width + 8
    if revim:
        sh.paste(revim, (0, head + h + 22))
    sh.save(path, "JPEG", quality=85)


def summary_md(reports, meta_run):
    lines = ["# rectify benchmark", "", "tool: `%s`  " % meta_run["tool"], "cases: %d, gating failures: %d, run time %.0f s"
             % (len(reports), sum(1 for r in reports if r["gating"] and r["verdict"] != "pass"), meta_run["wall_s"]), "",
             "| case | kind | exit | corners % | aspect % | straight % | dE mean/p95 (before) | light % (before) | warnings | s | MB | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        m = r.get("metrics", {})
        a, b = m.get("after", {}), m.get("before", {})
        cor = "%.2f" % max(m["corner_err_pct"]) if m.get("corner_err_pct") else ""
        asp = "%.2f" % m["aspect_err_pct"] if m.get("aspect_err_pct") is not None else ""
        st = "%.2f" % m["straight_pct"] if m.get("straight_pct") is not None else ""
        de = "%.2f/%.1f (%s)" % (a["de_mean"], a["de_p95"], "%.1f" % b["de_mean"] if "de_mean" in b else "-") if "de_mean" in a else ""
        li = "%.1f (%s)" % (a["light"] * 100, "%.0f" % (b["light"] * 100) if b.get("light") is not None else "-") if a.get("light") is not None else ""
        w = ", ".join(sorted(set(k[5:] for k, v in r["checks"].items() if k.startswith("warn_") and v["pass"])))
        verdict = r["verdict"] if r["verdict"] == "pass" or not r.get("failures") else "fail: " + ", ".join(r["failures"])
        mb = "%.0f" % r["peak_rss_mb"] if r.get("peak_rss_mb") is not None else ""
        lines.append("| %s | %s | %s→%s | %s | %s | %s | %s | %s | %s | %.1f | %s | %s |" % (
            r["case"], r["kind"], r.get("expected_exit"), r.get("exit"), cor, asp, st, de, li, w, r.get("time_s") or 0, mb,
            verdict))
    notes = [(r["case"], n) for r in reports for n in r.get("notes", [])]
    if notes:
        lines += ["", "## notes", ""] + ["- %s: %s" % n for n in notes]
    return "\n".join(lines) + "\n"


def overview(reports, sheet_dir, path):
    thumbs = []
    for r in reports:
        p = os.path.join(sheet_dir, r["case"] + ".jpg")
        if os.path.exists(p):
            im = Image.open(p).convert("RGB")
            thumbs.append(im.resize((int(im.width * 0.45), int(im.height * 0.45))))
    if not thumbs:
        return
    W = max(t.width for t in thumbs)
    cols = 2
    rows = (len(thumbs) + cols - 1) // cols
    hmax = max(t.height for t in thumbs)
    ov = Image.new("RGB", (W * cols + 10, rows * (hmax + 6)), (255, 255, 255))
    for i, t in enumerate(thumbs):
        ov.paste(t, ((i % cols) * (W + 10), (i // cols) * (hmax + 6)))
    ov.save(path, "JPEG", quality=80)


# --------------------------------------------------------------------------- stand-ins

def flatten_lin(lin, frac=0.08):
    """What a light corrector does when it takes the picture's own light and dark for the lamp's: divide out the
    low-frequency luminance (its scale is relative to the picture, so the self-test can apply it to gt too)."""
    lin = np.asarray(lin, np.float32)
    Y = np.maximum(lin @ mp.LUM.astype(np.float32), 0)     # cubic resampling dips below 0 at dark edges
    low = ndi.gaussian_filter(np.log(Y + 1e-3), frac * max(lin.shape[:2]))
    return lin * np.exp(low.mean() - low).astype(np.float32)[..., None]


def grayworld_lin(lin):
    """Grey world: the three channel means made equal, luminance kept."""
    lin = np.asarray(lin, np.float32)
    m = lin.reshape(-1, 3).mean(0, dtype=np.float64)     # float32 down a column of millions drifts by percents
    return lin / np.maximum(m / max(float(m @ mp.LUM), 1e-6), 1e-6).astype(np.float32)


def enhance_srgb(lin, sat=1.15, con=1.15):
    """'Auto-enhance': saturation up in linear light, contrast up about the median in sRGB; returns sRGB."""
    Y = (lin @ mp.LUM.astype(np.float32))[..., None]
    enc = mp.lin_to_srgb(np.clip(Y + sat * (lin - Y), 0, 1))
    med = float(np.median(enc))
    return np.clip(med + con * (enc - med), 0, 1)


def unsharp_srgb(enc, amount=0.5, sigma=1.0):
    return np.clip(enc + amount * (enc - ndi.gaussian_filter(enc, (sigma, sigma, 0))), 0, 1)


SAY = {"glare": "Glare burns out part of the painting; reshoot at a slight angle to the light.",
       "cut_off": "Part of the work is cut off by the photo's edge; reshoot with all of it in view.",
       "steep": "Very steep view: the far side is foreshortened; reshoot more straight on.",
       "blur": "Motion blur: hold the phone still and reshoot.",
       "resolution": "Too few pixels across the work; move closer and reshoot."}


def standin_main(argv):
    """A fake rectify with the contract's CLI, built from the case's truth: the oracle, or the oracle with
    exactly one fault (see the module docstring), to prove the benchmark."""
    ap = argparse.ArgumentParser(prog="bench.py --as-standin")
    ap.add_argument("name", choices=STANDINS)
    ap.add_argument("photo")
    ap.add_argument("--out", required=True)
    ap.add_argument("--review")
    for o in ("--corners", "--frame", "--lens", "--light", "--color", "--max"):
        ap.add_argument(o)
    a = ap.parse_args(argv)
    nm = a.name
    folder = os.path.dirname(os.path.abspath(a.photo))
    with open(os.path.join(folder, "meta.json")) as f:
        meta = json.load(f)
    exp = meta.get("expect", {})
    ph, _ = load_rgb(a.photo)
    Hp, Wp = ph.shape[:2]
    rep = {"changed": False, "written": None, "review": None, "size_in": [Wp, Hp], "size_out": None,
           "quad": {"corners": None, "source": None, "confidence": None},
           "lens": {"applied": False, "k1": None, "straightness_px_before": None, "straightness_px_after": None},
           "perspective": {"applied": False, "aspect": None, "aspect_method": None, "tilt_deg": None},
           "light": {"applied": False, "reference": "none", "variation_before": None, "variation_after": None},
           "color": {"applied": False, "reference": "none", "gains": None, "cast_before": None, "cast_after": None},
           "frame": {"found": False, "removed": False}, "warnings": [], "note": "stand-in %s" % nm}
    noop = exp.get("unchanged") or meta["kind"] == "real"
    if nm == "identity" or (noop and nm not in RESTYLERS + ("copy",)):
        print(json.dumps(rep))
        sys.exit(4)
    lin = mp.srgb_to_lin(ph)
    want_light = exp.get("light") == "correct" and a.light != "off"       # what the oracle corrects
    want_color = exp.get("color") == "correct" and a.color != "off"
    if nm == "copy" or noop:
        out = lin                                       # the restylers then "fix" the flat picture
    else:
        gw, gh = meta["gt_size"]
        x0, y0, x1, y1 = 0.0, 0.0, float(gw), float(gh)
        if nm == "overcrop":                            # an inner edge taken for the work's: 1.5% short on every side
            x0, y0, x1, y1 = 0.015 * gw, 0.015 * gh, 0.985 * gw, 0.985 * gh
        qx, qy = mp.gt_to_photo(meta, np.array([x0, x1, x1, x0]), np.array([y0, y0, y1, y1]))
        quad = np.c_[qx, qy]
        side = lambda i, j: math.hypot(qx[i] - qx[j], qy[i] - qy[j])
        k = max(side(0, 1) / (x1 - x0), side(3, 2) / (x1 - x0), side(0, 3) / (y1 - y0), side(1, 2) / (y1 - y0))
        ow, oh = max(8, round((x1 - x0) * k)), max(8, round((y1 - y0) * k))
        if nm == "stretch":                             # resampled to the wrong shape: corners exact, 3% too wide
            ow = round(ow * 1.03)
        Hc = mp.fit_homography(np.array([[0, 0], [ow, 0], [ow, oh], [0, oh]], float), quad)
        tr = np.load(os.path.join(folder, "truth.npz"))
        Es, Gs = tr["E"].astype(np.float32), tr["G"].astype(np.float32)
        fix_light = want_light and nm not in ("geometry", "nolight", "overcorrect")
        fix_color = want_color and nm not in ("geometry", "grayworld", "overcorrect")
        order = 1 if nm == "overcorrect" else 3
        coef = [ndi.spline_filter(lin[..., c], order=3, mode="mirror") if order == 3 else lin[..., c] for c in range(3)]
        out = np.zeros((oh, ow, 3), np.float32)
        for r0 in range(0, oh, 256):
            ys, xs = np.mgrid[r0:min(oh, r0 + 256), 0:ow] + 0.5
            if nm == "nolens":
                x, y, _ = mp.hom(Hc, xs, ys)
            else:
                x, y = mp.gt_to_photo(meta, x0 + xs * (x1 - x0) / ow, y0 + ys * (y1 - y0) / oh)
            smp = np.stack([ndi.map_coordinates(coef[c], [y - 0.5, x - 0.5], order=order, mode="mirror", prefilter=False)
                            for c in range(3)], -1)
            smp[(x < 0) | (y < 0) | (x > Wp) | (y > Hp)] = 0      # beyond the photo (a cut-off corner)
            ex, ey = x * Es.shape[1] / Wp - 0.5, y * Es.shape[0] / Hp - 0.5
            if fix_light:
                smp /= np.maximum(ndi.map_coordinates(Es, [ey, ex], order=1, mode="nearest"), 1e-3)[..., None]
            if fix_color:
                smp /= np.maximum(np.stack([ndi.map_coordinates(Gs[..., c], [ey, ex], order=1, mode="nearest")
                                            for c in range(3)], -1), 1e-3)
            out[r0:r0 + len(ys)] = smp
        if fix_light or fix_color:              # rescale without clipping a small bright passage (a moon)
            out *= 0.97 / max(float(np.percentile(out.max(-1), 99.99)), 1e-6)
        rep["quad"] = {"corners": quad.tolist(), "source": "given" if a.corners else "edges", "confidence": 1.0}
        k1 = meta["camera"]["k1"]
        lens = nm != "nolens" and abs(k1) > 0
        rep["lens"].update(applied=lens, k1=k1 if lens else None)
        rep["perspective"] = {"applied": True, "aspect": ow / float(oh), "aspect_method": "focal",
                              "tilt_deg": meta["camera"].get("tilt_deg")}
        ev = (meta.get("evidence") or {}).get("available") or []
        refname = "margin" if ("margin" in ev or "mat" in ev) else ("surround" if ev else "none")
        # flags as the oracle's: geometry and nolight claim corrections they did not make
        rep["light"].update(applied=want_light, reference=refname if want_light else "none")
        rep["color"].update(applied=want_color, reference=refname if want_color else "none",
                            gains=[round(1 / g, 4) for g in meta["gains"]["g"]] if want_color else None)
        frame = bool(meta.get("quads", {}).get("frame"))
        rep["frame"] = {"found": frame, "removed": frame and a.frame != "keep"}
    # the restyles
    if nm in ("grayworld", "overcorrect") and a.color != "off":
        out = grayworld_lin(out)
        rep["color"].update(applied=True, reference="none")
    if nm in ("flatten", "overcorrect") and a.light != "off":
        out = flatten_lin(out)
        rep["light"].update(applied=True, reference="none")
    if nm in ("grayworld", "flatten", "overcorrect") and not noop:
        out *= min(1.0, 0.97 / max(float(np.percentile(out.max(-1), 99.99)), 1e-6))
    if nm == "sharpen":
        enc = unsharp_srgb(mp.lin_to_srgb(np.clip(out, 0, 1)))
    elif nm == "enhance":
        enc = enhance_srgb(out)
    elif nm == "overcorrect":
        enc = unsharp_srgb(enhance_srgb(out, 1.25, 1.0), 0.8, 1.2)
    else:
        enc = mp.lin_to_srgb(np.clip(out, 0, 1))
    if nm not in ("copy", "overcorrect"):               # the oracle's words
        rep["warnings"] = [SAY[t] for t in exp.get("warnings", [])]
        if exp.get("say_so") and nm != "grayworld":
            rep["note"] = "No neutral reference in view: colour left as shot."
    Image.fromarray(mp.u8(enc)).save(a.out, "PNG")
    rep.update(changed=True, written=a.out, size_out=[enc.shape[1], enc.shape[0]])
    if a.review:
        th = _fit(Image.fromarray(mp.u8(mp.lin_to_srgb(lin))), 240)
        oo = _fit(Image.fromarray(mp.u8(enc)), 240)
        rv = Image.new("RGB", (th.width + oo.width + 8, 240), (255, 255, 255))
        rv.paste(th, (0, 0))
        rv.paste(oo, (th.width + 8, 0))
        rv.save(a.review, "JPEG", quality=80)
        rep["review"] = a.review
    print(json.dumps(rep))
    sys.exit(0)


# --------------------------------------------------------------------------- runner

def _work(job):
    case_dir, run_dir, cmd, timeout, sheet_dir, honest = job
    try:
        return case_report(case_dir, run_dir, cmd, timeout, sheet_dir, honest)
    except Exception as e:                            # the bench itself failed on this case: report it loudly
        import traceback
        return {"case": os.path.basename(case_dir), "kind": "?", "gating": True, "verdict": "fail",
                "failures": ["bench_error"], "checks": {"bench_error": {"pass": False, "gating": True, "value": repr(e)}},
                "metrics": {}, "notes": [traceback.format_exc()[-1500:]], "time_s": 0}


def list_cases(root, pats):
    idx = os.path.join(root, "index.json")
    if os.path.exists(idx):
        with open(idx) as f:
            names = json.load(f)["cases"]
    else:
        names = sorted(d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "meta.json")))
    names = [n for n in names if os.path.exists(os.path.join(root, n, "meta.json"))]
    if pats:
        names = [n for n in names if any(fnmatch.fnmatch(n, p) for p in pats)]
    return names


def run_all(root, report_dir, cmd, label, pats, jobs, timeout, quiet=False, standin=False):
    names = list_cases(root, pats)
    if not names:
        raise SystemExit("bench: no cases in %s (make them with make_photos.py)" % root)
    sheet_dir = os.path.join(report_dir, "sheets")
    os.makedirs(sheet_dir, exist_ok=True)
    honest = jobs == 1 and not standin
    if jobs > 1:    # one BLAS thread per process: a dozen spinning threads in each of N workers swamp the machine
        for v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ.setdefault(v, "1")
    jobs_ = [(os.path.join(root, n), os.path.join(report_dir, "runs", n), cmd, timeout, sheet_dir, honest) for n in names]
    t0 = time.perf_counter()
    reports = []
    if jobs > 1:
        with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
            for r in ex.map(_work, jobs_):
                reports.append(r)
                if not quiet:
                    sys.stderr.write("%-28s %s\n" % (r["case"], r["verdict"]))
    else:
        for j in jobs_:
            r = _work(j)
            reports.append(r)
            if not quiet:
                sys.stderr.write("%-28s %s\n" % (r["case"], r["verdict"]))
    wall = time.perf_counter() - t0
    gate = [r for r in reports if r["gating"]]
    times = [r["time_s"] for r in reports if r.get("time_s")]
    rss = [r["peak_rss_mb"] for r in reports if r.get("peak_rss_mb")]
    summ = {"tool": label, "cases": len(reports), "gating": len(gate), "passed": sum(r["verdict"] == "pass" for r in gate),
            "failed": [r["case"] for r in gate if r["verdict"] != "pass"], "wall_s": round(wall, 1),
            "tool_time_median_s": round(float(np.median(times)), 2) if times else None,
            "tool_time_max_s": round(float(max(times)), 2) if times else None,
            "tool_peak_rss_mb_max": round(float(max(rss)), 1) if rss else None, "honest_timing": honest, "thresholds": TH}
    with open(os.path.join(report_dir, "report.json"), "w") as f:
        json.dump({"summary": summ, "cases": reports}, f, indent=1, ensure_ascii=False, default=str)
    with open(os.path.join(report_dir, "summary.md"), "w") as f:
        f.write(summary_md(reports, summ))
    overview(reports, sheet_dir, os.path.join(report_dir, "overview.jpg"))
    return summ, reports


def check_model(root):
    """Self-checks of the yardsticks: CIEDE2000 against Sharma's published pairs; the camera model against
    each case's stored corners."""
    pairs = [((50, 2.6772, -79.7751), (50, 0, -82.7485), 2.0425), ((50, 3.1571, -77.2803), (50, 0, -82.7485), 2.8615),
             ((50, 0, 0), (50, -1, 2), 2.3669), ((50, 2.5, 0), (73, 25, -18), 27.1492),
             ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
             ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
             ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
             ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441)]
    worst = max(abs(float(mp.delta_e2000(np.array(a, float), np.array(b, float))) - e) for a, b, e in pairs)
    problems = [] if worst < 1e-3 else ["CIEDE2000 off by %.4f on Sharma's pairs" % worst]
    for n in list_cases(root, None):
        with open(os.path.join(root, n, "meta.json")) as f:
            meta = json.load(f)
        if meta["kind"] in ("noop", "real") or not meta.get("gt"):
            continue
        gw, gh = meta["gt_size"]
        x, y = mp.gt_to_photo(meta, np.array([0, gw, gw, 0.0]), np.array([0, 0, gh, float(gh)]))
        err = np.abs(np.c_[x, y] - np.asarray(meta["corners"])).max()
        X, Y, _ = mp.photo_to_plane(meta, x, y)
        G = np.array(meta["camera"]["gt_to_plane"])
        back = np.abs(np.c_[X, Y] - np.c_[G[0, 2] + np.array([0, gw, gw, 0]), G[1, 2] + np.array([0, 0, gh, gh])]).max()
        if err > 0.05 or back > 0.05:
            problems.append("%s: corners off by %.3f px, round trip %.3f" % (n, err, back))
    return problems, worst


def predict(meta, case_dir):
    """How the restyles whose effect depends on the work (grayworld, flatten, enhance) score when applied to gt
    itself, noise-free, against the case's own limits: the share of each limit used (over 1 fails). The
    stand-ins apply them to an output that is otherwise the oracle's, so this says where they must show."""
    _, gt_c, ign_c, wc, hc = cmp_grid(meta, case_dir)
    noop = bool(meta.get("expect", {}).get("unchanged"))
    lim_de = (TH["noop_de_mean"], TH["noop_de_p95"]) if noop else (TH["de_mean"], TH["de_p95"])
    lim_light = TH["noop_light"] if noop else TH["light"]
    out = {}
    for nm, fn in (("grayworld", grayworld_lin), ("flatten", flatten_lin),
                   ("enhance", lambda l: mp.srgb_to_lin(enhance_srgb(l)))):
        o = fn(gt_c)
        if nm != "enhance" and not noop:            # as the stand-in: rescaled rather than clipped
            o = o * min(1.0, 0.97 / max(float(np.percentile(o.max(-1), 99.99)), 1e-6))
        res, _, _, _ = compare(np.clip(o, 0, 1), None, lambda u, v: (u, v), 1.0, gt_c, ign_c, not noop,
                               wc, hc, wc, hc)
        if "error" in res:
            continue
        use = {"colour": max(res["de_mean"] / lim_de[0], res["de_p95"] / lim_de[1])}
        if res.get("light") is not None:
            use["light"] = res["light"] / lim_light
        for key, (lo, hi) in (("chroma", TH["chroma"]), ("contrast", TH["contrast"])):
            v = res.get(key + "_ratio")
            if v is not None:
                use[key] = max((1 - v) / (1 - lo), (v - 1) / (hi - 1))
        out[nm] = {k: round(v, 3) for k, v in use.items()}
    return out


def wants(meta, case_dir):
    """What each stand-in must score on this case if the benchmark measures what it claims: "pass"; None (no
    expectation); or (must, may): some check in `must` fails (the right reason) and every check that fails is
    in `may` (None: anything). Also returns predict()'s numbers."""
    e, kind = meta.get("expect", {}), meta["kind"]
    noop = bool(e.get("unchanged"))
    warn = {"warn_" + t for t in e.get("warnings", [])}
    args = meta.get("args", [])
    off = {args[i][2:] for i in range(len(args) - 1) if args[i] in ("--light", "--color") and args[i + 1] == "off"}
    restyle = {"colour", "light", "chroma", "contrast", "sharpen"}
    w, pred = {"oracle": "pass"}, {}
    if kind == "stretch":                   # never gating: only the oracle is held to every check
        return w, pred
    if noop:
        w.update({n: "pass" for n in ("identity", "copy", "geometry", "nolens", "nolight", "overcrop", "stretch")})
    elif kind == "stress":                  # gating here: exit, contract, the warning, sharpening, glare kept
        w["identity"] = "pass" if e.get("warn_or_refuse") else (warn, warn)
        w["copy"] = (warn, None)
        w.update({n: "pass" for n in ("geometry", "nolens", "nolight", "overcrop", "stretch", "grayworld")})
        w["flatten"] = None if meta.get("glare_file") else "pass"     # flattening glare paints it over: either
        w["enhance"] = None
    else:
        lf, cf = e.get("light") == "correct", e.get("color") == "correct"
        say = {"say_" + t for t in e.get("say_so", [])}
        w["identity"] = ({"exit"}, {"exit"} | say)
        w["copy"] = ({"corners", "aligned"}, None)
        w["geometry"] = ({"light", "colour", "chroma"}, {"light", "colour", "chroma", "contrast"}) if lf or cf else "pass"
        w["nolens"] = (({"straight", "corners"}, {"straight", "corners", "aspect", "colour", "light"})
                       if e.get("lens") == "correct" else "pass")
        w["nolight"] = ({"light"}, {"light", "colour", "chroma", "contrast"}) if lf else "pass"
        w["overcrop"] = ({"corners"}, {"corners", "quad"})
        w["stretch"] = ({"aspect"}, {"aspect", "resolution"})
    w["sharpen"] = ({"sharpen"}, {"sharpen", "contrast"})
    w["overcorrect"] = (restyle | warn | {"applied_light", "applied_color", "say_no_color_reference"}, None)
    if kind != "stress":
        pred = predict(meta, case_dir)
        # on a flat picture nothing is rescaled, so grey world or flattening may clip highlights, which changes
        # detail too; on a photo they are rescaled, and the sharpen check must ignore their gains
        clip = {"sharpen"} if noop else set()
        for nm, must, may in (("grayworld", {"colour", "chroma"}, {"colour", "chroma", "contrast", "light"} | clip),
                              ("flatten", {"light", "contrast"}, {"light", "contrast", "colour", "chroma"} | clip),
                              ("enhance", restyle, restyle)):
            use = pred.get(nm) or {}
            if (nm == "grayworld" and "color" in off) or (nm == "flatten" and "light" in off):
                w[nm] = "pass"              # options are honoured: nothing to restyle
            elif nm == "grayworld" and e.get("color") == "keep":
                w[nm] = ({"applied_color", "colour", "chroma", "say_no_color_reference"},
                         may | {"applied_color", "say_no_color_reference"})
            elif use and max(use.values()) > 1.25:
                w[nm] = (must, may)
            else:
                w[nm] = None                # too small to show on this work: no expectation
    return w, pred


def self_test(root, report_dir, pats, jobs, timeout):
    problems, worst = check_model(root)
    results = {}
    for nm in STANDINS:
        cmd = [sys.executable, os.path.abspath(__file__), "--as-standin", nm]
        summ, reps = run_all(root, os.path.join(report_dir, "standin-" + nm), cmd, "standin " + nm, pats, jobs, timeout,
                             quiet=True, standin=True)
        results[nm] = {r["case"]: r for r in reps}
        sys.stderr.write("stand-in %-11s %d/%d gating cases pass (%.0f s)\n" % (nm, summ["passed"], summ["gating"], summ["wall_s"]))
    lines, tally, preds = [], {nm: [0, 0, 0] for nm in STANDINS}, {}
    for n in results["oracle"]:
        with open(os.path.join(root, n, "meta.json")) as f:
            meta = json.load(f)
        if meta["kind"] == "real":
            continue
        want, preds[n] = wants(meta, os.path.join(root, n))
        row = []
        for nm, w in want.items():
            r = results[nm].get(n)
            if r is None:
                continue
            if meta["kind"] == "stretch":
                fails = {k for k, v in r["checks"].items() if not v["pass"]}
            else:
                fails = set(r.get("failures") or [])
            got = "+".join(sorted(fails)) if fails else "pass"
            broken = fails & {"contract", "measure", "bench_error"}     # never acceptable, from any stand-in
            if w is None:
                ok = not broken
            elif w == "pass":
                ok = not fails
                tally[nm][1] += 1
            else:
                must, may = w
                ok = bool(fails & must) and (may is None or fails <= may) and not broken
                tally[nm][0] += 1
            if not ok:
                tally[nm][2] += 1
                problems.append("%s with stand-in %s: got %s, wanted %s" % (
                    n, nm, got, "pass" if w == "pass" else "no bench error" if w is None else
                    "a failure of %s%s" % ("/".join(sorted(w[0])), "" if w[1] is None else ", nothing outside "
                                           + "/".join(sorted(w[1])))))
            row.append("%s:%s%s" % (nm, got if len(got) < 40 else got[:37] + "...", "" if ok else " (WRONG)"))
        lines.append("%-28s %s" % (n, "  ".join(row)))
    summary = ["%-11s must fail on %2d cases, must pass on %2d, wrong on %d" % (nm, t[0], t[1], t[2]) for nm, t in tally.items()]
    with open(os.path.join(report_dir, "self-test.txt"), "w") as f:
        f.write("\n".join(lines) + "\n\n" + "\n".join(summary) + "\n\n"
                + ("\n".join(problems) or "benchmark discriminates as designed") + "\n")
    with open(os.path.join(report_dir, "predictions.json"), "w") as f:
        json.dump(preds, f, indent=1)
    sys.stderr.write("\n".join(lines) + "\n\n" + "\n".join(summary) + "\n")
    out = {"self_test": "pass" if not problems else "fail", "ciede2000_worst_error": worst, "problems": problems,
           "matrix": os.path.join(report_dir, "self-test.txt")}
    print(json.dumps(out, indent=1))
    sys.exit(0 if not problems else 1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--as-standin":
        return standin_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", help="folder written by make_photos.py")
    ap.add_argument("--out", required=True, help="report folder (outside the skill)")
    ap.add_argument("--tool", help="command to benchmark instead of picture.py rectify (same CLI)")
    ap.add_argument("--standin", choices=STANDINS, help="benchmark a built-in stand-in instead")
    ap.add_argument("--self-test", action="store_true", help="run every stand-in and check the benchmark discriminates")
    ap.add_argument("--cases", dest="only", help="comma-separated glob patterns of case names")
    ap.add_argument("--jobs", type=int, default=1, help="cases in parallel (1 keeps the run times honest)")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per run")
    a = ap.parse_args()
    root, rd = os.path.abspath(a.cases), os.path.abspath(a.out)
    if not os.path.isdir(root):
        ap.error("no such folder: %s" % root)
    if os.path.commonpath([os.path.realpath(rd), os.path.realpath(SKILL)]) == os.path.realpath(SKILL):
        ap.error("--out must be outside the skill folder")
    os.makedirs(rd, exist_ok=True)
    pats = [p.strip() for p in a.only.split(",")] if a.only else None
    if a.self_test:
        return self_test(root, rd, pats, max(a.jobs, min(6, os.cpu_count() or 2)), a.timeout)
    if a.standin:
        cmd, label = [sys.executable, os.path.abspath(__file__), "--as-standin", a.standin], "standin " + a.standin
    elif a.tool:
        cmd, label = shlex.split(a.tool), a.tool
    else:
        cmd, label = TOOL, " ".join(TOOL[1:])
    summ, reps = run_all(root, rd, cmd, label, pats, a.jobs, a.timeout, standin=bool(a.standin))
    print(json.dumps({k: summ[k] for k in ("tool", "cases", "gating", "passed", "failed", "wall_s", "tool_time_median_s",
                                           "tool_time_max_s", "tool_peak_rss_mb_max")}
                     | {"report": os.path.join(rd, "report.json"), "summary": os.path.join(rd, "summary.md")}))
    sys.exit(0 if not summ["failed"] else 1)


if __name__ == "__main__":
    main()
