#!/usr/bin/env python3
"""Simulated casual phone photos of paintings, with exact ground truth, for benchmarking
`picture.py rectify` (bench.py runs it; README.md explains the numbers).

  python3 make_photos.py --out DIR [--seed N] [--extra PATH ...] [--real PATH ...] [--only GLOB]

Every case gets a folder DIR/<case>/ with
  photo.jpg   what the phone saved (photo.png for lossless no-op cases; extras keep their format)
  gt.png      the artwork exactly as rectify should return it (lossless sRGB)
  meta.json   every parameter, the true corners in the photo, the gt -> photo mapping, what must
              happen (expect) and what must not (expect.must_not)
  ignore.png  only where some gt pixels cannot be recovered from the photo (glare, fingers, the part
              outside the photo): white pixels are left out of the colour and light scores
  glare.png   the stress-glare case only: where the glare burns the paint out
  truth.npz   light field and colour gains in photo space at 1/8 size, for bench.py's oracle

Forward model, in linear light: the artwork (with its paper margin, mat, frame, and the wall, table
or hands round it) lies on a plane; a pinhole camera at a plausible viewpoint sees the plane through
a homography; radial lens distortion moves the image points; the plane is lit by a lamp falloff, a
broad hotspot and/or a soft shadow, the lens vignettes; a von Kries colour cast (a blackbody lamp the
camera's white balance did not fully remove) and auto exposure scale it; then optical blur (or camera
shake), sensor noise, ISP chroma smoothing and sharpening, JPEG. Corners are in photo pixels after
EXIF orientation, continuous coordinates (the image spans 0..w, 0..h).

--extra PATH  a local flat picture used as an extra ground-truth artwork: adds a no-op case (the file
              itself must pass through unchanged) and a case photographed on a wall. Local only:
              never commit extras or generated folders.
--real PATH   a real casual photo without ground truth: bench.py runs it and shows it, unscored.

Everything is drawn here (or comes from the skill's own example); nothing copyrighted. Seeded and
reproducible: the same --seed gives the same files on the same Python, numpy and Pillow, and every
case draws from its own seed stream, so adding a case changes no other case.
"""
import argparse
import fnmatch
import json
import math
import os
import re
import shutil
import sys
import zlib

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy import ndimage as ndi

sys.dont_write_bytecode = True     # the skill folder may be shared or read-only: no __pycache__ inside it

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(os.path.dirname(HERE))
NIGHT_HOUSE = os.path.join(SKILL, "examples", "night-house", "night-house.png")
VERSION = "photo-bench 1"

# Yardsticks shared with bench.py, so expectations are set with the rule they are judged by.
LIGHT_TOL = 0.04        # residual low-frequency light variation over the work (bench gate)
DE_TOL = 3.0            # mean CIEDE2000 where a correction is expected (bench gate)
STRAIGHT_TOL = 0.35     # % of the gt diagonal a straight line may still bow after the best homography
NEUTRAL_CHROMA = 3.0    # C*ab at or below which a surface is near-neutral evidence
FIT_OK = 0.025          # an expected light correction must be reachable from the evidence to this residual
CMP_LONG = 400          # long side of the comparison grid for colour and light (px)
LIGHT_SIGMA = 0.04      # low-pass for the light metric, as a fraction of the comparison diagonal
FULL_FRAME_DIAG = 43.27  # mm; 35 mm-equivalent focal lengths are defined on the diagonal


# --------------------------------------------------------------------------- colour

M_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                      [0.2126729, 0.7151522, 0.0721750],
                      [0.0193339, 0.1191920, 0.9503041]])
WHITE = M_RGB2XYZ.sum(1)
LUM = M_RGB2XYZ[1]


def _rows(fn, v):
    """fn over a big image a band of rows at a time (the same arithmetic, a fraction of the temporaries)."""
    if v.ndim < 2 or v.size < 4_000_000:
        return fn(v)
    out = np.empty(v.shape, np.float32)
    step = max(1, 1_000_000 // max(1, v[0].size))
    for i in range(0, v.shape[0], step):
        out[i:i + step] = fn(v[i:i + step])
    return out


def srgb_to_lin(v):
    return _rows(lambda v: np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4).astype(np.float32),
                 np.asarray(v, np.float32))


def lin_to_srgb(v):
    return _rows(lambda v: (lambda c: np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)
                            .astype(np.float32))(np.clip(v, 0, 1)), np.asarray(v, np.float32))


def lin_to_lab(rgb):
    xyz = np.asarray(rgb, np.float64) @ M_RGB2XYZ.T / WHITE
    e = (6 / 29) ** 3
    f = np.where(xyz > e, np.cbrt(np.maximum(xyz, e)), xyz / (3 * (6 / 29) ** 2) + 4 / 29)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def chroma_srgb(c):
    lab = lin_to_lab(srgb_to_lin(np.asarray(c, np.float32)))
    return float(np.hypot(lab[..., 1], lab[..., 2]))


def delta_e2000(lab1, lab2):
    """CIEDE2000 (Sharma, Wu and Dalal 2005), vectorised."""
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    c7 = ((np.hypot(a1, b1) + np.hypot(a2, b2)) / 2) ** 7
    g = 0.5 * (1 - np.sqrt(c7 / (c7 + 25.0 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    zero = (c1p * c2p) == 0
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(zero, 0, dh)
    dL, dC = L2 - L1, c2p - c1p
    dH = 2 * np.sqrt(c1p * c2p) * np.sin(np.radians(dh / 2))
    Lm, Cm = (L1 + L2) / 2, (c1p + c2p) / 2
    hs = h1p + h2p
    hm = np.where(np.abs(h1p - h2p) <= 180, hs / 2, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2))
    hm = np.where(zero, hs, hm)
    t = (1 - 0.17 * np.cos(np.radians(hm - 30)) + 0.24 * np.cos(np.radians(2 * hm))
         + 0.32 * np.cos(np.radians(3 * hm + 6)) - 0.20 * np.cos(np.radians(4 * hm - 63)))
    dth = 30 * np.exp(-((hm - 275) / 25) ** 2)
    cm7 = Cm ** 7
    rc = 2 * np.sqrt(cm7 / (cm7 + 25.0 ** 7))
    sl = 1 + 0.015 * (Lm - 50) ** 2 / np.sqrt(20 + (Lm - 50) ** 2)
    sc = 1 + 0.045 * Cm
    sh = 1 + 0.015 * Cm * t
    rt = -np.sin(np.radians(2 * dth)) * rc
    return np.sqrt((dL / sl) ** 2 + (dC / sc) ** 2 + (dH / sh) ** 2 + rt * (dC / sc) * (dH / sh))


def kelvin_rgb(T):
    """Linear sRGB of a blackbody white of luminance 1 (Kim et al. 2002 Planckian locus fit)."""
    T = float(T)
    if T <= 4000:
        x = -0.2661239e9 / T ** 3 - 0.2343589e6 / T ** 2 + 0.8776956e3 / T + 0.179910
    else:
        x = -3.0258469e9 / T ** 3 + 2.1070379e6 / T ** 2 + 0.2226347e3 / T + 0.240390
    if T <= 2222:
        y = -1.1063814 * x ** 3 - 1.34811020 * x ** 2 + 2.18555832 * x - 0.20219683
    elif T <= 4000:
        y = -0.9549476 * x ** 3 - 1.37418593 * x ** 2 + 2.09137015 * x - 0.16748867
    else:
        y = 3.0817580 * x ** 3 - 5.87338670 * x ** 2 + 3.75112997 * x - 0.37001483
    rgb = np.linalg.solve(M_RGB2XYZ, np.array([x / y, 1.0, (1 - x - y) / y]))
    return rgb / (rgb @ LUM)


def cast_gains(t_scene, t_camera):
    """von Kries gains a lamp of t_scene leaves when the camera balanced for t_camera; white keeps luminance."""
    g = kelvin_rgb(t_scene) / kelvin_rgb(t_camera)
    return g / (g @ LUM)


# --------------------------------------------------------------------------- shared measures

def light_variation(y_out, y_ref, mask, sigma, region=None):
    """exp(P98 - P2) - 1 of log(G(y_out) / G(y_ref)), G a Gaussian of `sigma` over the masked pixels: how much
    the light still varies over the work at the scale of lamps and shadows. Local means are compared, so blur,
    noise and resampling, which keep local means, do not count, nor do dark passages next to bright ones."""
    m = np.asarray(mask, np.float64)
    num = ndi.gaussian_filter(np.nan_to_num(np.asarray(y_out, np.float64)) * m, sigma, mode="constant")
    den = ndi.gaussian_filter(np.nan_to_num(np.asarray(y_ref, np.float64)) * m, sigma, mode="constant")
    sup = ndi.gaussian_filter(m, sigma, mode="constant")
    ok = (sup > 0.25 * max(sup.max(), 1e-12)) & (num > 1e-7) & (den > 1e-7)
    if region is not None:
        ok &= region
    if ok.sum() < 25:
        return None
    lo, hi = np.percentile(np.log(num[ok] / den[ok]), [2, 98])
    return float(np.exp(hi - lo) - 1)


def cmp_size(w, h, cap=CMP_LONG):
    k = min(1.0, cap / float(max(w, h)))
    return max(8, int(round(w * k))), max(8, int(round(h * k)))


def resize_lin(a, size):
    """Area-correct resize of a float image (H, W[, C]) to size=(w, h), via Pillow's float mode."""
    a = np.asarray(a, np.float32)
    w, h = size
    if a.shape[1] == w and a.shape[0] == h:
        return a.copy()
    flt = Image.BOX if (w < a.shape[1] and h < a.shape[0]) else Image.BILINEAR
    if a.ndim == 2:
        return np.asarray(Image.fromarray(a, "F").resize((w, h), flt), np.float32)
    return np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c]), "F").resize((w, h), flt),
                                np.float32) for c in range(a.shape[2])], -1)


# --------------------------------------------------------------------------- camera model

def hom(Hm, x, y):
    w = Hm[2, 0] * x + Hm[2, 1] * y + Hm[2, 2]
    return (Hm[0, 0] * x + Hm[0, 1] * y + Hm[0, 2]) / w, (Hm[1, 0] * x + Hm[1, 1] * y + Hm[1, 2]) / w, w


def distort(xu, yu, k1, c, rn):
    """x_d = c + (x_u - c)(1 + k1 r^2), r = |x_u - c| / rn (rn = half the photo diagonal)."""
    dx, dy = (xu - c[0]) / rn, (yu - c[1]) / rn
    f = 1 + k1 * (dx * dx + dy * dy)
    return c[0] + dx * f * rn, c[1] + dy * f * rn


def undistort(xd, yd, k1, c, rn, iters=12):
    dx, dy = (xd - c[0]) / rn, (yd - c[1]) / rn
    ux, uy = dx, dy
    for _ in range(iters):
        f = 1 + k1 * (ux * ux + uy * uy)
        ux, uy = dx / f, dy / f
    return c[0] + ux * rn, c[1] + uy * rn


def lens_of(meta):
    cam = meta["camera"]
    return cam["k1"], cam["center"], cam["rnorm"]


def gt_to_photo(meta, u, v):
    """gt pixel coordinates (continuous) -> photo coordinates after EXIF orientation, through the true model."""
    cam = meta["camera"]
    X, Y, _ = hom(np.array(cam["gt_to_plane"]), u, v)
    xu, yu, _ = hom(np.array(cam["H"]), X, Y)
    k1, c, rn = lens_of(meta)
    return distort(xu, yu, k1, c, rn)


def photo_to_plane(meta, x, y):
    cam = meta["camera"]
    k1, c, rn = lens_of(meta)
    xu, yu = undistort(x, y, k1, c, rn)
    return hom(np.linalg.inv(np.array(cam["H"])), xu, yu)


def fit_homography(src, dst):
    """Least-squares homography src -> dst (N x 2 each, N >= 4), normalised DLT."""
    src, dst = np.asarray(src, np.float64), np.asarray(dst, np.float64)

    def norm(p):
        m = p.mean(0)
        s = math.sqrt(2) / max(1e-12, np.sqrt(((p - m) ** 2).sum(1)).mean())
        return np.array([[s, 0, -s * m[0]], [0, s, -s * m[1]], [0, 0, 1]])
    Ts, Td = norm(src), norm(dst)
    ps = np.c_[src, np.ones(len(src))] @ Ts.T
    pd = np.c_[dst, np.ones(len(dst))] @ Td.T
    A = []
    for (x, y, _), (u, v, _) in zip(ps, pd):
        A.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        A.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.asarray(A))
    Hn = vt[-1].reshape(3, 3)
    Hm = np.linalg.inv(Td) @ Hn @ Ts
    return Hm / Hm[2, 2]


def look_at(f, wh, target, tilt, azim, roll, dist):
    """Pinhole camera `dist` from the plane point `target`, tilted `tilt` degrees off the plane normal
    towards azimuth `azim`, rolled `roll` degrees; returns H (plane -> undistorted photo px) and the centre."""
    th, ph, ro = np.radians([tilt, azim, roll])
    T = np.array([target[0], target[1], 0.0])
    C = T + dist * np.array([math.sin(th) * math.cos(ph), math.sin(th) * math.sin(ph), -math.cos(th)])
    z = (T - C) / np.linalg.norm(T - C)
    x = np.cross(z, [0.0, -1.0, 0.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    x, y = math.cos(ro) * x + math.sin(ro) * y, -math.sin(ro) * x + math.cos(ro) * y
    R = np.stack([x, y, z])
    t = -R @ C
    K = np.array([[f, 0, wh[0] / 2.0], [0, f, wh[1] / 2.0], [0, 0, 1.0]])
    Hm = K @ np.column_stack([R[:, 0], R[:, 1], t])
    return Hm / Hm[2, 2], C


def zhang_aspect(m, f, c):
    """Width/height of a rectangle from its undistorted image corners m = [TL, TR, BR, BL], the focal length f
    (px; None estimates it from the two vanishing points) and principal point c (Zhang & He 2007)."""
    tl, tr, br, bl = [np.array([p[0], p[1], 1.0]) for p in m]
    m1, m2, m3, m4 = tl, tr, bl, br
    d2 = np.dot(np.cross(m2, m4), m3)
    d3 = np.dot(np.cross(m3, m4), m2)
    if abs(d2) < 1e-12 or abs(d3) < 1e-12:
        return None, None
    k2 = np.dot(np.cross(m1, m4), m3) / d2
    k3 = np.dot(np.cross(m1, m4), m2) / d3
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    u0, v0 = c
    if f is None:
        den = n2[2] * n3[2]
        if abs(den) < 1e-12:
            return None, None
        f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + n2[2] * n3[2] * u0 * u0)
               + (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + n2[2] * n3[2] * v0 * v0)) / den
        if not f2 > 0:
            return None, None
        f = math.sqrt(f2)
    Ai = np.linalg.inv(np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]]))
    q2, q3 = Ai @ n2, Ai @ n3
    return math.sqrt(float(q2 @ q2) / float(q3 @ q3)), f


# --------------------------------------------------------------------------- noise and helpers

def sstep(t):
    """Smooth step from 0 at t = -1 to 1 at t = +1."""
    u = np.clip((np.asarray(t) + 1) * 0.5, 0, 1)
    return u * u * (3 - 2 * u)


def u8(a):
    return (np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8)


def fbm(rng, h, w, scale, octaves=4, gain=0.5):
    """Smooth fractal noise, zero mean and unit std; `scale` is the size of the largest features (px)."""
    out = np.zeros((h, w), np.float32)
    amp, s = 1.0, float(scale)
    for _ in range(octaves):
        gh, gw = max(2, int(round(h / s)) + 2), max(2, int(round(w / s)) + 2)
        g = rng.standard_normal((gh, gw)).astype(np.float32)
        out += amp * np.asarray(Image.fromarray(g, "F").resize((w, h), Image.BICUBIC), np.float32)
        amp *= gain
        s /= 2.0
        if s < 1.0:
            break
    out -= out.mean()
    return out / (out.std() + 1e-8)


def fbm1(rng, n, scale, octaves=3):
    return fbm(rng, 1, n, scale, octaves)[0]


def tile_noise(rng, n=256, f0=8.0, bw=0.6):
    """Seamless n x n noise tile, unit std, with about f0 features across the tile."""
    white = rng.standard_normal((n, n))
    fy = np.fft.fftfreq(n)[:, None] * n
    fx = np.fft.fftfreq(n)[None, :] * n
    f = np.hypot(fx, fy) + 1e-6
    t = np.real(np.fft.ifft2(np.fft.fft2(white) * np.exp(-0.5 * (np.log(f / f0) / bw) ** 2)))
    return ((t - t.mean()) / t.std()).astype(np.float32)


def tex(tile, X, Y, period, s, f0=8.0):
    """A seamless tile sampled at plane coordinates (one tile per `period` plane units); faded out where its
    features would span under ~3 photo pixels, so far or small textures do not alias."""
    n = tile.shape[0]
    fade = np.clip((period / f0 / np.maximum(s, 1e-6) - 1.5) / 1.5, 0, 1)
    v = ndi.map_coordinates(tile, [np.asarray(Y, np.float64) / period * n, np.asarray(X, np.float64) / period * n],
                            order=1, mode="grid-wrap")
    return v.astype(np.float32) * fade


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow without FreeType: a fixed bitmap font, still deterministic
        return ImageFont.load_default()


# --------------------------------------------------------------------------- artworks (sRGB floats 0..1)

def strokes(img, rng, n, length, width, angle, jitter, ss=2):
    """Paint n short tapered strokes over img, each in the colour under it plus jitter, along angle(x, y);
    returns the painting and an impasto height map (strokes laid over strokes)."""
    h, w = img.shape[:2]
    can = Image.fromarray(u8(img)).resize((w * ss, h * ss), Image.BICUBIC)
    hm = Image.new("L", (w * ss, h * ss), 128)
    d, dh = ImageDraw.Draw(can), ImageDraw.Draw(hm)
    xs, ys = rng.uniform(0, w, n), rng.uniform(0, h, n)
    lens, wids = rng.uniform(length[0], length[1], n), rng.uniform(width[0], width[1], n)
    jl = rng.normal(0, jitter, n)
    jc = rng.normal(0, jitter * 0.35, (n, 3))
    ja = rng.normal(0, 0.25, n)
    hv = rng.integers(40, 255, n)
    for i in range(n):
        x, y = xs[i], ys[i]
        c = img[min(h - 1, int(y)), min(w - 1, int(x))] * (1 + jl[i]) + jc[i]
        a = angle(x, y) + ja[i]
        ca, sa = math.cos(a), math.sin(a)
        L, W = lens[i] * ss, wids[i] * ss
        pts = []
        for u, v in ((-0.5, 0), (-0.3, 0.5), (0.3, 0.45), (0.5, 0), (0.3, -0.45), (-0.3, -0.5)):
            pts.append((x * ss + u * L * ca - v * W * sa, y * ss + u * L * sa + v * W * ca))
        d.polygon(pts, fill=tuple(int(v) for v in u8(c)))
        dh.polygon(pts, fill=int(hv[i]))
    out = np.asarray(can.resize((w, h), Image.LANCZOS), np.float32) / 255
    hmap = np.asarray(hm.resize((w, h), Image.BILINEAR), np.float32) / 255
    return out, hmap


def impasto(img, hmap, k=0.05, weave=0.012):
    """Light the stroke relief from the upper left and add a faint canvas weave (linear light)."""
    h, w = img.shape[:2]
    hb = ndi.gaussian_filter(hmap, 0.8)
    gy, gx = np.gradient(hb)
    shade = 1 + k * np.clip((-gx - gy) * 6, -1.5, 1.5)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    shade *= 1 + weave * np.sin(xx * 2.1) * np.sin(yy * 2.1)
    return lin_to_srgb(srgb_to_lin(img) * shade[..., None])


def ramp(stops, t):
    """Piecewise-linear colour ramp; stops = [(t, (r, g, b)), ...] with t increasing."""
    ts = np.array([s[0] for s in stops])
    cs = np.array([s[1] for s in stops], np.float32)
    return np.stack([np.interp(t, ts, cs[:, c]) for c in range(3)], -1).astype(np.float32)


def art_nighthouse():
    return np.asarray(Image.open(NIGHT_HOUSE).convert("RGB"), np.float32) / 255, None


def art_print(img, rng, border=0.05):
    """The picture as a print with a white paper border; the border is part of the work."""
    h, w = img.shape[:2]
    b = int(round(border * min(w, h)))
    paper = np.array([0.953, 0.951, 0.946], np.float32)
    out = np.empty((h + 2 * b, w + 2 * b, 3), np.float32)
    grain = 1 + 0.008 * fbm(rng, h + 2 * b, w + 2 * b, 2.5, 2)
    out[:] = lin_to_srgb(srgb_to_lin(paper)[None, None] * grain[..., None])
    out[b:b + h, b:b + w] = img
    neutral = np.ones(out.shape[:2], bool)
    neutral[b - 3:b + h + 3, b - 3:b + w + 3] = False
    return out, neutral


def art_wash(rng, w=1500, h=1100):
    """Watercolour landscape on white cotton paper inside a taped rectangle: the paper margin is part of
    the work (and the natural neutral reference)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    paper = srgb_to_lin(np.array([0.957, 0.953, 0.941], np.float32))
    grain = 1 + 0.010 * fbm(rng, h, w, 2.5, 2) + 0.006 * fbm(rng, h, w, 30, 3)
    m = int(0.075 * min(w, h))
    rag = 1.3 * fbm(rng, h, w, 5, 2)
    din = np.minimum(np.minimum(xx - m, (w - m) - xx), np.minimum(yy - m, (h - m) - yy)) + rag
    inside = np.clip(din + 0.5, 0, 1)
    hz = 0.55 * h + 0.012 * h * fbm1(rng, w, 260)
    D = np.zeros((h, w, 3), np.float32)

    def add(dens, trans, gran=0.0, rim=0.0):
        dens = np.clip(dens, 0, None)
        if rim:
            dens = dens + rim * np.clip(dens - ndi.gaussian_filter(dens, 3.0), 0, None) * 3
        if gran:
            dens = dens * (1 + gran * fbm(rng, h, w, 1.6, 2))
        D[...] += dens[..., None] * (-np.log(np.asarray(trans, np.float32)))[None, None]

    t = np.clip(yy / hz[None, :], 0, 1.2)
    add(np.clip(1 - t, 0, 1) ** 1.25 * (0.95 + 0.20 * fbm(rng, h, w, 140, 3)), (0.34, 0.62, 0.86))
    add(0.55 * np.exp(-((yy - hz[None, :]) / (0.10 * h)) ** 2) * (yy < hz[None, :] + 0.02 * h), (0.96, 0.80, 0.50))
    far = hz - 0.05 * h - 0.07 * h * np.abs(fbm1(rng, w, 380))
    farm = sstep((yy - far[None, :]) / 2.0) * (1 - sstep((yy - hz[None, :] - 0.01 * h) / 2.0))
    add(0.85 * farm, (0.66, 0.60, 0.74), gran=0.35, rim=0.6)
    lake0, lake1 = hz + 0.01 * h, hz + 0.12 * h + 0.02 * h * fbm1(rng, w, 200)
    lakem = sstep((yy - lake0[None, :]) / 2.0) * (1 - sstep((yy - lake1[None, :]) / 3.0))
    streak = 0.5 + 0.5 * np.sin(yy * 0.9 + 3 * fbm(rng, h, w, 40, 2))
    add(lakem * (0.25 + 0.25 * streak), (0.40, 0.66, 0.86))
    near = lake1 + 0.015 * h * fbm1(rng, w, 120)
    nearm = sstep((yy - near[None, :]) / 1.5)
    add(nearm * (0.9 + 0.35 * (yy - near[None, :]) / h + 0.15 * fbm(rng, h, w, 60, 3)), (0.48, 0.64, 0.30), rim=0.5)
    add(nearm * np.clip((yy - 0.82 * h) / (0.18 * h), 0, 1) * 0.9, (0.62, 0.48, 0.34))
    trees = np.zeros((h, w), np.float32)
    for _ in range(int(rng.integers(6, 10))):
        cx = rng.uniform(0.12, 0.88) * w
        cy = np.interp(cx, np.arange(w), near) - rng.uniform(0.0, 0.03) * h
        for _ in range(int(rng.integers(5, 9))):
            r = rng.uniform(0.012, 0.03) * w
            px, py = cx + rng.normal(0, r), cy - abs(rng.normal(0, 1.4 * r))
            trees = np.maximum(trees, sstep((r - np.hypot(xx - px, (yy - py) * 1.2)) / 3.0))
    add(1.25 * trees * (1 + 0.25 * fbm(rng, h, w, 8, 2)), (0.30, 0.45, 0.26), rim=0.8)
    hx, hy = rng.uniform(0.25, 0.7) * w, None
    hy = float(np.interp(hx, np.arange(w), near)) + 0.01 * h
    house = ((np.abs(xx - hx) < 0.025 * w) & (yy > hy - 0.035 * h) & (yy < hy)).astype(np.float32)
    roof = ((yy < hy - 0.035 * h) & (yy > hy - 0.035 * h - 0.025 * w + np.abs(xx - hx) * 0.9)
            & (np.abs(xx - hx) < 0.03 * w)).astype(np.float32)
    add(ndi.gaussian_filter(house, 0.8) * 0.7, (0.86, 0.78, 0.66))
    add(ndi.gaussian_filter(roof, 0.8) * 1.2, (0.74, 0.40, 0.30))
    lin = paper[None, None] * grain[..., None] * np.exp(-D * inside[..., None])
    neutral = (din < -3) & (xx > 2) & (yy > 2) & (xx < w - 3) & (yy < h - 3)
    return lin_to_srgb(lin), neutral


def art_oil(rng, w=1000, h=1250, gradient=True):
    """Oil on canvas with brush strokes and impasto. gradient=True: a strong deliberate value gradient
    (a light sky above, a near-black foreground below) that is the painting's own, not the lamp's."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    hz = 0.47 * h + 0.015 * h * fbm1(rng, w, 300)
    t = yy / h
    if gradient:
        base = ramp([(0.0, (0.80, 0.86, 0.93)), (0.28, (0.93, 0.88, 0.72)), (0.44, (0.96, 0.74, 0.44)),
                     (0.50, (0.44, 0.34, 0.22)), (0.72, (0.17, 0.14, 0.10)), (1.0, (0.06, 0.05, 0.05))],
                    t + (0.47 - hz[None, :] / h))
        tl = hz - 0.02 * h - 0.05 * h * np.abs(fbm1(rng, w, 90))
        trees = sstep((yy - tl[None, :]) / 1.5) * (1 - sstep((yy - hz[None, :] - 0.03 * h) / 2))
        base = base * (1 - 0.8 * trees[..., None]) + 0.8 * trees[..., None] * np.array([0.12, 0.11, 0.08])
        rx = 0.5 * w + 0.18 * w * np.sin(yy / h * 7 + 1.3) * (yy / h)
        river = sstep((0.02 * w + 0.12 * w * (yy - hz[None, :]) / h - np.abs(xx - rx)) / 3) * (yy > hz[None, :] + 0.01 * h)
        base = base * (1 - 0.6 * river[..., None]) + 0.6 * river[..., None] * np.array([0.62, 0.55, 0.45])
        ang = lambda x, y: 0.0 if y < 0.47 * h else 0.6 * math.sin(x / w * 6)
    else:
        base = ramp([(0.0, (0.40, 0.58, 0.84)), (0.42, (0.70, 0.80, 0.90)), (0.47, (0.34, 0.46, 0.28)),
                     (0.56, (0.42, 0.54, 0.30)), (0.62, (0.82, 0.70, 0.36)), (1.0, (0.70, 0.56, 0.26))],
                    t + (0.47 - hz[None, :] / h))
        bx, by = rng.uniform(0.25, 0.65) * w, 0.58 * h
        barn = (np.abs(xx - bx) < 0.07 * w) & (yy > by - 0.06 * h) & (yy < by + 0.02 * h)
        roof = (yy <= by - 0.06 * h) & (yy > by - 0.06 * h - 0.05 * w + np.abs(xx - bx) * 0.7) & (np.abs(xx - bx) < 0.08 * w)
        base[barn] = (0.80, 0.74, 0.62)
        base[roof] = (0.72, 0.18, 0.12)
        for _ in range(int(rng.integers(3, 6))):
            cx, cy, r = rng.uniform(0.05, 0.95) * w, hz.mean() + rng.uniform(-0.02, 0.05) * h, rng.uniform(0.03, 0.06) * w
            m = sstep((r - np.hypot(xx - cx, (yy - cy) * 1.3)) / 2)
            base = base * (1 - m[..., None]) + m[..., None] * np.array([0.20, 0.32, 0.16])
        ang = lambda x, y: 0.0 if y < 0.45 * h else -0.3
    base = np.clip(base * (1 + 0.04 * fbm(rng, h, w, 50, 3))[..., None], 0, 1)
    img, hmap = strokes(base, rng, int(w * h / 110), (10, 30), (3, 8), ang, 0.05)
    return impasto(img, hmap), None


def art_red(rng, w=900, h=1200):
    """A colour-field painting in reds only: crimson, cadmium, orange-red, deep red; no neutral pixel."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    base = np.empty((h, w, 3), np.float32)
    base[:] = (0.80, 0.11, 0.08)
    for (y0, y1, col) in ((0.07, 0.36, (0.62, 0.04, 0.12)), (0.42, 0.62, (0.92, 0.32, 0.07)),
                          (0.68, 0.93, (0.44, 0.03, 0.07))):
        edge = 6 * fbm(rng, h, w, 40, 3)
        dist = np.minimum(np.minimum(yy - y0 * h, y1 * h - yy), np.minimum(xx - 0.08 * w, 0.92 * w - xx)) + edge
        m = sstep(dist / 9.0)
        base = base * (1 - m[..., None]) + m[..., None] * np.array(col, np.float32)
    base *= (1 + 0.05 * fbm(rng, h, w, 60, 3))[..., None]
    img, hmap = strokes(np.clip(base, 0, 1), rng, int(w * h / 100), (12, 34), (4, 9),
                        lambda x, y: 0.15 * math.sin(y / h * 9), 0.05)
    img = impasto(img, hmap, k=0.04)
    r = np.clip(img[..., 0], 0.38, 1)          # keep it red everywhere: no grey or white can appear
    img = np.stack([r, np.minimum(img[..., 1], 0.36 * r), np.minimum(img[..., 2], 0.30 * r)], -1)
    return img.astype(np.float32), None


def art_dark(rng, w=1100, h=850):
    """A nocturne: deep blue night, a small moon, black trees and water, two warm windows; mostly very dark."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    hz = 0.62 * h
    base = ramp([(0.0, (0.05, 0.06, 0.11)), (0.62, (0.12, 0.14, 0.22)), (0.63, (0.05, 0.06, 0.09)), (1.0, (0.03, 0.035, 0.05))], yy / h)
    mx, my, mr = rng.uniform(0.55, 0.8) * w, rng.uniform(0.15, 0.28) * h, 0.022 * w
    glow = np.exp(-np.hypot(xx - mx, yy - my) / (0.09 * w))
    base += glow[..., None] * np.array([0.10, 0.10, 0.09]) * (yy < hz)[..., None]
    disc = sstep((mr - np.hypot(xx - mx, yy - my)) / 1.5)
    base = base * (1 - disc[..., None]) + disc[..., None] * np.array([0.86, 0.84, 0.70])
    refl = np.exp(-((xx - mx) / (0.02 * w)) ** 2) * (yy > hz) * (0.5 + 0.5 * np.sin(yy * 0.7)) * np.exp(-(yy - hz) / (0.25 * h))
    base += refl[..., None] * np.array([0.30, 0.29, 0.24])
    for side in (0, 1):
        tl = 0.35 * h + 0.12 * h * fbm1(rng, w, 50)
        edge = (xx < 0.22 * w) if side == 0 else (xx > 0.80 * w)
        m = sstep((yy - tl[None, :]) / 2) * edge * sstep((hz + 0.02 * h - yy) / 2)
        base = base * (1 - m[..., None]) + m[..., None] * np.array([0.025, 0.03, 0.025])
    cx = rng.uniform(0.3, 0.45) * w
    cot = (np.abs(xx - cx) < 0.06 * w) & (yy > hz - 0.07 * h) & (yy < hz)
    base[cot] = (0.06, 0.055, 0.06)
    for k in (-1, 1):
        win = (np.abs(xx - cx - k * 0.025 * w) < 0.009 * w) & (np.abs(yy - hz + 0.04 * h) < 0.012 * h)
        base[win] = (0.86, 0.62, 0.28)
    base *= (1 + 0.06 * fbm(rng, h, w, 40, 3))[..., None]
    img, hmap = strokes(np.clip(base, 0, 1), rng, int(w * h / 140), (10, 26), (3, 7),
                        lambda x, y: 0.0 if y < hz else 0.1, 0.06)
    return impasto(img, hmap, k=0.03), None


def art_figure(rng, w=1200, h=1600):
    """A small ink figure with a red umbrella on a big plain sheet: the plain ground is part of the work."""
    paper = srgb_to_lin(np.array([0.945, 0.941, 0.930], np.float32))
    grain = 1 + 0.009 * fbm(rng, h, w, 2.5, 2) + 0.005 * fbm(rng, h, w, 40, 3)
    lin = paper[None, None] * grain[..., None]
    ss = 3
    ink = Image.new("L", (w * ss, h * ss), 0)
    red = Image.new("L", (w * ss, h * ss), 0)
    d, dr = ImageDraw.Draw(ink), ImageDraw.Draw(red)
    fx, fy, fh = rng.uniform(0.5, 0.66) * w, rng.uniform(0.62, 0.72) * h, 0.10 * h
    S = lambda x, y: (x * ss, y * ss)
    lw = int(0.004 * h * ss)
    d.ellipse([S(fx - 0.06 * fh, fy - fh)[0], S(0, fy - fh)[1], S(fx + 0.06 * fh, 0)[0], S(0, fy - 0.88 * fh)[1]], fill=255)
    d.line([S(fx, fy - 0.88 * fh), S(fx + 0.02 * fh, fy - 0.45 * fh)], fill=255, width=lw * 2)
    d.line([S(fx + 0.02 * fh, fy - 0.45 * fh), S(fx - 0.12 * fh, fy)], fill=255, width=lw)
    d.line([S(fx + 0.02 * fh, fy - 0.45 * fh), S(fx + 0.16 * fh, fy - 0.02 * fh)], fill=255, width=lw)
    d.line([S(fx, fy - 0.8 * fh), S(fx + 0.14 * fh, fy - 1.05 * fh)], fill=255, width=lw)
    d.line([S(fx + 0.14 * fh, fy - 1.05 * fh), S(fx + 0.14 * fh, fy - 1.35 * fh)], fill=255, width=lw // 2 + 1)
    dr.pieslice([S(fx - 0.18 * fh, fy - 1.58 * fh)[0], S(0, fy - 1.58 * fh)[1], S(fx + 0.46 * fh, 0)[0], S(0, fy - 1.06 * fh)[1]],
                180, 360, fill=255)
    for _ in range(3):
        bx, by, bs = rng.uniform(0.15, 0.6) * w, rng.uniform(0.12, 0.3) * h, rng.uniform(0.008, 0.014) * w
        d.line([S(bx - bs, by - 0.4 * bs), S(bx, by), S(bx + bs, by - 0.5 * bs)], fill=200, width=max(1, lw // 2))
    sx, sy = 0.66 * w, 0.95 * h
    pts = [S(sx + i * 0.004 * w, sy + 0.004 * w * math.sin(i * 1.7)) for i in range(14)]
    d.line(pts, fill=150, width=max(1, lw // 2))
    inkm = np.asarray(ink.resize((w, h), Image.LANCZOS), np.float32) / 255
    redm = np.asarray(red.resize((w, h), Image.LANCZOS), np.float32) / 255
    lin = lin * (1 - inkm[..., None]) + inkm[..., None] * srgb_to_lin(np.array([0.14, 0.12, 0.12], np.float32))
    lin = lin * (1 - redm[..., None]) + redm[..., None] * srgb_to_lin(np.array([0.80, 0.12, 0.10], np.float32))
    neutral = ndi.binary_dilation((inkm + redm) > 0.02, iterations=int(0.02 * w)) == 0
    return lin_to_srgb(lin), neutral


def art_screenshot(rng):
    """A flat screenshot of a painting with a share-platform style mark in the lower right (a generic pill
    and an ID line, no real brand)."""
    img, _ = art_oil(rng, 1080, 1350, gradient=False)
    h, w = img.shape[:2]
    im = Image.fromarray(u8(img)).convert("RGBA")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    ph = int(0.034 * h)
    pw = int(3.2 * ph)
    x1, y1 = w - int(0.035 * w), h - int(0.07 * h)
    d.rounded_rectangle([x1 - pw, y1 - ph, x1, y1], radius=ph // 2, fill=(255, 255, 255, 135))
    d.text((x1 - pw / 2, y1 - ph / 2), "app", fill=(70, 70, 70, 230), font=font(int(ph * 0.6)), anchor="mm")
    f2 = font(int(0.021 * h))
    d.text((x1 + 1, y1 + int(0.014 * h) + 1), "ID: 2718281828", fill=(0, 0, 0, 70), font=f2, anchor="ra")
    d.text((x1, y1 + int(0.014 * h)), "ID: 2718281828", fill=(255, 255, 255, 215), font=f2, anchor="ra")
    return np.asarray(Image.alpha_composite(im, ov).convert("RGB"), np.float32) / 255, None


def load_flat(path, max_side=None):
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    if max_side and max(im.size) > max_side:
        k = max_side / float(max(im.size))
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return np.asarray(im, np.float32) / 255


def make_art(key, rng, extras):
    if key == "nighthouse":
        return art_nighthouse()
    if key == "print":
        return art_print(art_nighthouse()[0], rng)
    if key == "wash":
        return art_wash(rng)
    if key == "oilgrad":
        return art_oil(rng, gradient=True)
    if key == "oil":
        return art_oil(rng, 1200, 950, gradient=False)
    if key == "oilbig":                         # enough detail to fill a 48 MP photo without looking soft
        return art_oil(rng, 3200, 2520, gradient=False)
    if key == "red":
        return art_red(rng)
    if key == "dark":
        return art_dark(rng)
    if key == "figure":
        return art_figure(rng)
    if key == "screenshot":
        return art_screenshot(rng)
    if key.startswith("extra:"):
        return load_flat(extras[key[6:]], 1600), None
    raise ValueError("unknown artwork " + key)


# --------------------------------------------------------------------------- scene on the plane

def rect_cov(X, Y, r, s):
    """Anti-aliased coverage of the axis-aligned rectangle r = (x0, y0, x1, y1)."""
    d = np.minimum(np.minimum(X - r[0], r[2] - X), np.minimum(Y - r[1], r[3] - Y))
    return np.clip(0.5 + d / s, 0, 1)


def ellipse_cov(X, Y, cx, cy, a, b, ang, s):
    ca, sa = math.cos(ang), math.sin(ang)
    dx, dy = X - cx, Y - cy
    u, v = (dx * ca + dy * sa) / a, (-dx * sa + dy * ca) / b
    rho = np.sqrt(u * u + v * v)
    return np.clip(0.5 + (1 - rho) * min(a, b) / s, 0, 1), rho, v


def soft_box(X, Y, r, soft):
    return (sstep((X - r[0]) / soft) * sstep((r[2] - X) / soft) * sstep((Y - r[1]) / soft) * sstep((r[3] - Y) / soft))


def frame_colour(fr, X, Y, inner, s, tiles, S):
    """A moulding round `inner`: profile across its width, mitred corners, lit from the upper left."""
    x0, y0, x1, y1 = inner
    stack = np.stack([y0 - Y, X - x1, Y - y1, x0 - X])          # top, right, bottom, left
    t = stack.max(0)
    side = stack.argmax(0)
    u = np.clip(t / fr["w"], 0, 1)
    along = np.where((side == 0) | (side == 2), X, Y)
    base = np.asarray(fr["rgb"], np.float32)
    if fr["kind"] == "gold":
        prof = np.interp(u, [0, .06, .12, .2, .35, .5, .62, .72, .85, .95, 1], [.9, 1.4, 1., .7, .6, .8, 1.35, 1.15, .85, .7, .55])
        sheen = 1 + 0.06 * tex(tiles[1], along, t * 7, S * 0.05, s)
        side_g = np.array([1.15, 0.85, 0.78, 1.06], np.float32)[side]
    elif fr["kind"] == "black":
        prof = np.interp(u, [0, .12, .25, .7, .85, 1], [.8, 2.4, 1., 1., 1.7, .7])
        sheen = 1 + 0.03 * tex(tiles[1], along, t, S * 0.05, s)
        side_g = np.array([1.2, 0.9, 0.8, 1.1], np.float32)[side]
    else:                                                          # wood
        prof = np.interp(u, [0, .1, .3, .8, 1], [.85, 1.25, 1., 1.05, .75])
        grain = np.sin(2 * math.pi * (t / (fr["w"] * 0.23)) + 2.5 * tex(tiles[2], along * 0.15, t, S * 0.08, s))
        sheen = 1 + 0.12 * grain
        side_g = np.array([1.12, 0.88, 0.82, 1.05], np.float32)[side]
    g = (prof * sheen * side_g)[..., None]
    return np.clip(base[None, None] * g, 0, 1.5)


def background(sc, X, Y, s):
    bg, S, tl = sc["bg"], sc["S"], sc["tiles"]
    base = np.asarray(bg["rgb"], np.float32)
    k = bg["kind"]
    if k in ("wall", "teal"):
        v = 1 + 0.012 * tex(tl[0], X, Y, 0.03 * S, s) + 0.006 * tex(tl[1], X, Y, 0.25 * S, s) + 0.004 * tex(tl[2], X, Y, 3 * S, s)
        return base[None, None] * v[..., None]
    if k == "plaster":
        n = tex(tl[1], X, Y, 0.12 * S, s)
        gy = tex(tl[1], X, Y + 0.002 * S, 0.12 * S, s) - n
        v = 1 + 0.03 * n - 0.25 * gy + 0.01 * tex(tl[0], X, Y, 0.02 * S, s)
        return base[None, None] * v[..., None]
    if k == "stone":
        sp = tex(tl[0], X, Y, 0.02 * S, s)
        v = 1 + 0.05 * np.tanh(2 * (sp - 1.2)) * (sp > 0.8) - 0.04 * (sp < -1.6) + 0.015 * tex(tl[1], X, Y, 0.4 * S, s)
        return base[None, None] * v[..., None]
    if k == "wood":
        a = bg["angle"]
        across = X * math.cos(a) + Y * math.sin(a)
        along = -X * math.sin(a) + Y * math.cos(a)
        plank = np.floor(across / (0.28 * S))
        hsh = np.sin(plank * 12.9898) * 43758.5453
        hsh = hsh - np.floor(hsh)
        ring = np.sin(2 * math.pi * (across / (0.011 * S) + 2.0 * tex(tl[2], along * 0.2, across, 0.5 * S, s)))
        mix = np.clip(0.5 + 0.5 * ring, 0, 1) ** 1.5 * np.clip(0.004 * S / np.maximum(s, 1e-6), 0, 1)
        c = (np.asarray(bg["rgb"], np.float32)[None, None] * (0.85 + 0.3 * hsh[..., None])
             * (1 - 0.25 * mix[..., None]) * (1 + 0.04 * tex(tl[0], X, Y, 0.05 * S, s))[..., None])
        seam = np.abs(across / (0.28 * S) - np.round(across / (0.28 * S))) * 0.28 * S
        return c * (1 - 0.6 * np.clip(1 - seam / np.maximum(0.0015 * S, 0.7 * s), 0, 1))[..., None]
    raise ValueError(k)


def shade(sc, X, Y, s):
    """Albedo (linear RGB) of the plane at (X, Y), plus the coverage of things on the plane (hands case:
    the rest is the room) and a class map: 0 none, 1 wall/table, 2 wall in the frame's shadow, 3 frame,
    4 mat, 5 artwork, 6 artwork's own neutral ground (paper margin), 7 hands, 8 another painting."""
    S = sc["S"]
    Wa, Ha = sc["W"], sc["H"]
    shp = X.shape
    cls = np.zeros(shp, np.uint8)
    if sc["bg"]["kind"] == "room":
        col = np.zeros(shp + (3,), np.float32)
        cover = np.zeros(shp, np.float32)
    else:
        col = background(sc, X, Y, s)
        cover = np.ones(shp, np.float32)
        cls[:] = 1
    obj = sc["outer"]
    if sc.get("shadow"):
        sh = sc["shadow"]
        r = (obj[0] + sh["dx"], obj[1] + sh["dy"], obj[2] + sh["dx"], obj[3] + sh["dy"])
        f = 1 - sh["alpha"] * soft_box(X, Y, r, sh["soft"])
        col *= f[..., None]
        cls[(f < 0.985) & (cls == 1)] = 2
    nb = sc.get("neighbour")
    if nb:
        o = nb["outer"]
        cf = rect_cov(X, Y, o, s)
        if cf.any():
            fc = frame_colour(nb["frame"], X, Y, nb["rect"], s, sc["tiles"], S)
            col = col * (1 - cf[..., None]) + fc * cf[..., None]
            ca = rect_cov(X, Y, nb["rect"], s)
            idx = ca > 0
            if idx.any():
                smp = sample_art(nb["art"], X[idx] - nb["rect"][0], Y[idx] - nb["rect"][1])
                col[idx] = col[idx] * (1 - ca[idx, None]) + smp * ca[idx, None]
            cls[cf > 0.5] = 8
    fr, mt = sc.get("frame"), sc.get("mat")
    mat_outer = (-sc["mw"], -sc["mw"], Wa + sc["mw"], Ha + sc["mw"])
    if fr:
        cf = rect_cov(X, Y, obj, s)
        fc = frame_colour(fr, X, Y, mat_outer, s, sc["tiles"], S)
        col = col * (1 - cf[..., None]) + fc * cf[..., None]
        cover = np.maximum(cover, cf)
        cls[cf > 0.5] = 3
    if mt:
        cm = rect_cov(X, Y, mat_outer, s)
        t = np.stack([-Y, X - Wa, Y - Ha, -X]).max(0)
        side = np.stack([-Y, X - Wa, Y - Ha, -X]).argmax(0)
        mc = np.asarray(mt["rgb"], np.float32)[None, None] * (1 + 0.006 * tex(sc["tiles"][0], X, Y, 0.02 * S, s))[..., None]
        bev = np.clip(0.5 + (mt["bevel"] - t) / s, 0, 1)
        bc = np.asarray(mt["core"], np.float32) * np.array([0.84, 1.05, 1.08, 0.88], np.float32)[side][..., None]
        mc = mc * (1 - bev[..., None]) + bc * bev[..., None]
        col = col * (1 - cm[..., None]) + mc * cm[..., None]
        cover = np.maximum(cover, cm)
        cls[cm > 0.5] = 4
    ca = rect_cov(X, Y, (0, 0, Wa, Ha), s)
    idx = ca > 0
    if idx.any():
        smp = sample_art(sc["art"], X[idx], Y[idx])
        col[idx] = col[idx] * (1 - ca[idx, None]) + smp * ca[idx, None]
        cover = np.maximum(cover, ca)
        inside = ca > 0.5
        cls[inside] = 5
        if sc.get("neutral") is not None:
            nm = ndi.map_coordinates(sc["neutral"].astype(np.float32), [Y[inside] - 0.5, X[inside] - 0.5], order=0, mode="nearest")
            sub = cls[inside]
            sub[nm > 0.5] = 6
            cls[inside] = sub
    if sc.get("hands"):
        skin = srgb_to_lin(np.array([0.87, 0.66, 0.55], np.float32))
        nail = srgb_to_lin(np.array([0.93, 0.79, 0.75], np.float32))
        for (cx, cy, a, b, ang, kind) in sc["hands"]:
            c, rho, v = ellipse_cov(X, Y, cx, cy, a, b, ang, s)
            if not c.any():
                continue
            sk = skin[None, None] * (0.72 + 0.32 * np.sqrt(np.clip(1 - rho * rho, 0, 1)))[..., None]
            if kind == "thumb":
                cn, _, _ = ellipse_cov(X, Y, cx + 0.62 * b * math.sin(ang), cy - 0.62 * b * math.cos(ang), 0.55 * a, 0.3 * b, ang, s)
                sk = sk * (1 - cn[..., None]) + nail[None, None] * cn[..., None]
            col = col * (1 - c[..., None]) + sk * c[..., None]
            cover = np.maximum(cover, c)
            cls[c > 0.5] = 7
    return col, cover, cls


def sample_art(art, X, Y):
    """Bilinear samples of a (prefiltered, linear) artwork at plane coordinates inside its window."""
    yy, xx = np.asarray(Y, np.float64) - 0.5, np.asarray(X, np.float64) - 0.5
    return np.stack([ndi.map_coordinates(art[..., c], [yy, xx], order=1, mode="nearest") for c in range(3)], -1)


def light_plane(L, X, Y):
    """Relative illumination on the plane: lamp falloff x broad hotspot x soft shadows."""
    E = np.ones(np.shape(X), np.float64)
    if L.get("lamp"):
        lx, ly, lz = L["lamp"]
        d2 = (X - lx) ** 2 + (Y - ly) ** 2 + lz * lz
        E *= lz / d2 ** 1.5 * L["lamp_norm"]
    if L.get("hot"):
        cx, cy, sg, a = L["hot"]
        E *= 1 + a * np.exp(-((X - cx) ** 2 + (Y - cy) ** 2) / (2 * sg * sg))
    for b in L.get("bands", []):
        t = (X - b["p"][0]) * b["n"][0] + (Y - b["p"][1]) * b["n"][1]
        m = sstep(t / b["soft"])
        if b.get("width"):
            m = m * sstep((b["width"] - t) / b["soft"])
        E *= 1 - b["depth"] * m
    return E


def gains_plane(G, X, Y):
    if G.get("mix"):
        mx = G["mix"]
        t = (X - mx["p"][0]) * mx["n"][0] + (Y - mx["p"][1]) * mx["n"][1]
        m = sstep(t / mx["soft"])[..., None]
        return np.asarray(G["g"], np.float32) * (1 - m) + np.asarray(mx["g2"], np.float32) * m
    return np.broadcast_to(np.asarray(G["g"], np.float32), np.shape(X) + (3,))


def local_scale(Hinv, xu, yu):
    """Plane units per photo pixel (sqrt of the homography's area scale) at undistorted photo points."""
    a, b, c, d, e, f, g, h, i = Hinv.ravel()
    w = g * xu + h * yu + i
    X = (a * xu + b * yu + c) / w
    Y = (d * xu + e * yu + f) / w
    det = ((a - g * X) * (e - h * Y) - (b - h * X) * (d - g * Y)) / (w * w)
    return np.sqrt(np.abs(det))


def render(sc, cam, L, G, wh, k1, room=None, strip=256):
    """Radiance (linear, before exposure), illumination E and class map of the photo."""
    Wp, Hp = wh
    c, rn = (Wp / 2.0, Hp / 2.0), math.hypot(Wp, Hp) / 2.0
    Hinv = np.linalg.inv(cam["H"])
    rad = np.zeros((Hp, Wp, 3), np.float32)
    Ef = np.zeros((Hp, Wp), np.float32)
    cls = np.zeros((Hp, Wp), np.uint8)
    xs = np.arange(Wp) + 0.5
    far = 60 * sc["S"]
    for y0 in range(0, Hp, strip):
        ys = np.arange(y0, min(Hp, y0 + strip)) + 0.5
        xd, yd = np.meshgrid(xs, ys)
        xu, yu = undistort(xd, yd, k1, c, rn)
        X, Y, w = hom(Hinv, xu, yu)
        ok = (w > 0) & (np.abs(X) < far) & (np.abs(Y) < far)
        X, Y = np.where(ok, X, 0.0), np.where(ok, Y, 0.0)
        s = np.where(ok, local_scale(Hinv, xu, yu), far)
        alb, cover, cl = shade(sc, X, Y, s)
        e = light_plane(L, X, Y)
        vig = 1 - L.get("vignette", 0.0) * (((xd - c[0]) ** 2 + (yd - c[1]) ** 2) / rn ** 2)
        gp = gains_plane(G, X, Y)
        r = alb * (e[..., None] * gp)
        if room is not None:
            rm = room[y0:y0 + len(ys)] * np.asarray(G["g"], np.float32)
            r = r * cover[..., None] + rm * (1 - cover[..., None])
            e = e * cover + 0.8 * (1 - cover)
        farcol = np.asarray(sc["far"], np.float32) * np.asarray(G["g"], np.float32)
        r[~ok] = farcol
        e = np.where(ok, e, 1.0)
        cl[~ok] = 0
        rad[y0:y0 + len(ys)] = r * vig[..., None]
        Ef[y0:y0 + len(ys)] = e * vig
        cls[y0:y0 + len(ys)] = cl
    return rad, Ef, cls


def room_background(rng, wh):
    """An out-of-focus room behind a hand-held sheet (image space, linear)."""
    Wp, Hp = wh
    cols = srgb_to_lin(np.array([(0.55, 0.50, 0.44), (0.36, 0.29, 0.22), (0.62, 0.60, 0.56), (0.26, 0.23, 0.21),
                                 (0.70, 0.68, 0.62), (0.45, 0.40, 0.38)], np.float32))
    grid = cols[rng.integers(0, len(cols), (5, 7))]
    up = np.stack([np.asarray(Image.fromarray(np.ascontiguousarray(grid[..., c]), "F").resize((Wp, Hp), Image.BILINEAR))
                   for c in range(3)], -1)
    up = ndi.gaussian_filter(up, (0.05 * Hp, 0.05 * Hp, 0))
    return (up * 0.75).astype(np.float32)


# --------------------------------------------------------------------------- photo pipeline

def motion_kernel(length, angle):
    n = int(math.ceil(length)) | 1
    k = Image.new("L", (n * 4, n * 4), 0)
    d = ImageDraw.Draw(k)
    c = n * 2
    dx, dy = math.cos(angle) * length * 2, math.sin(angle) * length * 2
    d.line([(c - dx, c - dy), (c + dx, c + dy)], fill=255, width=4)
    a = np.asarray(k.resize((n, n), Image.BOX), np.float64)
    return a / a.sum()


def finish(lin, rng, p):
    """Optics, sensor and ISP: blur or shake, noise, chroma smoothing, sharpening; returns sRGB floats."""
    from scipy.signal import fftconvolve
    if p.get("motion"):
        k = motion_kernel(*p["motion"])
        lin = np.stack([fftconvolve(lin[..., c], k, mode="same") for c in range(3)], -1)
    lin = ndi.gaussian_filter(lin, (p["blur"], p["blur"], 0))
    a, b = p["noise"]
    # in place, the same arithmetic as lin + sqrt(a lin+ + b^2) n: a 48 MP photo must not need many full copies
    sd = np.clip(lin, 0, None)
    sd *= a
    sd += b * b
    np.sqrt(sd, out=sd)
    sd *= rng.standard_normal(lin.shape, dtype=np.float32)
    lin += sd
    del sd
    Y = (lin @ LUM.astype(np.float32))[..., None]
    lin -= Y
    lin = ndi.gaussian_filter(lin, (1.0, 1.0, 0), output=lin)
    lin += Y
    del Y
    enc = lin_to_srgb(lin)
    del lin
    if p.get("sharpen"):                       # enc + amt (enc - G(enc)), in place
        amt, r = p["sharpen"]
        d = ndi.gaussian_filter(enc, (r, r, 0))
        np.subtract(enc, d, out=d)
        d *= amt
        enc += d
        del d
    return np.clip(enc, 0, 1, out=enc)


def save_photo(enc, path, quality, exif=None, orientation=1):
    im = Image.fromarray(u8(enc))
    ex = Image.Exif()
    if orientation == 6:
        im = im.transpose(Image.Transpose.ROTATE_90)      # stored sideways, as phones do; EXIF says turn it
        ex[0x0112] = 6
    if exif:
        ex[0x010F] = "Phone"
        ifd = ex.get_ifd(0x8769)
        ifd[0x920A] = round(exif["focal_mm"], 2)
        ifd[0xA405] = int(round(exif["focal_35mm"]))
    kw = {"quality": int(quality)}
    if len(ex):
        kw["exif"] = ex.tobytes()
    im.save(path, "JPEG", **kw)


# --------------------------------------------------------------------------- cases

def C(**kw):
    return kw


WARM, COOL, MILD = ("warm",), ("cool",), ("mild",)

CASES = [
    # no-op: already a flat scan or screenshot of just the artwork; rectify must leave it alone (exit 4)
    C(name="noop-scan-nighthouse", kind="noop", art="nighthouse", fmt="png",
      what="the skill's example picture itself, a flat lossless file"),
    C(name="noop-scan-wash", kind="noop", art="wash", fmt="jpg", q=95,
      what="a flat scan of a watercolour whose white paper margin is part of the work (do not crop it)"),
    C(name="noop-screenshot-watermark", kind="noop", art="screenshot", fmt="jpg", q=85,
      what="a flat screenshot of a painting with a share-platform mark in the lower right"),
    C(name="noop-gradient", kind="noop", art="oilgrad", fmt="jpg", q=92,
      what="a flat oil painting with a deliberate light-to-dark gradient (do not even it out)"),
    C(name="noop-red", kind="noop", art="red", fmt="png",
      what="a flat, saturated red painting under neutral light (do not neutralise it)"),
    C(name="noop-figure", kind="noop", art="figure", fmt="jpg", q=92,
      what="a flat small figure on a big plain sheet (do not crop to the figure)"),

    # casual photos where a correction is expected
    C(name="wash-wall-warm", kind="correct", art="wash", bg="wall", size="12mp", tilt=(14, 22), roll=(-4, 4),
      fill=(0.35, 0.45), k1=(-0.04, -0.02), exif=True, cast=WARM,
      light=dict(lamp=("above", 0.25, 0.35), vignette=(0.05, 0.12)),
      what="watercolour with its paper margin on a white wall under a warm picture light, 12 MP"),
    C(name="print-stone-cool", kind="correct", art="print", bg="stone", tilt=(22, 30), roll=(-8, 8),
      fill=(0.45, 0.6), k1=(0.01, 0.02), exif=False, cast=COOL,
      light=dict(lamp=("overhead", 0.10, 0.16), vignette=(0.15, 0.25)),
      what="a bordered print lying on a light stone table, shot from above at an angle in cool shade"),
    C(name="oilgrad-gold-warm", kind="correct", art="oilgrad", bg="wall", frame="gold", tilt=(18, 26), roll=(-5, 5),
      fill=(0.30, 0.42), k1=(-0.02, 0.0), exif=True, cast=WARM,
      light=dict(lamp=("side", 0.20, 0.30), hot=(0.08, 0.14), vignette=(0.05, 0.12)),
      what="the gradient oil in a gold frame on a white wall (frame shadow), warm lamp and a broad hotspot"),
    C(name="red-wall-falloff", kind="correct", art="red", bg="wall", tilt=(10, 16), roll=(-3, 3),
      fill=(0.4, 0.55), k1=(-0.06, -0.045), exif=False, cast=None,
      light=dict(lamp=("side", 0.25, 0.35), vignette=(0.05, 0.1)),
      what="the red painting on a white wall, neutral light with a strong side falloff and barrel distortion"),
    C(name="red-wall-warm", kind="correct", art="red", bg="wall", tilt=(20, 28), roll=(-4, 4),
      fill=(0.3, 0.4), k1=(-0.015, 0.0), exif=True, cast=WARM, light=dict(vignette=(0.03, 0.06)),
      what="the red painting on a white wall under a warm lamp: never neutralise the painting itself"),
    C(name="oil-tealwall-warm", kind="correct", art="oil", bg="teal", tilt=(10, 18), roll=(-4, 4),
      fill=(0.35, 0.5), k1=(-0.015, 0.0), exif=False, cast=WARM, light=dict(vignette=(0.02, 0.05)),
      what="an oil with no neutral passage on a teal wall under a warm lamp: no evidence, colour stays as shot"),
    C(name="dark-mat-cool", kind="correct", art="dark", bg="wall", wall="grey", mat=True, frame="black",
      tilt=(12, 18), roll=(-3, 3), fill=(0.28, 0.36), k1=(-0.02, 0.0), exif=True, cast=COOL,
      light=dict(lamp=("above", 0.15, 0.25), vignette=(0.05, 0.1)),
      what="a dark nocturne in a white mat and thin black frame on a grey wall, cool light"),
    C(name="figure-hands-warm", kind="correct", art="figure", bg="room", hands=True, tilt=(6, 12), roll=(-7, 7),
      fill=(0.55, 0.65), k1=(-0.02, 0.0), exif=False, cast=WARM,
      light=dict(bands=[("lower", 0.22, 0.30)], vignette=(0.08, 0.15)),
      what="a sketch held in both hands (thumbs on the margin) in a room, warm light, phone shadow on the lower part"),
    C(name="print-wall-shadow", kind="correct", art="print", bg="wall", tilt=(6, 10), roll=(-3, 3),
      fill=(0.5, 0.62), k1=(-0.02, 0.0), exif=True, cast=MILD,
      light=dict(bands=[("side", 0.25, 0.32)], vignette=(0.12, 0.18)),
      what="a bordered print on a wall with the photographer's soft shadow over one side"),
    C(name="wash-plaster-barrel", kind="correct", art="wash", bg="plaster", tilt=(28, 34), roll=(-5, 5),
      fill=(0.25, 0.30), k1=(-0.08, -0.065), exif=True, cast=COOL,
      light=dict(lamp=("side", 0.15, 0.22), vignette=(0.08, 0.14)),
      what="a small watercolour far away on a textured plaster wall, steep-ish view, strong barrel distortion"),
    C(name="oilgrad-frontal-fill", kind="correct", art="oilgrad", bg="wall", tilt=(2, 4), roll=(-2, 2),
      fill=(0.80, 0.86), k1=(-0.02, 0.0), exif=False, cast=WARM, light=dict(vignette=(0.20, 0.25)),
      what="the gradient oil almost straight on, filling the frame; vignetting must not be confused with paint"),
    C(name="print-pincushion-tele", kind="correct", art="print", bg="wall", lens="tele", tilt=(18, 24), roll=(-3, 3),
      fill=(0.45, 0.6), k1=(0.04, 0.05), exif=True, cast=COOL,
      light=dict(lamp=("above", 0.12, 0.18), vignette=(0.04, 0.08)),
      what="a bordered print through a tele lens with pincushion distortion"),
    C(name="gallery-neighbour", kind="correct", art="oil", bg="wall", frame="wood", neighbour=True,
      tilt=(12, 20), roll=(-3, 3), fill=(0.22, 0.28), k1=(-0.02, 0.0), exif=True, cast=WARM,
      light=dict(lamp=("above", 0.15, 0.22), vignette=(0.05, 0.1)),
      what="a framed painting with a smaller neighbouring painting also in view: crop the main one"),
    C(name="exif-rotated", kind="correct", art="oilgrad", bg="wall", size="12mp", orientation=6, tilt=(15, 20),
      roll=(-3, 3), fill=(0.35, 0.45), k1=(-0.02, 0.0), exif=True, cast=WARM,
      light=dict(lamp=("side", 0.15, 0.22), vignette=(0.05, 0.1)),
      what="a portrait photo stored sideways with EXIF orientation 6, as phones save them"),
    C(name="wash-table-wood", kind="correct", art="wash", bg="wood", tilt=(18, 26), roll=(-6, 6),
      fill=(0.40, 0.55), k1=(-0.03, -0.01), exif=True, cast=WARM,
      light=dict(lamp=("overhead", 0.15, 0.22), vignette=(0.08, 0.14)),
      what="a watercolour lying on a wooden table under a warm ceiling lamp: the wood is no white reference, "
           "the paper margin is"),
    C(name="perf-48mp", kind="correct", art="oilbig", bg="wall", size="48mp", tilt=(10, 16), roll=(-3, 3),
      fill=(0.30, 0.36), k1=(-0.02, -0.005), exif=True, cast=WARM, perf=True,
      light=dict(lamp=("side", 0.15, 0.22), vignette=(0.05, 0.1)),
      what="a 48 MP phone photo (8064 x 6048) of an oil on a wall: must stay usable (time and memory gated)"),

    # the contract's options
    C(name="opt-corners", kind="option", art="figure", bg="wall", wall="white", tilt=(15, 20), roll=(-4, 4),
      fill=(0.4, 0.5), k1=(-0.02, 0.0), exif=True, cast=COOL,
      light=dict(lamp=("above", 0.10, 0.16), vignette=(0.05, 0.1)), args="corners",
      what="white paper on a white wall (faint boundary): the corners are given, rectify must use them"),
    C(name="opt-frame-keep", kind="option", art="oil", bg="wall", mat=True, frame="gold", tilt=(12, 18),
      roll=(-3, 3), fill=(0.22, 0.30), k1=(-0.02, 0.0), exif=True, cast=WARM,
      light=dict(lamp=("above", 0.12, 0.18), vignette=(0.05, 0.1)), args="frame-keep",
      what="--frame keep: the result is the framed object, mat and moulding included"),
    C(name="opt-off", kind="option", art="wash", bg="wall", tilt=(12, 18), roll=(-3, 3), fill=(0.35, 0.45),
      k1=(-0.02, 0.0), exif=True, cast=WARM, light=dict(lamp=("side", 0.25, 0.32), vignette=(0.05, 0.1)),
      args="off", what="--light off --color off: geometry only, the cast and falloff stay as shot"),

    # a reshoot would be better: say so in plain words (and never fake a fix)
    C(name="stress-glare", kind="stress", art="oil", bg="wall", tilt=(10, 15), roll=(-3, 3), fill=(0.35, 0.45),
      k1=(-0.02, 0.0), exif=True, cast=WARM, light=dict(vignette=(0.04, 0.08)), glare=True,
      what="a glare spot burns out part of a glossy painting"),
    C(name="stress-cutoff", kind="stress", art="wash", bg="wall", tilt=(8, 14), roll=(-3, 3), fill=(0.30, 0.38),
      k1=(-0.02, 0.0), exif=True, cast=WARM, light=dict(vignette=(0.04, 0.08)), cutoff=True,
      what="one corner of the work lies outside the photo"),
    C(name="stress-steep", kind="stress", art="print", bg="stone", tilt=(62, 66), roll=(-3, 3), fill=(0.22, 0.28),
      k1=(-0.02, 0.0), exif=True, cast=None, light=dict(vignette=(0.04, 0.08)),
      what="a print on a table seen at more than 60 degrees"),
    C(name="stress-motionblur", kind="stress", art="print", bg="wall", size="mb", tilt=(8, 14), roll=(-3, 3),
      fill=(0.4, 0.5), k1=(-0.02, 0.0), exif=True, cast=MILD, light=dict(vignette=(0.04, 0.08)), motion=(0.016, 0.022),
      what="camera shake smears the whole photo"),
    C(name="stress-lowres", kind="stress", art="red", bg="wall", size="lowres", tilt=(8, 14), roll=(-3, 3),
      fill=(0.22, 0.28), k1=(-0.01, 0.0), exif=False, cast=None, light=dict(vignette=(0.04, 0.08)),
      what="a tiny, recompressed photo: the work is under 300 px across"),

    # beyond the global model; reported, not gating
    C(name="stretch-hard-shadow", kind="stretch", art="wash", bg="wall", tilt=(8, 14), roll=(-3, 3),
      fill=(0.45, 0.55), k1=(-0.02, 0.0), exif=True, cast=None,
      light=dict(bands=[("hard", 0.28, 0.34)], vignette=(0.04, 0.08)),
      what="a crisp diagonal shadow band across the whole sheet"),
    C(name="stretch-mixed-light", kind="stretch", art="print", bg="wall", tilt=(8, 14), roll=(-3, 3),
      fill=(0.45, 0.55), k1=(-0.02, 0.0), exif=True, cast=("mixed",),
      light=dict(lamp=("side", 0.12, 0.18), vignette=(0.04, 0.08)),
      what="a warm lamp from one side and cool window light from the other"),
]

SIZES = {"std": (2400, 1800), "12mp": (4032, 3024), "48mp": (8064, 6048), "mb": (2000, 1500), "lowres": (640, 480)}
MAX_OUT = 4096          # rectify's default --max: the output's long side is capped there
WALLS = {"white": (0.905, 0.902, 0.893), "grey": (0.78, 0.778, 0.77)}

def draw(rng, rngspec):
    return float(rng.uniform(rngspec[0], rngspec[1])) if isinstance(rngspec, (tuple, list)) else float(rngspec)


def quad_area(q):
    q = np.asarray(q)
    x, y = q[:, 0], q[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def outline(r, n=24):
    x0, y0, x1, y1 = r
    t = np.linspace(0, 1, n, endpoint=False)
    return np.concatenate([np.c_[x0 + (x1 - x0) * t, np.full(n, y0)], np.c_[np.full(n, x1), y0 + (y1 - y0) * t],
                           np.c_[x1 - (x1 - x0) * t, np.full(n, y1)], np.c_[np.full(n, x0), y1 - (y1 - y0) * t]])


def project(Hm, k1, c, rn, pts):
    pts = np.asarray(pts, np.float64)
    xu, yu, w = hom(Hm, pts[:, 0], pts[:, 1])
    xd, yd = distort(xu, yu, k1, c, rn)
    return np.c_[xd, yd], w


def place(rng, spec, wh, f, sc, tilt, azim, roll, fill, k1):
    """Distance and aim for the target fill, with the whole object (frame included) inside the photo."""
    Wp, Hp = wh
    c, rn = (Wp / 2.0, Hp / 2.0), math.hypot(Wp, Hp) / 2.0
    Wa, Ha = sc["W"], sc["H"]
    art = [(0, 0), (Wa, 0), (Wa, Ha), (0, Ha)]
    obj = outline(sc["outer"])
    margin = 0.025 * min(Wp, Hp)
    off = np.array([rng.uniform(-0.12, 0.12) * Wa, rng.uniform(-0.12, 0.12) * Ha])
    if spec.get("neighbour"):
        off = np.array([0.42 * Wa, rng.uniform(-0.05, 0.05) * Ha])
    dist = f * math.sqrt(Wa * Ha / (fill * Wp * Hp))
    for attempt in range(40):
        target = (Wa / 2 + off[0], Ha / 2 + off[1])
        for _ in range(6):
            Hm, Cc = look_at(f, wh, target, tilt, azim, roll, dist)
            q, _ = project(Hm, k1, c, rn, art)
            dist *= math.sqrt(quad_area(q) / (fill * Wp * Hp))
        Hm, Cc = look_at(f, wh, target, tilt, azim, roll, dist)
        p, w = project(Hm, k1, c, rn, obj)
        inside = (w > 0).all() and p[:, 0].min() > margin and p[:, 1].min() > margin and \
            p[:, 0].max() < Wp - margin and p[:, 1].max() < Hp - margin
        if inside:
            return Hm, Cc, dist, target
        if spec.get("neighbour"):
            off[0] *= 0.93
        else:
            off *= 0.7
        if attempt > 8:
            fill *= 0.96
    raise RuntimeError("could not place the camera for " + spec["name"])


def push_corner_out(Hm_fn, sc, wh, k1, target, amount):
    """Move the aim so the bottom-right corner of the work lies past the bottom-right corner of the photo
    by `amount` of its photo width (diagonally), so only that corner is cut off."""
    Wp, Hp = wh
    c, rn = (Wp / 2.0, Hp / 2.0), math.hypot(Wp, Hp) / 2.0
    Wa, Ha = sc["W"], sc["H"]
    t = np.array(target, np.float64)
    for _ in range(8):
        Hm = Hm_fn(t)
        q, _ = project(Hm, k1, c, rn, [(0, 0), (Wa, 0), (Wa, Ha), (0, Ha)])
        width = np.linalg.norm(q[1] - q[0])
        want = np.array([Wp + 0.7 * amount * width, Hp + 0.7 * amount * width])
        e = q[2] - want
        J = np.zeros((2, 2))
        for k in range(2):
            dt = np.zeros(2)
            dt[k] = 1.0
            q2, _ = project(Hm_fn(t + dt), k1, c, rn, [(Wa, Ha)])
            J[:, k] = q2[0] - q[2]
        t = t - np.linalg.solve(J, e)
    return t


def photo_params(rng, spec, wh):
    # noise as a phone leaves it after its own noise reduction (shadows a few levels, clipped at black)
    p = dict(blur=float(rng.uniform(0.5, 1.0)), noise=(float(rng.uniform(5e-5, 2e-4)), float(rng.uniform(0.0008, 0.002))),
             sharpen=(float(rng.uniform(0.2, 0.5)), 1.2) if rng.uniform() < 0.6 else None,
             quality=int(rng.integers(82, 94)))
    if spec.get("size") == "lowres":
        p.update(blur=0.6, quality=72, sharpen=None)
    if spec.get("motion"):
        p["motion"] = (draw(rng, spec["motion"]) * max(wh), float(rng.uniform(0, math.pi)))
    return p


def make_light(rng, spec, sc):
    """Pick lamp, hotspot, shadow and vignetting for the spec; the lamp distance is solved so the falloff
    over the work hits the drawn strength."""
    Wa, Ha, S = sc["W"], sc["H"], sc["S"]
    ls = spec.get("light", {})
    L = {"vignette": draw(rng, ls.get("vignette", (0.0, 0.0))), "bands": []}
    gy, gx = np.mgrid[0:24, 0:24]
    X, Y = (gx + 0.5) / 24 * Wa, (gy + 0.5) / 24 * Ha

    def var(E):
        return float(np.percentile(E, 98) / np.percentile(E, 2) - 1)
    if ls.get("lamp"):
        where, v0, v1 = ls["lamp"]
        want = rng.uniform(v0, v1)
        if where == "above":
            lx, ly = Wa * rng.uniform(0.35, 0.65), -Ha * rng.uniform(0.15, 0.4)
        elif where == "side":
            sd = rng.choice([-1, 1])
            lx, ly = Wa / 2 + sd * Wa * rng.uniform(0.7, 1.1), Ha * rng.uniform(-0.3, 0.4)
        else:
            lx, ly = Wa * rng.uniform(0.2, 0.8), Ha * rng.uniform(0.2, 0.8)
        lo, hi = 0.02 * S, 30 * S
        for _ in range(50):
            lz = math.sqrt(lo * hi)
            E = lz / ((X - lx) ** 2 + (Y - ly) ** 2 + lz * lz) ** 1.5
            if var(E) > want:
                lo = lz
            else:
                hi = lz
        E = lz / ((X - lx) ** 2 + (Y - ly) ** 2 + lz * lz) ** 1.5
        L["lamp"] = [float(lx), float(ly), float(lz)]
        L["lamp_norm"] = float(1 / E.mean())
    if ls.get("hot"):
        L["hot"] = [float(Wa * rng.uniform(0.3, 0.7)), float(Ha * rng.uniform(0.3, 0.7)),
                    float(S * rng.uniform(0.45, 0.65)), float(draw(rng, ls["hot"]))]
    for kind, d0, d1 in ls.get("bands", []):
        depth = float(rng.uniform(d0, d1))
        if kind == "lower":
            a = math.radians(rng.uniform(-12, 12))
            L["bands"].append({"p": [Wa / 2, Ha * rng.uniform(0.58, 0.72)], "n": [-math.sin(a), math.cos(a)],
                               "soft": float(Ha * rng.uniform(0.14, 0.2)), "depth": depth})
        elif kind == "side":
            sd = rng.choice([-1.0, 1.0])
            a = math.radians(rng.uniform(-10, 10))
            px = Wa / 2 - sd * Wa * rng.uniform(0.1, 0.22)
            L["bands"].append({"p": [px, Ha / 2], "n": [-sd * math.cos(a), math.sin(a)],
                               "soft": float(Wa * rng.uniform(0.38, 0.5)), "depth": depth})
        else:                                   # hard: a crisp diagonal band right across
            a = math.radians(rng.uniform(25, 60))
            n = [math.cos(a), math.sin(a)]
            p = [Wa * 0.5 - n[0] * 0.12 * S, Ha * 0.5 - n[1] * 0.12 * S]
            L["bands"].append({"p": p, "n": n, "soft": float(0.008 * S), "depth": depth, "width": float(0.22 * S)})
    for b in L["bands"]:
        b["p"] = [float(v) for v in b["p"]]
        b["n"] = [float(v) for v in b["n"]]
    return L


def make_gains(rng, spec, sc):
    cast = spec.get("cast")
    if not cast:
        return {"g": [1.0, 1.0, 1.0], "kelvin": None, "camera_kelvin": None}
    if cast[0] == "warm":
        T = rng.uniform(2700, 3200)
        Tc = T + rng.uniform(500, 900)
    elif cast[0] == "cool":
        T = rng.uniform(6800, 8000)
        Tc = T - rng.uniform(1500, 2200)
    elif cast[0] == "mild":
        T, Tc = rng.uniform(4000, 4500), 5500.0
    else:                                       # mixed: warm lamp on one side, cool daylight on the other
        T, T2 = rng.uniform(2800, 3200), rng.uniform(6800, 7500)
        Tc = 4800.0
        g1, g2 = cast_gains(T, Tc), cast_gains(T2, Tc)
        sd = rng.choice([-1.0, 1.0])
        return {"g": [float(v) for v in g1], "kelvin": float(T), "camera_kelvin": Tc,
                "mix": {"g2": [float(v) for v in g2], "kelvin2": float(T2), "p": [sc["W"] / 2, sc["H"] / 2],
                        "n": [sd, 0.0], "soft": float(0.6 * sc["W"])}}
    g = cast_gains(T, Tc)
    return {"g": [float(v) for v in g], "kelvin": float(T), "camera_kelvin": float(Tc)}


def build_scene(rng, spec, art_srgb, neutral):
    Ha, Wa = art_srgb.shape[:2]
    S = float(max(Wa, Ha))
    tiles = [tile_noise(rng, 256, 9), tile_noise(rng, 256, 6), tile_noise(rng, 256, 4)]
    sc = {"W": Wa, "H": Ha, "S": S, "tiles": tiles, "neutral": neutral, "far": srgb_to_lin(np.array([0.3, 0.3, 0.31])).tolist()}
    kind = spec.get("bg", "wall")
    if kind == "wall":
        base = np.array(WALLS[spec.get("wall", "white")]) * rng.uniform(0.97, 1.02)
        sc["bg"] = {"kind": "wall", "rgb": srgb_to_lin(np.clip(base, 0, 1)).tolist()}
    elif kind == "plaster":
        sc["bg"] = {"kind": "plaster", "rgb": srgb_to_lin(np.array([0.88, 0.876, 0.866])).tolist()}
    elif kind == "stone":
        sc["bg"] = {"kind": "stone", "rgb": srgb_to_lin(np.array([0.78, 0.778, 0.77])).tolist()}
    elif kind == "teal":
        sc["bg"] = {"kind": "teal", "rgb": srgb_to_lin(np.array([0.22, 0.50, 0.50])).tolist()}
    elif kind == "room":
        sc["bg"] = {"kind": "room", "rgb": [0.0, 0.0, 0.0]}
    elif kind == "wood":                        # medium oak: far too coloured to be a white reference
        sc["bg"] = {"kind": "wood", "rgb": srgb_to_lin(np.array([0.56, 0.39, 0.25])).tolist(),
                    "angle": float(rng.uniform(0, math.pi))}
    sc["mw"] = 0.0
    if spec.get("mat"):
        mw = 0.12 * min(Wa, Ha)
        sc["mat"] = {"w": mw, "rgb": srgb_to_lin(np.array([0.94, 0.935, 0.926])).tolist(),
                     "core": srgb_to_lin(np.array([0.975, 0.972, 0.965])).tolist(), "bevel": 0.006 * min(Wa, Ha)}
        sc["mw"] = mw
    outer = (-sc["mw"], -sc["mw"], Wa + sc["mw"], Ha + sc["mw"])
    if spec.get("frame"):
        fk = spec["frame"]
        fw = {"gold": 0.075, "black": 0.025, "wood": 0.045}[fk] * min(Wa, Ha)
        rgb = {"gold": (0.70, 0.55, 0.28), "black": (0.09, 0.09, 0.095), "wood": (0.40, 0.25, 0.14)}[fk]
        sc["frame"] = {"kind": fk, "w": fw, "rgb": srgb_to_lin(np.array(rgb)).tolist()}
        outer = (outer[0] - fw, outer[1] - fw, outer[2] + fw, outer[3] + fw)
        sc["shadow"] = {"dx": 0.006 * S, "dy": 0.013 * S, "soft": 0.012 * S, "alpha": float(rng.uniform(0.22, 0.32))}
    sc["outer"] = outer
    if spec.get("hands"):
        sc["hands"] = [(0.08 * Wa, Ha + 0.03 * Ha, 0.055 * Wa, 0.085 * Ha, math.radians(18), "thumb"),
                       (0.92 * Wa, Ha + 0.03 * Ha, 0.055 * Wa, 0.085 * Ha, math.radians(-18), "thumb"),
                       (0.02 * Wa, Ha + 0.20 * Ha, 0.16 * Wa, 0.15 * Ha, math.radians(30), "hand"),
                       (0.98 * Wa, Ha + 0.20 * Ha, 0.16 * Wa, 0.15 * Ha, math.radians(-30), "hand")]
    if spec.get("neighbour"):
        a2, _ = art_dark(rng, 700, 900)
        fw2 = 0.03 * 700
        x0 = outer[2] + 0.22 * Wa + fw2
        y0 = Ha / 2 - 450 + rng.uniform(-0.05, 0.05) * Ha
        sc["neighbour"] = {"rect": (x0, y0, x0 + 700, y0 + 900), "outer": (x0 - fw2, y0 - fw2, x0 + 700 + fw2, y0 + 900 + fw2),
                           "frame": {"kind": "black", "w": fw2, "rgb": srgb_to_lin(np.array([0.09, 0.09, 0.095])).tolist()},
                           "art": srgb_to_lin(a2)}
    sc["art_srgb"] = art_srgb
    return sc


def evidence_fit(meta, sc, E, cls, wh, evid):
    """How well a smooth surface fitted to the evidence (margin, mat, neutral wall) predicts the light over
    the work: the residual variation a capable corrector can reach (best of degree 1-3 polynomials and a
    thin-plate spline through the evidence)."""
    from scipy.interpolate import RBFInterpolator
    Wp, Hp = wh
    step = max(4, int(max(Wp, Hp) / 300))
    ys, xs = np.mgrid[step // 2:Hp:step, step // 2:Wp:step]
    X, Y, w = photo_to_plane(meta, xs + 0.5, ys + 0.5)
    e = np.log(np.maximum(E[ys, xs], 1e-4))
    cl = cls[ys, xs]
    codes = {"margin": [6], "mat": [4], "surround": [1]}
    ev = np.isin(cl, sum((codes[k] for k in evid), []))
    art = np.isin(cl, [5, 6])
    if ev.sum() < 30 or art.sum() < 30:
        return None
    S = sc["S"]
    u, v = X / S, Y / S

    def spread(r):
        lo, hi = np.percentile(r, [2, 98])
        return float(np.exp(hi - lo) - 1)
    best = None
    for deg in (1, 2, 3):
        cols = [u ** i * v ** j for i in range(deg + 1) for j in range(deg + 1 - i)]
        A = np.stack(cols, -1)
        coef, *_ = np.linalg.lstsq(A[ev], e[ev], rcond=None)
        val = spread(e[art] - A[art] @ coef)
        best = val if best is None else min(best, val)
    pe = np.c_[u[ev], v[ev]]
    k = max(1, len(pe) // 1500)
    tps = RBFInterpolator(pe[::k], e[ev][::k], kernel="thin_plate_spline", smoothing=1e-6, degree=1)
    return min(best, spread(e[art] - tps(np.c_[u[art], v[art]])))


def bow_if_ignored(meta, gw, gh):
    """How far straight lines in the work would still bow (% of the gt diagonal) if the lens were ignored,
    measured against the best homography, as bench.py's straightness check sees it."""
    n = 17
    uu, vv = np.meshgrid(np.linspace(0, gw, n), np.linspace(0, gh, n))
    xd, yd = gt_to_photo(meta, uu.ravel(), vv.ravel())
    src = np.c_[uu.ravel(), vv.ravel()]
    dst = np.c_[xd, yd]
    Hf = fit_homography(dst, src)              # photo -> gt
    px, py, _ = hom(Hf, dst[:, 0], dst[:, 1])
    err = np.hypot(px - src[:, 0], py - src[:, 1])
    return float(err.max() / math.hypot(gw, gh) * 100)


def aspect_tolerance(meta, quad_u, f_true, wh, exif, gw, gh, rng):
    """Aspect tolerance (%): 1.0 when the focal length is in EXIF (aspect then follows exactly from the four
    corners); otherwise what an ideal method reaches from the vanishing points (corners jittered by 0.5 px)
    or, failing that, by assuming the usual 26 mm phone lens, whichever is better, with 25% and 0.5 points
    of slack."""
    true = gw / float(gh)
    c = (wh[0] / 2.0, wh[1] / 2.0)
    a, _ = zhang_aspect(quad_u, f_true, c)
    sanity = abs(a / true - 1) * 100 if a else None
    if exif:
        return 1.0, {"exact_with_focal_pct": sanity}
    errs = []
    for _ in range(16):
        q = np.asarray(quad_u) + rng.normal(0, 0.5, (4, 2))
        a, f = zhang_aspect(q, None, c)
        diag = math.hypot(*wh)
        errs.append(abs(a / true - 1) * 100 if a and 0.3 * diag < f < 4 * diag else float("inf"))
    e_vp = float(np.median(errs))
    f26 = 26.0 / FULL_FRAME_DIAG * math.hypot(*wh)
    a26, _ = zhang_aspect(quad_u, f26, c)
    e_26 = abs(a26 / true - 1) * 100 if a26 else float("inf")
    tol = max(1.0, 1.25 * min(e_vp, e_26) + 0.5)
    return round(tol, 2), {"exact_with_focal_pct": sanity, "vanishing_pct": None if math.isinf(e_vp) else round(e_vp, 3),
                           "assume_26mm_pct": None if math.isinf(e_26) else round(e_26, 3)}


def cast_de(art_srgb, g):
    """Mean CIEDE2000 a cast would leave on the work (exposure refitted), on the comparison grid; g is one
    gain triple or a field of them at the grid's size (mixed light)."""
    h, w = art_srgb.shape[:2]
    small = resize_lin(srgb_to_lin(art_srgb), cmp_size(w, h))
    g = np.asarray(g, np.float32)
    if g.ndim == 3 and g.shape[:2] != small.shape[:2]:
        g = resize_lin(g, (small.shape[1], small.shape[0]))
    cast = small * g
    Yg, Yc = small @ LUM, cast @ LUM
    ok = (Yg > 0.02) & (Yc > 0.02)
    e = float(np.median(Yg[ok] / Yc[ok])) if ok.any() else 1.0
    return float(delta_e2000(lin_to_lab(np.clip(cast * e, 0, 1)), lin_to_lab(small)).mean())


def sample_photo_field(F, meta, gw, gh, step=1.0):
    """Sample a photo-space field (H, W[, C]) at every gt pixel centre through the true mapping; NaN outside."""
    uu, vv = np.meshgrid((np.arange(0, gw, step) + 0.5 * step), (np.arange(0, gh, step) + 0.5 * step))
    xd, yd = gt_to_photo(meta, uu, vv)
    Hp, Wp = F.shape[:2]
    inside = (xd >= 0) & (yd >= 0) & (xd <= Wp) & (yd <= Hp)
    coords = [yd - 0.5, xd - 0.5]
    if F.ndim == 2:
        out = ndi.map_coordinates(F, coords, order=1, mode="nearest")
    else:
        out = np.stack([ndi.map_coordinates(F[..., c], coords, order=1, mode="nearest") for c in range(F.shape[2])], -1)
    out = out.astype(np.float32)
    out[~inside] = np.nan
    return out, inside


def rnd(x, n=10):
    """JSON-ready copy with n significant digits (a homography's perspective terms are ~1e-5: never round
    them to a fixed number of decimals)."""
    if isinstance(x, dict):
        return {k: rnd(v, n) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [rnd(v, n) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float("%.*g" % (n, float(x)))
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, np.ndarray):
        return rnd(x.tolist(), n)
    return x


INNER = {"wash": "the painted area without its paper margin", "print": "the picture without its paper border",
         "figure": "the figure alone, without its plain ground"}


def inner_quad(art_key, neutral):
    """For bench.py's notes: the rectangle (gt px) a tool lands on when it takes an inner edge for the work's
    (a watercolour's painted area, a print's picture, a small figure on its plain sheet)."""
    if neutral is None or art_key not in INNER:
        return None
    inner = ~neutral
    inner[:4], inner[-4:], inner[:, :4], inner[:, -4:] = False, False, False, False
    ys, xs = np.nonzero(inner)
    if not len(xs):
        return None
    return {"what": INNER[art_key], "rect": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]}


def write_noop(spec, art, folder, seed, neutral=None):
    """The artwork itself as the photo: lossless PNG, or a JPEG as a scanner or screenshot would save it."""
    os.makedirs(folder, exist_ok=True)
    if spec.get("fmt") == "png":
        photo = os.path.join(folder, "photo.png")
        if spec["art"] == "nighthouse":
            shutil.copyfile(NIGHT_HOUSE, photo)
        else:
            Image.fromarray(u8(art)).save(photo)
    else:
        photo = os.path.join(folder, "photo.jpg")
        Image.fromarray(u8(art)).save(photo, "JPEG", quality=spec.get("q", 92))
    dec = np.asarray(ImageOps.exif_transpose(Image.open(photo)).convert("RGB"))
    Image.fromarray(dec).save(os.path.join(folder, "gt.png"))
    h, w = dec.shape[:2]
    meta = noop_meta(spec["name"], spec.get("what", ""), os.path.basename(photo), w, h, seed, spec["art"])
    iq = inner_quad(spec["art"], neutral)
    if iq:
        meta["inner_quad"] = iq
    with open(os.path.join(folder, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    return meta


def noop_meta(name, what, photo, w, h, seed, art):
    corners = [[0, 0], [w, 0], [w, h], [0, h]]
    return {"case": name, "kind": "noop", "what": what, "version": VERSION, "seed": seed, "photo": photo, "gt": "gt.png",
            "ignore": None, "photo_size": [w, h], "gt_size": [w, h], "artwork": art,
            "camera": {"H": np.eye(3).tolist(), "gt_to_plane": np.eye(3).tolist(), "k1": 0.0,
                       "center": [w / 2, h / 2], "rnorm": math.hypot(w, h) / 2, "tilt_deg": 0.0},
            "corners": corners, "quads": {"gt": corners}, "args": [],
            "expect": {"exit": [4, 0], "unchanged": True, "geometry": False, "lens": "none", "light": "none", "color": "none",
                       "warnings": [], "must_not": ["change", "crop", "restyle", "warn_reshoot"]}}


def make_case(spec, seed, root, extras):
    name, kind = spec["name"], spec["kind"]
    rngs = [np.random.default_rng([seed, zlib.crc32(name.encode()), i]) for i in range(6)]
    rng_art, rng_tex, rng_cam, rng_light, rng_ph, rng_misc = rngs
    folder = os.path.join(root, name)
    if os.path.isdir(folder):
        shutil.rmtree(folder)
    os.makedirs(folder)
    art, neutral = make_art(spec["art"], rng_art, extras)
    if kind == "noop":
        return write_noop(spec, art, folder, seed, neutral)

    Wp, Hp = SIZES[spec.get("size", "std")]
    gh, gw = art.shape[:2]
    if gh > gw * 1.05:
        Wp, Hp = Hp, Wp                         # people turn the phone for a portrait work
    wh = (Wp, Hp)
    f35 = float(rng_cam.uniform(24, 28)) if spec.get("lens", "main") == "main" else float(rng_cam.choice([48.0, 52.0, 77.0]))
    f = f35 / FULL_FRAME_DIAG * math.hypot(Wp, Hp)
    tilt, roll = draw(rng_cam, spec["tilt"]), draw(rng_cam, spec["roll"])
    azim = float(rng_cam.uniform(0, 360))
    fill, k1 = draw(rng_cam, spec["fill"]), draw(rng_cam, spec["k1"])
    sc = build_scene(rng_tex, spec, art, neutral)
    c, rn = (Wp / 2.0, Hp / 2.0), math.hypot(Wp, Hp) / 2.0
    Hm, Cc, dist, target = place(rng_cam, spec, wh, f, sc, tilt, azim, roll, fill, k1)
    if spec.get("cutoff"):
        fn = lambda t: look_at(f, wh, t, tilt, azim, roll, dist)[0]
        amount = float(rng_cam.uniform(0.05, 0.08))
        for _ in range(6):                      # exactly one corner outside, the other three well inside
            t2 = tuple(push_corner_out(fn, sc, wh, k1, target, amount))
            q, _ = project(fn(t2), k1, c, rn, [(0, 0), (gw, 0), (gw, gh), (0, gh)])
            m = 0.03 * min(Wp, Hp)
            inside = [(m < p[0] < Wp - m) and (m < p[1] < Hp - m) for p in q]
            if inside[:2] + inside[3:] == [True, True, True] and not inside[2]:
                break
            amount *= 0.75
        target = t2
        Hm, Cc = look_at(f, wh, target, tilt, azim, roll, dist)

    # the artwork as the camera sees it: prefiltered to the photo's sampling where the photo is coarser
    s_mid = float(local_scale(np.linalg.inv(Hm), *hom(Hm, gw / 2.0, gh / 2.0)[:2]))
    lin = srgb_to_lin(art)
    if s_mid > 1.0:
        lin = ndi.gaussian_filter(lin, (0.5 * s_mid, 0.5 * s_mid, 0))
    sc["art"] = lin

    L = make_light(rng_light, spec, sc)
    G = make_gains(rng_light, spec, sc)
    room = room_background(rng_misc, wh) if sc["bg"]["kind"] == "room" else None
    rad, E, cls = render(sc, {"H": Hm}, L, G, wh, k1, room=room)

    glare = None
    if spec.get("glare"):
        q, _ = project(Hm, k1, c, rn, [(gw * 0.62, gh * 0.38)])
        gx, gy = q[0]
        sg = 0.035 * math.hypot(Wp, Hp)
        yy, xx = np.mgrid[0:Hp, 0:Wp]
        r2 = ((xx + 0.5 - gx) ** 2 + (yy + 0.5 - gy) ** 2).astype(np.float32)
        amp = float(rng_misc.uniform(2.0, 3.0)) * float(np.percentile(rad[cls == 5] @ LUM.astype(np.float32), 90))
        glare = (amp * (np.exp(-r2 / (2 * sg * sg)) + 0.12 * np.exp(-r2 / (2 * (3 * sg) ** 2))))[..., None] * np.asarray(G["g"], np.float32)
    peak = float(np.percentile(rad.max(-1), 99.8))
    expo = float(rng_misc.uniform(0.82, 0.92)) / max(peak, 1e-6)
    lin_photo = rad * expo
    if glare is not None:
        lin_photo += glare * expo
    else:
        del rad                                 # only the glare's ignore mask needs it later
    pp = photo_params(rng_ph, spec, wh)
    enc = finish(lin_photo.astype(np.float32, copy=False), rng_ph, pp)
    del lin_photo
    exif = None
    if spec.get("exif"):
        crop = FULL_FRAME_DIAG / 9.6
        exif = {"focal_mm": f35 / crop, "focal_35mm": f35}
    orient = int(spec.get("orientation", 1))
    save_photo(enc, os.path.join(folder, "photo.jpg"), pp["quality"], exif, orient)

    # ground truth and the mapping
    arg = spec.get("args")
    gt_to_plane = np.eye(3)
    gt_srgb = art
    if arg == "frame-keep":
        o = sc["outer"]
        fw_, fh_ = int(round(o[2] - o[0])), int(round(o[3] - o[1]))
        gy_, gx_ = np.mgrid[0:fh_, 0:fw_] + 0.5
        Xg, Yg = gx_ + o[0], gy_ + o[1]
        sc_flat = dict(sc, art=srgb_to_lin(art), shadow=None)
        alb, _, _ = shade(sc_flat, Xg, Yg, np.ones_like(Xg))
        gt_srgb = lin_to_srgb(alb)
        gt_to_plane = np.array([[1, 0, o[0]], [0, 1, o[1]], [0, 0, 1.0]])
    ggh, ggw = gt_srgb.shape[:2]
    meta = {"case": name, "kind": kind, "what": spec.get("what", ""), "version": VERSION, "seed": seed,
            "photo": "photo.jpg", "gt": "gt.png", "ignore": None, "photo_size": [Wp, Hp], "gt_size": [ggw, ggh],
            "stored_orientation": orient, "artwork": spec["art"],
            "camera": {"H": (Hm / Hm[2, 2]).tolist(), "gt_to_plane": gt_to_plane.tolist(), "k1": k1, "center": [c[0], c[1]],
                       "rnorm": rn, "lens_model": "x_d = c + (x_u - c)(1 + k1 r^2), r = |x_u - c| / rnorm",
                       "focal_px": f, "focal_35mm": f35, "tilt_deg": tilt, "azimuth_deg": azim, "roll_deg": roll,
                       "distance": dist, "fill_target": fill, "exif": exif},
            "photo_params": pp, "exposure": expo, "light": L, "gains": G}
    corners, _ = project(np.array(meta["camera"]["H"]) @ gt_to_plane, k1, c, rn, [(0, 0), (ggw, 0), (ggw, ggh), (0, ggh)])
    meta["corners"] = corners.tolist()
    quads = {"gt": corners.tolist(), "art": project(Hm, k1, c, rn, [(0, 0), (gw, 0), (gw, gh), (0, gh)])[0].tolist()}
    if sc.get("mat"):
        mw = sc["mw"]
        quads["mat"] = project(Hm, k1, c, rn, [(-mw, -mw), (gw + mw, -mw), (gw + mw, gh + mw), (-mw, gh + mw)])[0].tolist()
    if sc.get("frame"):
        o = sc["outer"]
        quads["frame"] = project(Hm, k1, c, rn, [(o[0], o[1]), (o[2], o[1]), (o[2], o[3]), (o[0], o[3])])[0].tolist()
    meta["quads"] = quads
    meta["fill_actual"] = quad_area(quads["art"]) / (Wp * Hp)
    qu = [hom(Hm @ gt_to_plane, x, y)[:2] for x, y in [(0, 0), (ggw, 0), (ggw, ggh), (0, ggh)]]
    meta["corners_undistorted"] = [[float(a), float(b)] for a, b in qu]
    sides = [np.linalg.norm(np.subtract(corners[i], corners[(i + 1) % 4])) for i in range(4)]
    meta["apparent_long_side"] = float(max(sides))
    meta["expected_long_side"] = min(float(max(sides)), MAX_OUT)       # native, never upsampled, capped by --max
    iq = inner_quad(spec["art"], neutral) if arg != "frame-keep" else None
    if iq:
        meta["inner_quad"] = iq

    # what the evidence allows
    evid = []
    if neutral is not None:
        evid.append("margin")
    if sc.get("mat"):
        evid.append("mat")
    bgk = sc["bg"]["kind"]
    if bgk != "room" and chroma_srgb(lin_to_srgb(np.array(sc["bg"]["rgb"]))) <= NEUTRAL_CHROMA and (cls == 1).mean() > 0.03:
        evid.append("surround")
    E_art, inside = sample_photo_field(E, meta, gw, gh, step=max(1.0, max(gw, gh) / 256.0))
    Ev = E_art[np.isfinite(E_art)]
    lw = np.isfinite(E_art)
    light_var = light_variation(np.where(lw, E_art, 0.0), np.ones_like(E_art), lw, LIGHT_SIGMA * math.hypot(*E_art.shape))
    fitres = evidence_fit(meta, sc, E, cls, wh, evid) if evid else None
    g_mean = np.asarray(G["g"]) if not G.get("mix") else (np.asarray(G["g"]) + np.asarray(G["mix"]["g2"])) / 2
    if G.get("mix"):                            # judge a mixed cast by the gains where each part of the work is
        uu, vv = np.meshgrid((np.arange(64) + 0.5) * gw / 64, (np.arange(64) + 0.5) * gh / 64)
        cde = cast_de(art, gains_plane(G, uu, vv))
    else:
        cde = cast_de(art, g_mean)
    light_need, color_need = (light_var or 0) > LIGHT_TOL, cde > DE_TOL
    exp_light = ("correct" if evid else "keep") if light_need else "none"
    exp_color = ("correct" if evid else "keep") if color_need else "none"
    if arg == "off":
        exp_light = "keep" if light_need else "none"
        exp_color = "keep" if color_need else "none"
    bow = bow_if_ignored(meta, ggw, ggh)
    tol, aspect_info = aspect_tolerance(meta, meta["corners_undistorted"], f, wh, bool(exif), ggw, ggh, rng_misc)

    # gt: the work as it should come out (only what the evidence and the options allow is undone)
    gt_lin = srgb_to_lin(gt_srgb)
    if exp_color == "keep":
        gt_lin = gt_lin * (g_mean / g_mean.max()).astype(np.float32)
    if exp_light == "keep":
        Ef, _ = sample_photo_field(E, meta, ggw, ggh)
        Ef = np.where(np.isfinite(Ef), Ef, np.nanmedian(Ef))
        gt_lin = gt_lin * (Ef / np.percentile(Ef, 99.5))[..., None]
    gt_out = lin_to_srgb(np.clip(gt_lin, 0, 1)) if (exp_color == "keep" or exp_light == "keep") else gt_srgb
    Image.fromarray(u8(gt_out)).save(os.path.join(folder, "gt.png"))

    # pixels nobody can recover
    ign = np.zeros((ggh, ggw), bool)
    needs = spec.get("glare") or spec.get("hands") or spec.get("cutoff")
    if needs:
        xd, yd = gt_to_photo(meta, *np.meshgrid(np.arange(ggw) + 0.5, np.arange(ggh) + 0.5))
        ok = (xd >= 1) & (yd >= 1) & (xd <= Wp - 1) & (yd <= Hp - 1)
        ign |= ~ok
        xi, yi = np.clip(xd.astype(int), 0, Wp - 1), np.clip(yd.astype(int), 0, Hp - 1)
        if spec.get("hands"):
            ign |= ndi.binary_dilation(cls[yi, xi] == 7, iterations=max(2, int(0.012 * max(ggw, ggh))))
        if glare is not None:
            gl = glare[..., 1] / (rad[..., 1] + 0.02)
            ign |= ndi.binary_dilation(gl[yi, xi] > 0.03, iterations=max(2, int(0.01 * max(ggw, ggh))))
            core = (glare[..., 1] * expo > 0.5)[yi, xi] & ok
            Image.fromarray((core * 255).astype(np.uint8)).save(os.path.join(folder, "glare.png"))
            meta["glare_file"] = "glare.png"
        if ign.any():
            Image.fromarray((ign * 255).astype(np.uint8)).save(os.path.join(folder, "ignore.png"))
            meta["ignore"] = "ignore.png"
    meta["ignored_fraction"] = float(ign.mean())

    # truth fields for the oracle (1/8 size)
    small = (max(1, Wp // 8), max(1, Hp // 8))
    Gsmall = np.broadcast_to(np.asarray(G["g"], np.float32), (small[1], small[0], 3))
    if G.get("mix"):
        Gfield = np.zeros((Hp, Wp, 3), np.float32)
        for y0 in range(0, Hp, 512):
            yy, xx = np.mgrid[y0:min(Hp, y0 + 512), 0:Wp] + 0.5
            X, Y, _ = photo_to_plane(meta, xx, yy)
            Gfield[y0:y0 + 512] = gains_plane(G, X, Y)
        Gsmall = resize_lin(Gfield, small)
    np.savez_compressed(os.path.join(folder, "truth.npz"), E=resize_lin(E, small).astype(np.float16),
                        G=np.ascontiguousarray(Gsmall).astype(np.float16))

    # what must and must not happen
    warn = []
    if spec.get("glare"):
        warn.append("glare")
    if spec.get("cutoff"):
        warn.append("cut_off")
    if spec.get("motion"):
        warn.append("blur")
    if spec.get("size") == "lowres":
        warn.append("resolution")
    if kind == "stress" and tilt > 55:
        warn.append("steep")
    must_not = ["restyle"]
    if exp_color == "keep":
        must_not.append("color_without_evidence")
    if spec["art"] == "red":
        must_not.append("neutralise_red")
    if spec["art"] == "oilgrad":
        must_not.append("flatten_gradient")
    if spec.get("glare"):
        must_not.append("paint_over_glare")
    if kind in ("correct", "option", "stretch"):
        must_not.append("warn_reshoot")
    expect = {"exit": [0] if kind != "stress" else [0, 4], "geometry": True, "unchanged": False,
              "lens": "correct" if bow > STRAIGHT_TOL else "none", "light": exp_light, "color": exp_color,
              "warnings": warn, "say_so": ["no_color_reference"] if exp_color == "keep" and arg != "off" else [],
              "must_not": must_not, "aspect_tol_pct": tol, "visible_corners": [0, 1, 2, 3]}
    if kind == "stress" and "steep" in warn:
        expect["warn_or_refuse"] = True
    if spec.get("perf"):
        expect["perf"] = True                   # bench.py holds the run to the brief's time and memory targets
    if spec.get("cutoff"):
        vis = [i for i, p in enumerate(corners) if 0 <= p[0] <= Wp and 0 <= p[1] <= Hp]
        expect["visible_corners"] = vis
    if arg == "corners":
        meta["args"] = ["--corners", ";".join("%.1f,%.1f" % (p[0], p[1]) for p in corners)]
        expect["quad_source"] = "given"
    elif arg == "frame-keep":
        meta["args"] = ["--frame", "keep"]
        expect["frame"] = {"found": True, "removed": False}
    elif arg == "off":
        meta["args"] = ["--light", "off", "--color", "off"]
        expect["applied"] = {"light": False, "color": False}
    else:
        meta["args"] = []
    if sc.get("frame") and arg != "frame-keep":
        expect["frame"] = {"found": True, "removed": True}
    meta["evidence"] = {"available": evid, "light_fit_residual": fitres, "background": bgk,
                        "background_chroma": chroma_srgb(lin_to_srgb(np.array(sc["bg"]["rgb"]))) if bgk != "room" else None}
    meta["truth"] = {"light_variation_on_work": light_var, "light_range_on_work": float(Ev.max() / Ev.min() - 1) if Ev.size else None,
                     "cast_mean_de": cde, "bow_pct_if_lens_ignored": bow, "aspect": ggw / float(ggh), "aspect_check": aspect_info}
    if kind == "correct" and exp_light == "correct" and (fitres is None or fitres > FIT_OK):
        meta["kind"] = kind = "stretch"
        meta["stretch_reason"] = "light not recoverable from the evidence to %.1f%% (%s)" % (FIT_OK * 100, fitres)
    if spec.get("neighbour"):
        quads["neighbour"] = project(Hm, k1, c, rn, [(sc["neighbour"]["outer"][0], sc["neighbour"]["outer"][1]),
                                                       (sc["neighbour"]["outer"][2], sc["neighbour"]["outer"][1]),
                                                       (sc["neighbour"]["outer"][2], sc["neighbour"]["outer"][3]),
                                                       (sc["neighbour"]["outer"][0], sc["neighbour"]["outer"][3])])[0].tolist()
    meta["expect"] = expect
    meta = rnd(meta)
    with open(os.path.join(folder, "meta.json"), "w") as fjs:
        json.dump(meta, fjs, indent=1)
    return meta


def extra_cases(extras):
    out = []
    for key in extras:
        out.append(C(name="extra-%s-flat" % key, kind="noop", art="extra:" + key, fmt="copy",
                     what="the local picture itself: must pass through unchanged"))
        out.append(C(name="extra-%s-wall" % key, kind="correct", art="extra:" + key, bg="wall", tilt=(14, 24), roll=(-4, 4),
                     fill=(0.35, 0.5), k1=(-0.03, 0.0), exif=True, cast=WARM,
                     light=dict(lamp=("side", 0.2, 0.3), vignette=(0.05, 0.1)),
                     what="the local picture hung on a white wall under a warm lamp"))
    return out


def safe_key(path, taken):
    stem = os.path.splitext(os.path.basename(path))[0]
    parent = os.path.basename(os.path.dirname(os.path.abspath(path)))
    base = stem if stem.lower() not in ("original", "image", "photo", "picture", "img", "source") else parent
    key = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:28] or "picture"
    k, i = key, 2
    while k in taken:
        k, i = "%s-%d" % (key, i), i + 1
    taken.add(k)
    return k


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder for the cases (outside the skill; never commit it)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--extra", nargs="*", default=[], help="local flat pictures to use as extra ground truth")
    ap.add_argument("--real", nargs="*", default=[], help="local real casual photos to run unscored")
    ap.add_argument("--only", help="comma-separated glob patterns of case names to (re)generate")
    a = ap.parse_args()
    root = os.path.abspath(a.out)
    if os.path.commonpath([os.path.realpath(root), os.path.realpath(SKILL)]) == os.path.realpath(SKILL):
        sys.exit("make_photos: --out must be outside the skill folder")
    os.makedirs(root, exist_ok=True)
    taken = set()
    extras = {safe_key(p, taken): os.path.abspath(p) for p in a.extra}
    cases = CASES + extra_cases(extras)
    pats = [p.strip() for p in a.only.split(",")] if a.only else None
    done = []
    for spec in cases:
        if pats and not any(fnmatch.fnmatch(spec["name"], p) for p in pats):
            continue
        if spec.get("fmt") == "copy":
            folder = os.path.join(root, spec["name"])
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder)
            src = extras[spec["art"][6:]]
            ext = os.path.splitext(src)[1].lower() or ".jpg"
            shutil.copyfile(src, os.path.join(folder, "photo" + ext))
            dec = np.asarray(ImageOps.exif_transpose(Image.open(src)).convert("RGB"))
            Image.fromarray(dec).save(os.path.join(folder, "gt.png"))
            meta = noop_meta(spec["name"], spec["what"], "photo" + ext, dec.shape[1], dec.shape[0], a.seed, "extra")
            meta["local_only"] = True
            with open(os.path.join(folder, "meta.json"), "w") as f:
                json.dump(meta, f, indent=1)
        else:
            meta = make_case(spec, a.seed, root, extras)
            if spec["art"].startswith("extra:"):
                meta["local_only"] = True
                with open(os.path.join(root, spec["name"], "meta.json"), "w") as f:
                    json.dump(meta, f, indent=1)
        done.append(spec["name"])
        sys.stderr.write("%-28s %s\n" % (spec["name"], meta["kind"]))
    for p in a.real:
        key = safe_key(p, taken)
        name = "real-" + key
        folder = os.path.join(root, name)
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder)
        ext = os.path.splitext(p)[1].lower() or ".jpg"
        shutil.copyfile(p, os.path.join(folder, "photo" + ext))
        w, h = ImageOps.exif_transpose(Image.open(p)).size
        with open(os.path.join(folder, "meta.json"), "w") as f:
            json.dump({"case": name, "kind": "real", "what": "a real casual photo, no ground truth (unscored)",
                       "version": VERSION, "photo": "photo" + ext, "gt": None, "photo_size": [w, h], "args": [],
                       "local_only": True, "expect": {"exit": [0, 4]}}, f, indent=1)
        done.append(name)
    index = os.path.join(root, "index.json")
    old = {}
    if pats and os.path.exists(index):
        with open(index) as f:
            old = json.load(f)
    names = sorted(set(old.get("cases", [])) | set(done)) if pats else done
    with open(index, "w") as f:
        json.dump({"version": VERSION, "seed": a.seed, "cases": names, "extra": sorted(extras),
                   "real": [os.path.basename(p) for p in a.real]}, f, indent=1)
    print(json.dumps({"out": root, "cases": len(names), "seed": a.seed}))


if __name__ == "__main__":
    main()
