"""Turn a casual phone photo of a painting into the flat, evenly lit picture it shows: `picture.py rectify`.

People photograph a painting on the wall, on a table, in their hands, in its frame or mat, at an angle, under a warm
lamp. The working picture should instead be the painting as a flat scan under neutral light would show it. This module
finds the painting in the photo and undoes only what the photo did to it:

  boundary     Apple Vision rectangles (macOS) and an edge finder of its own (Hough lines on colour edges, convex
               quadrilaterals supported on all four sides, one corner may lie outside the photo), refined to sub-pixel
               edge points along each side, following a gentle lens bow. Of the credible ones the outermost whose edges
               reach its corners is the object. A frame, and a mat inside it, is a band of material that goes all round
               and ends at a straight edge; with --frame auto the painted surface inside is kept. A light, near-neutral
               band at the outer edge is the sheet's own paper margin and stays, as part of the work.
  lens         one radial coefficient, kept only when it clearly straightens the sides.
  perspective  the rectangle's real proportions from the camera's 35 mm-equivalent focal length (EXIF), else the
               focal length its two vanishing points give, else a phone's 26 mm (Zhang & He's whiteboard method);
               resampled once, bicubic with an anti-ringing clamp, at the detail of the nearest part, in linear light.
  light        a smooth illumination field fitted, in the work's own plane, only to neutral, uniform evidence (paper
               margin or mat, else plain wall or table round the work): a robust cubic where the evidence surrounds
               the work, a smoothing spline on what it leaves when that is clearly more than noise; divided out when
               it clearly exceeds the noise.
  colour       von Kries gains in linear light from the same evidence, only along the warm-cool (Planckian) axis and
               only when the evidence is near-neutral; capped. Never grey-world on the painting itself.

It never sharpens, never boosts saturation or contrast, never guesses a light or colour without neutral evidence and
never paints over glare. A flat scan or screenshot of the artwork alone passes through untouched (exit 4). Every
result is a starting point: look at the review sheet. Nothing leaves the machine.

Coordinates are input pixels after EXIF orientation, continuous: pixel (i, j) covers [i, i+1] x [j, j+1].
Lens model: p_distorted = c + (p - c)(1 + k1 r^2), r = |p - c| / half-diagonal, c the image centre; k1 < 0 is barrel.
"""
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from . import imgops as io

DETECT = 1000          # longer side (px) of the copy the boundary is searched on
WORK = 1600            # prepare's default cap: blur and size warnings are judged at this working size
FILM_DIAG = 43.2666    # mm: the 36 x 24 mm frame that 35 mm-equivalent focal lengths refer to (by its diagonal)
PHONE_F35 = 26.0       # typical main phone camera, used only to estimate the tilt when the file has no focal length
MIN_CONF = 0.5         # below this the boundary is not trusted: exit 4, the picture stays as it is
PAPER_WHITE = 0.80     # linear level a white paper or mat reference is mapped to (sRGB 231), never clipped
FIELD_MIN = 0.06       # light variation below this (relative range over the work) is left alone
LUM = np.array([0.2126, 0.7152, 0.0722], np.float32)


# ---------------------------------------------------------------- camera

def _exif_tags(path):
    """(IFD0, Exif IFD) of the file's EXIF as dicts. Pillow reads JPEG, PNG, TIFF and WebP; for HEIC/AVIF (which
    Pillow cannot open) the Exif block is found inside the file by its TIFF header."""
    ex = None
    try:
        with Image.open(path) as im:
            ex = im.getexif()
    except Exception:
        ex = None
    if not ex:
        try:
            with open(path, "rb") as f:
                data = f.read(64 << 20)
            for mark in (b"Exif\x00\x00MM\x00\x2a", b"Exif\x00\x00II\x2a\x00"):
                i = data.find(mark)
                if i >= 0:
                    ex = Image.Exif()
                    ex.load(data[i:i + (1 << 20)])
                    break
        except Exception:
            ex = None
    if not ex:
        return {}, {}
    try:
        sub = dict(ex.get_ifd(0x8769))
    except Exception:
        sub = {}
    return dict(ex), sub


def _num(v):
    try:
        v = float(v)
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def camera(path, size):
    """What the file says about the camera: make, model, 35 mm-equivalent focal length, and the focal length in
    pixels of this picture when it applies (the picture has the shape of the frame the camera recorded)."""
    ifd0, sub = _exif_tags(path)
    f35 = _num(sub.get(0xA405))
    if not f35 and sys.platform == "darwin" and not sub:
        try:      # Spotlight knows it for files it indexed (HEIC from Photos exports, AirDrop)
            p = subprocess.run(["mdls", "-raw", "-name", "kMDItemFocalLength35mm", path],
                               capture_output=True, text=True, timeout=5)
            f35 = _num(p.stdout.strip())
        except Exception:
            f35 = None
    W, H = size
    text = lambda v: (str(v).strip("\x00 ") or None) if v is not None else None
    info = {"make": text(ifd0.get(0x010F)), "model": text(ifd0.get(0x0110)), "focal_35mm": f35,
            "focal_px": None, "focal_note": None,
            "camera_photo": bool(f35 or _num(sub.get(0x920A)))}
    if f35 and 8 <= f35 <= 800:
        ew, eh = _num(sub.get(0xA002)), _num(sub.get(0xA003))
        shape = max(W, H) / float(min(W, H))
        if ew and eh and abs(max(ew, eh) / min(ew, eh) - shape) > 0.015 * shape:
            info["focal_note"] = ("the picture is a crop of the camera frame (EXIF size %dx%d has another shape), "
                                  "so its focal length does not apply" % (ew, eh))
        else:
            info["focal_px"] = f35 / FILM_DIAG * math.hypot(W, H)
    return info


# ---------------------------------------------------------------- colour

_LIN8 = np.arange(256) / 255.0
_LIN8 = np.where(_LIN8 <= 0.04045, _LIN8 / 12.92, ((_LIN8 + 0.055) / 1.055) ** 2.4).astype(np.float32)
_ENC = None


def _to_lin(rgb, rows=256):
    """sRGB as decoded (8-bit levels, or floats 0..1) to linear light, by table, a block of rows at a time (a 48 MP
    photo would otherwise need several full-size temporaries)."""
    if rgb.dtype == np.uint8:
        return _LIN8[rgb]
    out = np.empty(rgb.shape, np.float32)
    for y in range(0, rgb.shape[0], rows):
        out[y:y + rows] = _LIN8[np.clip(np.rint(rgb[y:y + rows] * 255.0), 0, 255).astype(np.uint8)]
    return out


def _to_srgb8(lin, rows=256):
    """Linear light to 8-bit sRGB, by a 16-bit table, a block of rows at a time."""
    global _ENC
    if _ENC is None:
        v = np.linspace(0, 1, 65536)
        _ENC = np.clip(np.rint(255.0 * np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)),
                       0, 255).astype(np.uint8)
    out = np.empty(lin.shape, np.uint8)
    for y in range(0, lin.shape[0], rows):
        out[y:y + rows] = _ENC[np.clip(np.rint(lin[y:y + rows] * 65535.0), 0, 65535).astype(np.int32)]
    return out


def _lin_f(a):
    a = np.clip(np.asarray(a, np.float64), 0, 1)
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _srgb_f(v):
    v = np.clip(np.asarray(v, np.float64), 0, 1)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def _chroma(lin_rgb):
    """CIELAB C*ab (standard units) of a linear RGB colour: how far from neutral it looks."""
    lab = io.rgb_to_lab(_srgb_f(np.asarray(lin_rgb, np.float64)).reshape(1, 1, 3).astype(np.float32))[0, 0] * 100
    return float(math.hypot(lab[1], lab[2]))


def _planck(T):
    """Linear sRGB of a black body at T kelvin (Kim et al. 2002 locus), G = 1."""
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
    M = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]])
    rgb = M @ np.array([x / y, 1.0, (1 - x - y) / y])
    return rgb / rgb[1]


def _warm_axis():
    """Unit direction, in (ln R/G, ln B/G) of linear sRGB, along which a lamp (warm) or shade (cool) moves a neutral
    surface: the Planckian locus through 6500 K, nearly straight in this space."""
    ref = np.log(_planck(6500.0))
    temps = (2500, 3000, 3500, 4500, 5500, 7500, 9500, 12000)
    pts = np.array([np.log(_planck(T))[[0, 2]] - ref[[0, 2]] for T in temps])
    u = np.linalg.svd(pts, full_matrices=False)[2][0]
    return u if u[0] > 0 else -u


WARM = _warm_axis()


# ---------------------------------------------------------------- geometry

def _hom(src, dst):
    """3x3 homography taking src points to dst points (4 or more), normalised DLT."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)

    def norm(p):
        m = p.mean(0)
        s = math.sqrt(2) / max(1e-9, np.sqrt(((p - m) ** 2).sum(1)).mean())
        return np.array([[s, 0, -s * m[0]], [0, s, -s * m[1]], [0, 0, 1.0]])
    Ts, Td = norm(src), norm(dst)
    ps = np.c_[src, np.ones(len(src))] @ Ts.T
    pd = np.c_[dst, np.ones(len(dst))] @ Td.T
    A = []
    for (x, y, _), (u, v, _) in zip(ps, pd):
        A.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        A.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    h = np.linalg.svd(np.array(A))[2][-1].reshape(3, 3)
    Hm = np.linalg.inv(Td) @ h @ Ts
    return Hm / Hm[2, 2]


def _apply(Hm, p):
    p = np.asarray(p, float).reshape(-1, 2)
    q = np.c_[p, np.ones(len(p))] @ Hm.T
    return q[:, :2] / q[:, 2:3]


def _fit_line(p, w=None):
    """Total least squares line through points: unit normal n and offset c with n.p + c = 0."""
    w = np.ones(len(p)) if w is None else w
    m = (p * w[:, None]).sum(0) / max(1e-12, w.sum())
    n = np.linalg.svd((p - m) * np.sqrt(w)[:, None], full_matrices=False)[2][-1]
    return n, -float(n @ m)


def _robust_line(p, iters=8, floor=0.15):
    """Line through points with Tukey weights; returns (n, c), residuals and their robust scale."""
    w = np.ones(len(p))
    n, c = _fit_line(p, w)
    s = floor
    for _ in range(iters):
        r = p @ n + c
        s = max(floor, 1.4826 * float(np.median(np.abs(r))))
        u = r / (4.685 * s)
        w = np.where(np.abs(u) < 1, (1 - u * u) ** 2, 0.0)
        if w.sum() < 3:
            break
        n, c = _fit_line(p, w)
    return (n, c), p @ n + c, s


def _meet(l1, l2):
    (n1, c1), (n2, c2) = l1, l2
    return np.linalg.solve(np.array([n1, n2]), np.array([-c1, -c2]))


def _order(q):
    """TL, TR, BR, BL order for the corners of a convex quadrilateral (image y down)."""
    q = np.asarray(q, float)
    c = q.mean(0)
    q = q[np.argsort(np.arctan2(q[:, 1] - c[1], q[:, 0] - c[0]))]
    return np.roll(q, -int(np.argmin(q.sum(1))), axis=0)


def _area(q):
    x, y = q[:, 0], q[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _convex(q):
    e = np.roll(q, -1, 0) - q
    z = e[:, 0] * np.roll(e, -1, 0)[:, 1] - e[:, 1] * np.roll(e, -1, 0)[:, 0]
    return bool((z > 0).all() or (z < 0).all())


def _inside(q, outer, margin=0.0):
    """Whether every corner of q lies inside the convex quad outer, at least margin px from its sides."""
    o = _order(outer)
    e0, e1 = o[1] - o[0], o[2] - o[1]
    sign = 1.0 if e0[0] * e1[1] - e0[1] * e1[0] > 0 else -1.0
    for i in range(4):
        a, b = o[i], o[(i + 1) % 4]
        e = b - a
        d = sign * (e[0] * (q[:, 1] - a[1]) - e[1] * (q[:, 0] - a[0])) / max(1e-9, float(np.linalg.norm(e)))
        if (d < margin).any():
            return False
    return True


def _sides(q):
    return [float(np.linalg.norm(q[(i + 1) % 4] - q[i])) for i in range(4)]     # top, right, bottom, left


def _zhang_he(q, f, c):
    """Width/height of the rectangle seen as quad q (TL, TR, BR, BL) by a camera of focal length f px with principal
    point c (Zhang & He, whiteboard scanning, 2007); also the plane's unit normal, and the focal length the quad
    itself implies (None when its sides are too close to parallel to say)."""
    m1, m2, m4, m3 = [np.array([x, y, 1.0]) for x, y in q]          # TL (0,0), TR (w,0), BR (w,h), BL (0,h)
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    u0, v0 = c
    f_self = None
    den = n2[2] * n3[2]
    if abs(den) > 1e-12:
        f2 = -((n2[0] * n3[0] - (n2[0] * n3[2] + n2[2] * n3[0]) * u0 + den * u0 * u0) +
               (n2[1] * n3[1] - (n2[1] * n3[2] + n2[2] * n3[1]) * v0 + den * v0 * v0)) / den
        f_self = math.sqrt(f2) if f2 > 0 else None
    if not f:
        return None, None, f_self
    Ai = np.linalg.inv(np.array([[f, 0, u0], [0, f, v0], [0, 0, 1.0]]))
    r1, r2 = Ai @ n2, Ai @ n3
    nrm = np.cross(r1 / np.linalg.norm(r1), r2 / np.linalg.norm(r2))
    return math.sqrt((r1 @ r1) / (r2 @ r2)), nrm / np.linalg.norm(nrm), f_self


# ---------------------------------------------------------------- lens

def _distort(x, y, k, c, R):
    vx, vy = x - c[0], y - c[1]
    g = 1 + k * (vx * vx + vy * vy) / (R * R)
    return c[0] + vx * g, c[1] + vy * g


def _undistort(x, y, k, c, R):
    if not k:
        return x, y
    vx, vy = x - c[0], y - c[1]
    rd = np.sqrt(vx * vx + vy * vy) / R
    ru = rd.copy()
    for _ in range(6):
        ru = ru - (ru * (1 + k * ru * ru) - rd) / (1 + 3 * k * ru * ru)
    g = np.where(rd > 1e-12, ru / np.maximum(rd, 1e-12), 1.0)
    return c[0] + vx * g, c[1] + vy * g


def _straightness(sides, k, c, R):
    """RMS distance (px) of the edge points of every side from the straight line through them, after undistorting."""
    tot, n = 0.0, 0
    for p in sides:
        x, y = _undistort(p[:, 0], p[:, 1], k, c, R)
        q = np.c_[x, y]
        nrm, cc = _fit_line(q)
        r = q @ nrm + cc
        tot += float((r * r).sum())
        n += len(r)
    return math.sqrt(tot / max(1, n))


def _lens_fit(sides, c, R):
    """The k1 that makes the sides straightest, by a coarse scan and a golden-section search."""
    sides = [p for p in sides if len(p) >= 12]
    if len(sides) < 3:
        return 0.0, None, None
    ks = np.linspace(-0.3, 0.3, 61)
    cs = [_straightness(sides, k, c, R) for k in ks]
    i = int(np.argmin(cs))
    a, b = ks[max(0, i - 1)], ks[min(len(ks) - 1, i + 1)]
    g = (math.sqrt(5) - 1) / 2
    x1, x2 = b - g * (b - a), a + g * (b - a)
    f1, f2 = _straightness(sides, x1, c, R), _straightness(sides, x2, c, R)
    for _ in range(24):
        if f1 < f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - g * (b - a)
            f1 = _straightness(sides, x1, c, R)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + g * (b - a)
            f2 = _straightness(sides, x2, c, R)
    k = (a + b) / 2
    return float(k), _straightness(sides, 0.0, c, R), _straightness(sides, k, c, R)


# ---------------------------------------------------------------- edges on the detection copy

def _small(rgb8):
    """The detection copy (float, longer side DETECT) of the 8-bit working picture, and its scale."""
    H, W = rgb8.shape[:2]
    s = min(1.0, DETECT / float(max(H, W)))
    if s >= 1.0:
        return rgb8.astype(np.float32) / 255.0, 1.0
    size = (max(1, int(round(W * s))), max(1, int(round(H * s))))
    im = Image.fromarray(rgb8).resize(size, Image.LANCZOS)
    return np.asarray(im, np.float32) / 255.0, size[0] / float(W)


def _u8(a, rows=256):
    """8-bit copy of a float picture, a block of rows at a time."""
    out = np.empty(a.shape, np.uint8)
    for y in range(0, a.shape[0], rows):
        out[y:y + rows] = np.clip(a[y:y + rows] * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out


def _edge_maps(small):
    """Colour edge strength (Di Zenzo), its normal direction (0..pi) and thin edges (non-maximum suppressed)."""
    lab = io.rgb_to_lab(small)
    Jxx = Jyy = Jxy = 0.0
    for ch, wgt in ((lab[..., 0], 1.0), (lab[..., 1], 0.5), (lab[..., 2], 0.5)):
        g = ndi.gaussian_filter(ch * wgt, 1.2)
        gx, gy = ndi.sobel(g, 1) / 8.0, ndi.sobel(g, 0) / 8.0
        Jxx, Jyy, Jxy = Jxx + gx * gx, Jyy + gy * gy, Jxy + gx * gy
    mag = np.sqrt(0.5 * (Jxx + Jyy + np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy * Jxy)))
    ang = (0.5 * np.arctan2(2 * Jxy, Jxx - Jyy)) % np.pi
    t = max(0.008, 1.5 * float(np.median(mag)))
    return mag, ang, _thin(mag, ang) & (mag > t), t


def _thin(mag, ang):
    """Non-maximum suppression across the edge (normal angle ang, 0..pi, quantised to 45 degrees)."""
    q = (np.round(ang / (np.pi / 4)) % 4).astype(np.int8)
    P = np.pad(mag, 1)
    h, w = mag.shape
    thin = np.zeros(mag.shape, bool)
    for k, (dy, dx) in enumerate(((0, 1), (1, 1), (1, 0), (1, -1))):
        thin |= (q == k) & (mag >= P[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]) & \
            (mag > P[1 - dy:1 - dy + h, 1 - dx:1 - dx + w])
    return thin


def _hough(edges, ang, mag, t, n=60):
    """Straight lines (normal angle, offset from the centre, votes) from oriented edge pixels."""
    h, w = edges.shape
    ys, xs = np.nonzero(edges)
    if len(xs) < 50:
        return []
    wt = np.minimum(1.0, mag[ys, xs] / (3 * t))
    th0 = ang[ys, xs]
    X, Y = xs + 0.5 - w / 2.0, ys + 0.5 - h / 2.0
    step = math.radians(0.5)
    nth = int(round(math.pi / step))
    D = int(math.ceil(math.hypot(w, h) / 2)) + 2
    nr = 2 * D + 1
    acc = np.zeros(nth * nr)
    for dk in range(-6, 7):                         # +-3 degrees round each pixel's own direction
        ti = np.round(th0 / step + dk).astype(np.int64) % nth
        r = X * np.cos(ti * step) + Y * np.sin(ti * step)
        acc += np.bincount(ti * nr + np.round(r).astype(np.int64) + D, weights=wt * (1 - abs(dk) / 9.0),
                           minlength=nth * nr)
    # a line's votes straddle neighbouring bins: sum them (votes ~ its length in px), then keep local maxima; a
    # frame's mouldings lie 3-5 px apart, so the window is small
    acc = ndi.uniform_filter(acc.reshape(nth, nr), 3, mode=("wrap", "constant")) * 9.0
    mx = ndi.maximum_filter(acc, size=(7, 7), mode=("wrap", "constant"))
    pk = np.argwhere((acc == mx) & (acc >= 0.1 * min(w, h)))
    vals = acc[pk[:, 0], pk[:, 1]]
    lines = []
    for i in np.argsort(-vals):
        th, rho = pk[i, 0] * step, float(pk[i, 1] - D)
        dup = False
        for th2, rho2, _ in lines:                  # the same line found across the 0 / pi wrap, or twice
            if abs(math.sin(th - th2)) < 0.03:
                if abs(rho - (rho2 if math.cos(th - th2) > 0 else -rho2)) < 3:
                    dup = True
                    break
        if not dup:
            lines.append((th, rho, float(vals[i])))
        if len(lines) >= n:
            break
    return lines


def _supports(lines, edges, ang, shape):
    """For each line, the running count of samples along it that sit on an edge of the same direction."""
    h, w = shape
    D = int(math.ceil(math.hypot(w, h) / 2)) + 2
    ts = np.arange(-D, D + 1, dtype=float)
    cums = []
    for th, rho, _ in lines:
        nx, ny = math.cos(th), math.sin(th)
        px, py = w / 2.0 + rho * nx - ts * ny, h / 2.0 + rho * ny + ts * nx
        hit = np.zeros(len(ts), bool)
        for o in (-1.0, 0.0, 1.0):
            xi, yi = np.floor(px + o * nx).astype(int), np.floor(py + o * ny).astype(int)
            ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
            e = np.zeros(len(ts), bool)
            e[ok] = edges[yi[ok], xi[ok]] & (np.abs(np.sin(ang[yi[ok], xi[ok]] - th)) < 0.21)
            hit |= e
        cums.append(np.concatenate([[0], np.cumsum(hit)]))
    return np.array(cums, dtype=np.float64), D


def _edge_quads(small, mag, ang, edges, t, top=16):
    """Convex quadrilaterals whose four sides are well supported by straight edges, best first (detection px)."""
    h, w = small.shape[:2]
    lines = _hough(edges, ang, mag, t)
    N = len(lines)
    if N < 4:
        return []
    cums, D = _supports(lines, edges, ang, (h, w))
    th = np.array([l[0] for l in lines])
    rho = np.array([l[1] for l in lines])
    nx, ny = np.cos(th), np.sin(th)
    r = rho + nx * w / 2.0 + ny * h / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        det = nx[:, None] * ny[None, :] - ny[:, None] * nx[None, :]
        Px = (r[:, None] * ny[None, :] - ny[:, None] * r[None, :]) / det
        Py = (nx[:, None] * r[None, :] - r[:, None] * nx[None, :]) / det
    T = (Px - w / 2.0) * (-ny[:, None]) + (Py - h / 2.0) * nx[:, None]      # T[i, j]: where j meets i, along i
    mind = min(w, h)
    pairs, pdir, pvote = [], [], []
    for i in range(N):
        for j in range(i + 1, N):
            if abs(math.sin(th[i] - th[j])) < 0.5:
                s = 1.0 if math.cos(th[i] - th[j]) > 0 else -1.0
                if abs(rho[i] - s * rho[j]) >= 0.08 * mind:
                    pairs.append((i, j))
                    pdir.append(math.atan2(math.sin(2 * th[i]) + math.sin(2 * th[j]),
                                           math.cos(2 * th[i]) + math.cos(2 * th[j])) / 2)
                    pvote.append(lines[i][2] + lines[j][2])
    if len(pairs) < 2:
        return []
    keep = np.argsort(-np.array(pvote))[:500]
    pairs = [pairs[k] for k in keep]
    pdir = np.array([pdir[k] for k in keep])
    A, B = np.triu_indices(len(pairs), 1)
    sel = np.abs(np.sin(pdir[A] - pdir[B])) > math.sin(math.radians(40))
    A, B = A[sel], B[sel]
    if not len(A):
        return []
    pr = np.array(pairs)
    I, J, K, L = pr[A, 0], pr[A, 1], pr[B, 0], pr[B, 1]
    ok = (I != K) & (I != L) & (J != K) & (J != L)
    I, J, K, L = I[ok], J[ok], K[ok], L[ok]
    C = np.stack([np.stack([Px[a, b], Py[a, b]], -1) for a, b in ((I, K), (I, L), (J, L), (J, K))], 1)  # (n,4,2)
    fin = np.isfinite(C).all((1, 2))
    tol = 1.5
    with np.errstate(invalid="ignore"):
        cin = (C[..., 0] > -tol) & (C[..., 0] < w + tol) & (C[..., 1] > -tol) & (C[..., 1] < h + tol)
        near = (C[..., 0] > -0.25 * w) & (C[..., 0] < 1.25 * w) & (C[..., 1] > -0.25 * h) & (C[..., 1] < 1.25 * h)
    inside = fin & near.all(1) & (cin.sum(1) >= 3)      # one corner may lie outside: a work cut off by the photo
    e = np.roll(C, -1, 1) - C
    z = e[..., 0] * np.roll(e, -1, 1)[..., 1] - e[..., 1] * np.roll(e, -1, 1)[..., 0]
    conv = (z > 0).all(1) | (z < 0).all(1)
    lens = np.linalg.norm(e, axis=2)
    ok = inside & conv & (lens.min(1) > 0.1 * mind)
    if not ok.any():
        return []
    I, J, K, L, C, lens = I[ok], J[ok], K[ok], L[ok], C[ok], lens[ok]

    # where each line is inside the picture (its t range), so a side running out of the photo is judged on the part
    # that is in it
    with np.errstate(divide="ignore", invalid="ignore"):
        x0 = w / 2.0 + rho * nx
        y0 = h / 2.0 + rho * ny
        tx = np.sort(np.stack([(x0 - 0) / ny, (x0 - w) / ny], 1), 1)          # x = x0 - t ny
        ty = np.sort(np.stack([(0 - y0) / nx, (h - y0) / nx], 1), 1)          # y = y0 + t nx
        tx[np.abs(ny) < 1e-9] = [-np.inf, np.inf]
        ty[np.abs(nx) < 1e-9] = [-np.inf, np.inf]
    tv0, tv1 = np.maximum(tx[:, 0], ty[:, 0]), np.minimum(tx[:, 1], ty[:, 1])

    def sup(li, ta, tb):
        lo, hi = np.minimum(ta, tb), np.maximum(ta, tb)
        pad = 0.04 * (hi - lo)                      # corners may be rounded, occluded or shadowed
        vlo, vhi = np.maximum(lo + pad, tv0[li]), np.minimum(hi - pad, tv1[li])
        a = np.clip(np.floor(vlo + D).astype(int), 0, 2 * D + 1)
        b = np.clip(np.ceil(vhi + D).astype(int), 0, 2 * D + 1)
        s = (cums[li, b] - cums[li, a]) / np.maximum(1, b - a)
        return np.where(vhi - vlo >= 0.5 * (hi - lo - 2 * pad), s, 0.0)
    S = np.stack([sup(I, T[I, K], T[I, L]), sup(L, T[L, I], T[L, J]), sup(J, T[J, L], T[J, K]),
                  sup(K, T[K, J], T[K, I])], 1)
    x, y = C[..., 0], C[..., 1]
    area = 0.5 * np.abs((x * np.roll(y, -1, 1) - y * np.roll(x, -1, 1)).sum(1)) / float(w * h)
    smin, smean = S.min(1), S.mean(1)
    good = (smin >= 0.4) & (area >= 0.05)
    if not good.any():
        return []
    score = smean * np.sqrt(smin) * area ** 0.25
    idx = np.where(good)[0]
    out, diag = [], math.hypot(w, h)
    # the best supported, and the largest well supported: a frame's outer edge lies only a few px outside its
    # mouldings and the painting's edge, so near-duplicates are suppressed finely
    by_score = idx[np.argsort(-score[idx])]
    by_area = [k for k in idx[np.argsort(-area[idx])] if smin[k] >= 0.6]
    for order, n in ((by_score, top), (by_area, top // 2)):
        added = 0
        for k in order:
            if added >= n:
                break
            q = _order(C[k])
            if any(np.abs(q - o["quad"]).max() < 0.005 * diag for o in out):
                continue
            out.append({"quad": q, "support": float(smin[k]), "score": float(score[k]), "area": float(area[k])})
            added += 1
    return out


# ---------------------------------------------------------------- sub-pixel refinement at full resolution

def _sample(img, x, y):
    """Bilinear colour (0..1) at continuous coordinates (any shape), from a float or an 8-bit picture."""
    co = [np.ravel(y) - 0.5, np.ravel(x) - 0.5]
    out = np.stack([ndi.map_coordinates(img[..., c], co, order=1, mode="nearest", output=np.float32)
                    for c in range(3)], -1).reshape(np.shape(x) + (3,))
    return out / np.float32(255.0) if img.dtype == np.uint8 else out


def _side(img, a, b, ctr, win, passes=2):
    """Sub-pixel edge points along the side a-b, its line, and how well the edge holds along it.

    Profiles across the side are projected on the colour difference between its two sides, so a boundary that is a
    change of colour more than of tone still counts, and the polarity stays the same along the side."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.linalg.norm(b - a))
    res = {"pts": np.zeros((0, 2)), "line": None, "support": 0.0, "contrast": 0.0, "resid": None,
           "tpos": np.zeros(0), "visible": 1.0}
    if L < 8:
        return res
    d = (b - a) / L
    n = np.array([d[1], -d[0]])
    if n @ ((a + b) / 2 - ctr) < 0:
        n = -n
    m = int(np.clip(L / 5, 24, 240))
    ts = np.linspace(0.05 * L, 0.95 * L, m)
    base = a + ts[:, None] * d
    h_, w_ = img.shape[:2]
    seen = (base[:, 0] > 2) & (base[:, 0] < w_ - 2) & (base[:, 1] > 2) & (base[:, 1] < h_ - 2)
    m_eff = int(seen.sum())                 # a side running out of the photo is judged on its visible part
    res["visible"] = m_eff / float(m)
    if m_eff < max(8, 0.4 * m):
        return res
    line, curve = None, None
    u_t = (ts - ts.mean()) / max(1e-9, 0.5 * (ts[-1] - ts[0]))          # -1..1 along the side
    base0, n0 = base.copy(), n.copy()
    wide = max(win, 0.015 * L)          # a strongly bowed side (barrel distortion) strays this far from its chord
    it, w = 0, win
    while it < passes:
        if it == 0 and w > win:         # second try of the first pass, wider
            base, n, line, curve = base0.copy(), n0.copy(), None, None
        if it > 0:
            w = 3.0
        if line is not None:            # recentre the samples on the line found by the last pass
            nl, cl = line
            base = base - (base @ nl + cl)[:, None] * nl
            if nl @ n < 0:
                nl = -nl
            n = nl
            if curve is not None:       # and on its gentle bow (lens distortion), so the ends are found too
                base = base + (curve @ np.stack([np.ones(m), u_t, u_t * u_t]))[:, None] * n
        ss = np.arange(-w, w + 0.25, 0.5)
        along = np.array([-1.0, 0.0, 1.0])
        P = base[:, None, None, :] + ss[None, :, None, None] * n + along[None, None, :, None] * d
        col = _sample(img, P[..., 0], P[..., 1]).mean(2)                     # (m, len(ss), 3)
        k = max(1, len(ss) // 4)
        v = np.median(col[:, -k:].mean(1) - col[:, :k].mean(1), axis=0)
        cn = float(np.linalg.norm(v))
        retry = it == 0 and w == win and wide > 1.3 * win
        if cn < 0.012:
            if retry:
                w = wide
                continue
            return res
        prof = col @ (v / cn)
        der = np.gradient(prof, 0.5, axis=1)
        der = (np.roll(der, 1, 1) + 2 * der + np.roll(der, -1, 1)) / 4
        der[:, 0] = der[:, -1] = -np.inf
        j = np.argmax(der, 1)
        jj = np.clip(j, 1, len(ss) - 2)
        y0, y1, y2 = der[np.arange(m), jj - 1], der[np.arange(m), jj], der[np.arange(m), jj + 1]
        with np.errstate(invalid="ignore", divide="ignore"):
            frac = np.where(np.isfinite(y0 + y2) & (y0 - 2 * y1 + y2 < 0), 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2), 0.0)
        frac = np.clip(frac, -0.5, 0.5)
        sp = ss[jj] + frac * 0.5
        strength = y1
        med = float(np.median(strength[np.isfinite(strength)])) if np.isfinite(strength).any() else 0.0
        valid = np.isfinite(strength) & (strength > max(0.3 * med, 0.004)) & (j > 0) & (j < len(ss) - 1) & seen
        if valid.sum() < max(8, 0.2 * m_eff):
            if retry:
                w = wide
                continue
            return res
        pts = base[valid] + sp[valid, None] * n
        line, r, s = _robust_line(pts)
        inl = np.abs(r) < max(0.8, 3 * s)
        ends = []
        for lo, hi in ((0.0, 0.2), (0.8, 1.0)):           # how far the edge reaches each end of the side (found at
            win_ = seen & (ts >= lo * L) & (ts <= hi * L)    # all, on the line or not: a bowed side still counts)
            ends.append(float(valid[win_].mean()) if win_.any() else None)
        if retry and (inl.sum() < 0.7 * m_eff or min([e for e in ends if e is not None] or [1.0]) < 0.5):
            w = wide                    # the edge left the window over part of the side: look wider once
            continue
        # the edge may bow gently (barrel or pincushion distortion: up to about 1% of the side): follow a quadratic
        # course along the side, at most 2% of its length off straight, so its ends count and the lens fit sees them
        V = np.stack([np.ones(valid.sum()), u_t[valid], u_t[valid] ** 2], 1)
        off = r if line[0] @ n >= 0 else -r              # offsets along the normal the next pass will use
        wq = np.ones(len(r))
        for _ in range(6):
            cq = np.linalg.lstsq(V * wq[:, None], off * wq, rcond=None)[0]
            rq = off - V @ cq
            sq = max(0.15, 1.4826 * float(np.median(np.abs(rq))))
            wq = np.where(np.abs(rq) < 4.685 * sq, (1 - (rq / (4.685 * sq)) ** 2) ** 2, 0.0)
        inq = np.abs(rq) < max(0.8, 3 * sq)
        if abs(cq[2]) <= 0.02 * L and sq < 0.8 * s and inq.sum() >= 0.8 * inl.sum():
            curve = cq if it < passes - 1 else None
            inl, r = inq, rq
        res.update(pts=pts[inl], line=line, support=float(inl.sum()) / m_eff, contrast=cn,
                   resid=float(np.sqrt(np.mean(r[inl] ** 2))) if inl.any() else None, tpos=ts[valid][inl] / L)
        res["ends"] = ends
        res["normal"], res["axis"] = n, v / cn       # for the edge width, measured later at working size
        it += 1
    return res


def _rise(prof, step):
    """10-90% rise (in units of step) of the edge at the middle of each profile (n, m): over the derivative's main
    lobe only (where it stays above 15% of its peak) and in the edge's own polarity, so neither a moulding beside
    the edge nor a sharpening halo widens it; a box-shaped lobe (camera shake) counts whole. NaN without an edge."""
    d = ndi.uniform_filter1d(np.gradient(prof, axis=1), 3, axis=1)
    n, m = d.shape
    c = m // 2
    d = np.clip(d * np.sign(d[:, c - 3:c + 4].sum(1, keepdims=True)), 0, None)
    out = np.full(n, np.nan)
    for i in range(n):
        row = d[i]
        k = c - m // 6 + int(np.argmax(row[c - m // 6:c + m // 6 + 1]))
        pk = row[k]
        if pk <= 1e-6:
            continue
        lo, hi = k, k
        while lo > 0 and row[lo - 1] > 0.15 * pk:
            lo -= 1
        while hi < m - 1 and row[hi + 1] > 0.15 * pk:
            hi += 1
        cs = np.r_[0.0, np.cumsum(row[lo:hi + 1])]           # mass up to each sample's edges
        if cs[-1] <= 0.4 * row.sum():
            continue                    # not the profile's main step: a noise ripple on a smooth slope
        xe = np.arange(lo - 0.5, hi + 1.0)
        out[i] = (np.interp(0.9 * cs[-1], cs, xe) - np.interp(0.1 * cs[-1], cs, xe)) * step
    return out


def _side_widths(img, sides, scale, reach=12.0):
    """Width (10-90% rise, working px) of the boundary's edge along each side: the median over up to 64 places,
    profiles reach working px either way. None for a side without edge points."""
    out = []
    for sd in sides or []:
        p = sd.get("pts")
        if p is None or len(p) < 12 or sd.get("normal") is None:
            out.append(None)
            continue
        p = p[np.linspace(0, len(p) - 1, min(64, len(p))).astype(int)]
        ss = np.arange(-reach, reach + 0.01, 0.5) / max(scale, 1e-6)
        Q = p[:, None, :] + ss[None, :, None] * sd["normal"]
        prof = _sample(img, Q[..., 0], Q[..., 1]) @ sd["axis"]
        w = _rise(prof, 0.5)
        w = w[np.isfinite(w)]
        out.append(float(np.median(w)) if len(w) >= 8 else None)
    return out


def _refine(img, quad, win):
    """Refine a quadrilateral on the full-resolution picture: four sides of sub-pixel edge points and their lines,
    and which corners are real (an edge reaches the corner along at least one of its sides: not a point where the
    lines of two different objects, a painting and its neighbour, happen to meet; one outside the photo is
    unknown, None)."""
    q = np.asarray(quad, float)
    ctr = q.mean(0)
    sides = [_side(img, q[s], q[(s + 1) % 4], ctr, win) for s in range(4)]
    out = {"sides": sides, "quad": q, "support": min(s["support"] for s in sides)}
    if all(s["line"] is not None for s in sides):
        try:
            c = [_meet(sides[(s - 1) % 4]["line"], sides[s]["line"]) for s in range(4)]
            nq = _order(np.array(c))
            if _convex(nq) and np.abs(nq - q).max() < 3 * win + 4:
                out["quad"] = nq
        except np.linalg.LinAlgError:
            pass
    h, w = img.shape[:2]
    corners = []
    for s in range(4):
        x, y = out["quad"][s]
        reach = [v for v in ((sides[s].get("ends") or [None, None])[0], (sides[s - 1].get("ends") or [None, None])[1])
                 if v is not None]
        if not (-1 <= x <= w + 1 and -1 <= y <= h + 1):
            corners.append(None)
        else:
            corners.append(bool(reach) and max(reach) >= 0.3)
    out["corners"] = corners
    return out


def _lines_quad(sides, k, c, R):
    """Corners (undistorted coordinates) where the straight lines through each side's undistorted points meet."""
    ls = []
    for s in sides:
        p = s["pts"]
        if len(p) < 6:
            return None
        x, y = _undistort(p[:, 0], p[:, 1], k, c, R)
        ls.append(_robust_line(np.c_[x, y])[0])
    try:
        return _order(np.array([_meet(ls[(i - 1) % 4], ls[i]) for i in range(4)]))
    except np.linalg.LinAlgError:
        return None


# ---------------------------------------------------------------- Apple Vision

def _vision_quads(path, size):
    """Rectangles Apple Vision sees (document and rectangle detectors), or ([], why not)."""
    try:
        from . import apple
    except ImportError as e:
        return [], "Vision not importable (%s)" % e
    try:
        found = apple.quads(path, max_n=8)
    except AttributeError:
        return [], "this apple.py has no quads()"
    except Exception as e:      # apple.Unavailable (not a Mac, no compiler) or a failed run: the edge finder suffices
        return [], "%s: %s" % (type(e).__name__, e)
    W, H = size
    out = []
    for f in found or []:
        try:
            q = _order(np.array(f["corners"], float).reshape(4, 2))
        except Exception:
            continue
        if (q[:, 0] < -0.02 * W).any() or (q[:, 0] > 1.02 * W).any() or (q[:, 1] < -0.02 * H).any() or \
                (q[:, 1] > 1.02 * H).any() or not _convex(q):
            continue
        out.append({"quad": q, "confidence": float(f.get("confidence", 0.5)), "kind": f.get("source", "rectangle")})
    return out, None


# ---------------------------------------------------------------- frame, mat and paper margin

def _rectified(img, quad, size):
    """Bilinear rectification of quad (in img px) to size (w, h); also the map back to img px."""
    w, h = size
    Hm = _hom([[0, 0], [w, 0], [w, h], [0, h]], quad)
    yy, xx = np.mgrid[0:h, 0:w]
    p = _apply(Hm, np.c_[xx.ravel() + 0.5, yy.ravel() + 0.5])
    return _sample(img, p[:, 0], p[:, 1]).reshape(h, w, 3), Hm


def _texture(rows, blocks=1):
    """Per row of a strip: how far its colours stray from a smooth (quadratic) course along it, 80th percentile, dE.
    A lamp's gradient along a mat is smooth and does not count; a painting's marks, even sparse ones, do. With
    blocks, the row is judged in that many pieces and the roughest one is forgiven (a thumb on the margin)."""
    n = rows.shape[1]
    if blocks > 1 and n >= 16 * blocks:
        cut = np.linspace(0, n, blocks + 1).astype(int)
        per = np.stack([_texture(rows[:, a:b]) for a, b in zip(cut[:-1], cut[1:])], 1)
        return np.sort(per, axis=1)[:, blocks - 2]
    t = np.linspace(-1, 1, n)
    V = np.c_[np.ones(n), t, t * t]
    coef = np.einsum("kj,djc->dkc", np.linalg.pinv(V), rows)
    return np.percentile(np.linalg.norm(rows - np.einsum("jk,dkc->djc", V, coef), axis=2), 80, axis=1)


def _segments(rows, tau):
    """Split a strip (row d lies d px in from the side) at straight steps parallel to the side and where it turns
    rough. Returns the segments, each [start row, end row, median Lab colour, uniform, ends at a straight step,
    that step's line (row = a + b k) or None], and the texture of every row."""
    dmax, n = rows.shape[:2]
    u = _texture(rows, 4)
    st = np.zeros((dmax, n))
    st[1:-1] = np.linalg.norm(rows[2:] - rows[:-2], axis=2)
    stw = ndi.maximum_filter1d(st, 5, axis=0)          # a slightly slanted straight edge still counts
    prof = np.median(stw, axis=1)
    peaks = [d for d in range(2, dmax - 2) if prof[d] >= 4.0 and prof[d] == prof[max(0, d - 3):d + 4].max()]
    steps, kk = [], np.arange(n, dtype=float)
    for d in peaks:
        lo, hi = max(1, d - 3), min(dmax - 1, d + 4)
        at = lo + np.argmax(st[lo:hi], axis=0).astype(float)
        A = np.c_[np.ones(n), kk]
        w = np.ones(n)
        for _ in range(4):
            ab = np.linalg.lstsq(A * w[:, None], at * w, rcond=None)[0]
            w = (np.abs(at - A @ ab) <= 1.5).astype(float)
            if w.sum() < 3:
                break
        if w.mean() >= 0.6 and abs(ab[1]) < 0.06:
            steps.append((float(ab[0] + ab[1] * (n - 1) / 2.0), ab))
    cuts = [0] + [int(round(c)) for c, _ in steps] + [dmax]
    segs = []
    for i in range(len(cuts) - 1):
        a, b = cuts[i] + 2, cuts[i + 1] - 1             # keep clear of the blur either side of a step
        if b - a < 2:
            continue
        rough = np.where(u[a:b] > tau)[0]
        end = a + int(rough[0]) if len(rough) else b
        if end - a >= 2:
            segs.append([a, end, np.median(rows[a:end].reshape(-1, 3), axis=0), True, i + 1 < len(cuts) - 1
                         and end == b, steps[i][1] if i < len(steps) and end == b else None])
        if len(rough):
            segs.append([end, b, None, False, False, None])
            break
    return segs, u


def _same_material(ci, cj, tol=0.1):
    """Whether two linear colours can be one material under different amounts of the same light: equal
    chromaticity over the channels neither has clipped (a lamp clips red first near white), lightness within 2.5x."""
    yr = float(ci @ LUM) / float(cj @ LUM)
    if not 0.4 <= yr <= 2.5:
        return False
    ok = (ci < 0.95) & (cj < 0.95)
    if ok.sum() < 2:
        return bool(min(ci @ LUM, cj @ LUM) >= 0.4)            # both clipped white
    k = np.where(ok)[0]
    li, lj = np.log(ci[k]), np.log(cj[k])
    return bool(np.abs((li - li[0]) - (lj - lj[0])).max() <= tol)


def _bands(small, quad, aspect, whole=False):
    """Look inward from each side of the outer boundary for bands of material that go all round: a frame, a mat, a
    paper margin. Per side: the band depth (fraction of that dimension), whether it ends at a straight edge (and
    that edge), its colour, and the rows of light, near-neutral material (evidence for light and colour).

    A painting can have a flat area along one edge (a sky) that is as uniform as a mat; it is told apart because
    it does not go all round. Each side's strip is cut at straight steps parallel to the side; the segments'
    colours are grouped across the four sides; a side's band is its outer run of uniform segments whose colour
    group appears on every side. What may be removed is a frame (darker or coloured material at the outer edge)
    and a mat just inside it; a light, near-neutral band at the outer edge is the sheet's own paper margin and
    belongs to the work. whole: the picture's border is the outer edge (no boundary was found)."""
    pw = 560 if aspect >= 1 else max(80, int(round(560 * aspect)))
    ph = max(80, int(round(pw / aspect)))
    R, Hm = _rectified(small, quad, (pw, ph))
    lab = io.rgb_to_lab(R) * 100.0
    lin = _lin_f(R)
    # Each side as a strip whose row d lies d px in from that side (top, right, bottom, left), and the map from
    # strip (row d, column k) back to preview pixels (x, y).
    turn = (lambda a: a, lambda a: np.transpose(a[:, ::-1], (1, 0, 2)), lambda a: a[::-1],
            lambda a: np.transpose(a, (1, 0, 2)))
    to_xy = (lambda d, k: (k, d), lambda d, k: (pw - 1 - d, k), lambda d, k: (k, ph - 1 - d), lambda d, k: (d, k))
    mid = lab[int(ph * 0.3):int(ph * 0.7), int(pw * 0.15):int(pw * 0.85)]
    u_int = float(np.median(np.concatenate([_texture(mid), _texture(np.transpose(mid, (1, 0, 2)))])))
    tau = float(np.clip(0.4 * u_int, 2.0, 5.0))
    per = []
    for s in range(4):
        S, SL = turn[s](lab), turn[s](lin)
        k0, k1 = int(S.shape[1] * 0.15), int(S.shape[1] * 0.85)
        dm = int(S.shape[0] * 0.36)
        rows = S[:dm, k0:k1]
        segs, u = _segments(rows, tau)
        for g in segs:          # the colour as linear light, to compare materials under different light
            g.append(np.median(SL[g[0]:g[1], k0:k1].reshape(-1, 3), axis=0) if g[3] else None)
        per.append((segs, u, rows, k0, k1, S.shape[0]))
    # Group the uniform segments by chromaticity (single linkage); lightness may differ up to 2.5x, as a lamp lights
    # one side more.
    items = [(s, i, np.maximum(g[6], 1e-4)) for s, (segs, *_rest) in enumerate(per)
             for i, g in enumerate(segs) if g[3]]
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _same_material(items[i][2], items[j][2]):
                parent[root(i)] = root(j)
    members = {}
    for k, (s, i, _) in enumerate(items):
        members.setdefault(root(k), set()).add(s)
    group = {(s, i): root(k) for k, (s, i, _) in enumerate(items)}
    shared = {g for g, m in members.items() if len(m) == 4}
    # the lightest material that goes all round (the mat or paper, not the wood or gilt round it) is the evidence
    light_of = {}
    for k, (s, i, c) in enumerate(items):
        light_of.setdefault(root(k), []).append(float(c @ LUM))
    lightest = max(shared, key=lambda g: np.median(light_of[g])) if shared else None
    sides = []
    for s, (segs, u, rows, k0, k1, depth) in enumerate(per):
        band = []
        for i, g in enumerate(segs):
            if not g[3] or group.get((s, i)) not in shared:
                break
            band.append(g)
        info = {"depth": 0.0, "rows": 0, "straight": False, "colour": None, "cells": [], "cols": (k0, k1),
                "edge": None}
        if whole and band and band[-1][1] >= rows.shape[0] - 3:
            band = []              # plain all the way in from the picture's border: a wall round a small work
        if band:
            paper = [g[2][0] >= 62 and math.hypot(g[2][1], g[2][2]) <= 40 for g in band]   # light, near-neutral
            i = 0
            while i < len(band) and not paper[i]:
                i += 1                                          # the frame's mouldings
            if 0 < i < len(band):
                i += 1                                          # and a mat inside them
            frame_part = band[:i]                               # empty: a sheet, its margin is part of the work
            last = frame_part[-1] if frame_part else band[-1]
            end = last[1] if frame_part else 0
            if frame_part and last[4] and last[5] is not None:  # the band ends at a straight step
                ab = last[5]
                n = k1 - k0
                ends = [ab[0] + ab[1] * k for k in (0, n - 1)]
                info["straight"] = True
                info["edge"] = [to_xy[s](e + 0.5, k + k0 + 0.5) for e, k in zip(ends, (0, n - 1))]
                end = int(round(float(np.mean(ends))))
            info["rows"] = end
            info["depth"] = end / float(depth)
            rr = np.concatenate([np.arange(g[0], g[1]) for g in band])
            med = np.median(rows[rr], axis=1)
            info["colour"] = [float(v) for v in np.median(med, axis=0)]
            # evidence: the lightest material all round, if it is light and near-neutral (paper, a mat, a white
            # frame), in blocks along the side and across its depth, so the light's course into the work is seen
            # (a shadow over one edge); a block is kept only where it is smooth and of the material's own colour
            # (a thumb, a pencil mark or a fading wash is not), and not clipped (counted: the light is strongest
            # there). A sheet's plain paper is used to a fifth of the way in, never deeper: a pale wash further in
            # would pass for shade.
            lr = np.concatenate([np.arange(g[0], g[1]) for i, g in enumerate(band)
                                 if group.get((s, i)) == lightest] or [np.zeros(0, int)]).astype(int)
            lr = lr[lr < max(4, int(0.2 * depth))] if not frame_part else lr
            info["cells"], info["clipped_rows"] = _band_cells(turn[s](lab), turn[s](lin), lr, tau)
        sides.append(info)
    return {"sides": sides, "size": (pw, ph), "H": Hm, "to_xy": to_xy, "tau": tau, "u_int": u_int}


def _band_quad(bands):
    """Inner quad (preview px) where the four bands end: the straight edges found, else lines at the band depths."""
    pw, ph = bands["size"]
    lines = []
    for s, info in enumerate(bands["sides"]):
        if info["edge"] is not None:
            p, q = (np.array(v, float) for v in info["edge"])
        else:
            d = info["rows"] + 0.5
            p, q = [(np.array([0, d]), np.array([pw, d])), (np.array([pw - d, 0]), np.array([pw - d, ph])),
                    (np.array([0, ph - d]), np.array([pw, ph - d])), (np.array([d, 0]), np.array([d, ph]))][s]
        n = np.array([q[1] - p[1], p[0] - q[0]], float)
        n /= max(1e-9, float(np.linalg.norm(n)))
        lines.append((n, -float(n @ p)))
    return _order(np.array([_meet(lines[(i - 1) % 4], lines[i]) for i in range(4)]))


def _band_cells(lab, lin, rows, tau, nk=16):
    """Blocks of a side's strip (rows given, ~2.5% of the depth each, by nk pieces along the whole side) that are
    smooth, light, near-neutral and of the material's usual colour: [(row array, k0, k1, clipped)], and how many
    rows are clipped white. A clipped block (white paper in strong light) still says the light there is at least
    that bright: it is kept, marked, as a lower bound."""
    if len(rows) < 2:
        return [], 0
    n, depth = lab.shape[1], lab.shape[0]
    top = np.median(lin[rows], axis=1).max(1)
    clipped = int((top >= 0.98).sum())
    if (top < 0.98).sum() < 2:
        return [], clipped
    ab0 = np.median(lab[rows[top < 0.98], int(0.1 * n):int(0.9 * n), 1:].reshape(-1, 2), axis=0)
    bd = max(2, int(round(0.025 * depth)))
    bins = [rows[(rows // bd) == b] for b in np.unique(rows // bd)]
    ks = np.linspace(int(0.02 * n), int(math.ceil(0.98 * n)), nk + 1).astype(int)
    out = []
    for rr in bins:
        if len(rr) < 2:
            continue
        for a, b in zip(ks[:-1], ks[1:]):
            blk = lab[rr, a:b].reshape(-1, 3)
            med = np.median(blk, axis=0)
            hot = float(np.median(lin[rr, a:b].reshape(-1, 3), axis=0).max()) >= 0.98
            tex = float(np.percentile(np.linalg.norm(blk - med, axis=1), 80))
            if hot and med[0] >= 90 and tex <= tau:
                out.append((rr, a, b, True))
            elif tex <= tau and med[0] >= 62 and math.hypot(med[1], med[2]) <= 40 and \
                    math.hypot(med[1] - ab0[0], med[2] - ab0[1]) <= 8 and lin[rr, a:b].max() < 0.995:
                out.append((rr, a, b, False))
    return out, clipped


def _band_evidence(bands):
    """Groups of sample points (detection px) on light, neutral band material, blocks along and across each side:
    (side, points)."""
    cells = []
    for s, info in enumerate(bands["sides"]):
        for rr, a, b, hot in info["cells"]:
            dd, kk = np.meshgrid(rr, np.arange(a, b), indexing="ij")
            x, y = bands["to_xy"][s](dd, kk)
            cells.append((s, _apply(bands["H"], np.c_[np.ravel(x) + 0.5, np.ravel(y) + 0.5]), hot))
    return cells


# ---------------------------------------------------------------- evidence, fields, gains

def _cells_from(img_lin, pts_list, minpx=6):
    """Median linear colour and centre of each group of sample points (img px), and whether it is clipped."""
    out_xy, out_rgb, out_side, out_clip = [], [], [], []
    for side, p, hot in pts_list:
        if len(p) < minpx:
            continue
        col = _sample(img_lin, p[:, 0], p[:, 1])
        out_xy.append(p.mean(0))
        out_rgb.append(np.median(col, axis=0))
        out_side.append(side)
        out_clip.append(hot)
    if not out_xy:
        return None
    return {"xy": np.array(out_xy), "rgb": np.array(out_rgb), "side": np.array(out_side),
            "clip": np.array(out_clip, bool)}


def _surround_cells(small_lin, mag, quad, cell=10):
    """Cells of plain wall or table round the work: low texture, one dominant near-uniform material."""
    h, w = mag.shape
    q = np.asarray(quad, float)
    size = math.sqrt(_area(q))
    inside = io.poly_mask((h, w), q)
    dist = ndi.distance_transform_edt(~inside)
    ring = (dist > 0.03 * size) & (dist < 0.4 * size)
    calm = ndi.gaussian_filter(mag, 1.5) < 0.02
    ny, nx = h // cell, w // cell
    if ny < 2 or nx < 2:
        return None
    blk = lambda a: a[:ny * cell, :nx * cell].reshape(ny, cell, nx, cell).swapaxes(1, 2).reshape(ny, nx, -1)
    rr, cc = blk(ring), blk(calm)
    ok = (rr.mean(-1) > 0.95) & (cc.mean(-1) > 0.85)
    if ok.sum() < 12:
        return None
    lin = small_lin[:ny * cell, :nx * cell].reshape(ny, cell, nx, cell, 3).swapaxes(1, 2).reshape(ny, nx, -1, 3)
    rgb = np.median(lin[ok], axis=1)
    yy, xx = np.mgrid[0:ny, 0:nx]
    xy = np.c_[(xx[ok] + 0.5) * cell, (yy[ok] + 0.5) * cell]
    good = rgb.min(1) > 0.01
    rgb, xy = rgb[good], xy[good]
    if len(rgb) < 12:
        return None
    lc = np.log(np.maximum(rgb, 1e-4))
    chrom = np.c_[lc[:, 0] - lc[:, 1], lc[:, 2] - lc[:, 1]]
    centre = np.median(chrom, axis=0)
    same = np.linalg.norm(chrom - centre, axis=1) < 0.07
    return {"xy": xy[same], "rgb": rgb[same], "side": _facing(q, xy)[same], "share": float(same.mean()),
            "n_all": int(len(rgb)), "clip": np.zeros(int(same.sum()), bool)}


def _facing(q, xy):
    """Which side of quad q (0 top, 1 right, 2 bottom, 3 left) each point lies nearest to."""
    d = []
    for i in range(4):
        a, b = q[i], q[(i + 1) % 4]
        e = (b - a) / max(1e-9, float(np.linalg.norm(b - a)))
        t = np.clip((xy - a) @ e, 0, np.linalg.norm(b - a))
        d.append(np.linalg.norm(xy - (a + t[:, None] * e), axis=1))
    return np.argmin(np.array(d), axis=0)


def _poly(uv, deg):
    u, v = uv[:, 0], uv[:, 1]
    return np.stack([u ** i * v ** j for i in range(deg + 1) for j in range(deg + 1 - i)], -1)


def _robust_lstsq(A, y, iters=10):
    """Least squares with Tukey weights: coefficients and the robust residual scale."""
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    s = 0.01
    for _ in range(iters):
        r = y - A @ beta
        s = max(0.004, 1.4826 * float(np.median(np.abs(r - np.median(r)))))
        u = r / (4.685 * s)
        w = np.where(np.abs(u) < 1, (1 - u * u) ** 2, 0.0)
        if (w > 0).sum() < A.shape[1] + 3:
            break
        sw = np.sqrt(w)
        beta = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)[0]
    return beta, s


def _tps_kernel(a, b):
    d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
    return 0.5 * d2 * np.log(np.maximum(d2, 1e-20))           # r^2 log r


def _tps(uv, y, lam=0.05, most=300):
    """Thin-plate smoothing spline through (uv, y), at most `most` centres (solved directly: small systems)."""
    k = max(1, int(math.ceil(len(uv) / float(most))))
    c, yc = uv[::k], y[::k]
    n = len(c)
    A = np.zeros((n + 3, n + 3))
    A[:n, :n] = _tps_kernel(c, c) + lam * np.eye(n)
    A[:n, n:] = np.c_[np.ones(n), c]
    A[n:, :n] = A[:n, n:].T
    sol = np.linalg.solve(A, np.r_[yc, np.zeros(3)])
    w, a = sol[:n], sol[n:]

    def f(q):
        q = np.asarray(q, float)
        out = np.empty(len(q))
        for i in range(0, len(q), 4096):          # bounded memory for large queries
            qq = q[i:i + 4096]
            out[i:i + 4096] = _tps_kernel(qq, c) @ w + np.c_[np.ones(len(qq)), qq] @ a
        return out
    return f


def _nn_noise(uv, r):
    """Noise of residuals r measured between nearest neighbours: what no smooth model can explain."""
    from scipy.spatial import cKDTree
    if len(uv) < 8:
        return float(1.4826 * np.median(np.abs(r - np.median(r))))
    _, j = cKDTree(uv).query(uv, k=2)
    return float(1.4826 * np.median(np.abs(r - r[j[:, 1]])) / math.sqrt(2))


SPLINE_LAM = 0.3       # smoothing of the spline on what the cubic leaves (uv spans about -1..1)
SPLINE_GATE = 1.6      # ... used only when the cubic leaves this many times the neighbour-to-neighbour noise


def _light_field(uv, logy, side, top=3, low=None):
    """A smooth model of log light fitted to evidence at uv (work rectangle coordinates, about -1..1 over the work):
    a robust cubic where the evidence surrounds the work, a quadratic with one side missing, a plane with two.
    Light on a wall or a sheet is a smooth function of the position on that plane (a lamp's falloff, a broad
    hotspot, vignetting), which a cubic fitted all round follows to about 1-3% over the work; when it leaves
    structure clearly above the noise between neighbouring cells (the soft edge of a shadow), a smoothing spline
    on its residuals follows that too. low: (uv, log light) of clipped evidence, where the light is at least that:
    added where the model falls short of it (censored data). Returns (predict(uv) -> log light, name, scatter,
    inliers over the unclipped evidence)."""
    beta, s = _robust_lstsq(_poly(uv, 2), logy)
    inl = np.abs(logy - _poly(uv, 2) @ beta) < max(3 * s, 0.015)        # paint marks, a thumb, a nail hole
    seen = len(set(side[inl].tolist()))
    deg = min(top, 3 if seen == 4 and inl.sum() >= 40 else 2 if seen >= 3 and inl.sum() >= 12 else 1)
    u, y = uv[inl], logy[inl]
    beta, s = _robust_lstsq(_poly(u, deg), y)
    if low is not None and len(low[0]):
        for _ in range(3):              # bring the fit up to where clipped paper shows the light at least that bright
            short = _poly(low[0], deg) @ beta < low[1]
            if not short.any():
                break
            beta, s = _robust_lstsq(_poly(np.r_[uv[inl], low[0][short]], deg), np.r_[logy[inl], low[1][short]])
    r = y - _poly(u, deg) @ beta
    keep = np.abs(r) < max(3 * s, 0.015)
    noise = _nn_noise(u[keep], r[keep])
    name = ("plane", "quadratic", "cubic")[deg - 1]
    spl = None
    if deg == 3 and keep.sum() >= 60 and s > max(SPLINE_GATE * noise, 0.006):
        spl = _tps(u[keep], r[keep], lam=SPLINE_LAM)
        r2 = r[keep] - spl(u[keep])
        s = max(0.004, 1.4826 * float(np.median(np.abs(r2 - np.median(r2)))))
        name = "cubic+spline"
    inl[np.where(inl)[0][~keep]] = False

    def pred(q):
        q = np.asarray(q, float).reshape(-1, 2)
        v = _poly(q, deg) @ beta
        return v + spl(q) if spl is not None else v
    return pred, name, max(s, 0.004), inl


def _spread(v):
    v = np.asarray(v, float)
    return float((np.percentile(v, 95) - np.percentile(v, 5)) / max(1e-9, np.median(v)))


# ---------------------------------------------------------------- resampling

def _lerp_weights(pos, nodes, extent):
    """Linear interpolation weights (len(pos) x nodes) from nodes spread evenly over 0..extent to positions."""
    t = np.clip(np.asarray(pos, float) / float(extent) * (nodes - 1), 0, nodes - 1)
    i = np.minimum(np.floor(t).astype(int), nodes - 2)
    f = (t - i).astype(np.float32)
    M = np.zeros((len(t), nodes), np.float32)
    M[np.arange(len(t)), i] = 1 - f
    M[np.arange(len(t)), i + 1] = f
    return M


def _field_grid(field, size, n=41):
    """The light correction (the field's mean over the work / the field) on a grid of nodes spread over the output
    rectangle edge to edge, so the model is evaluated a few thousand times, not once per pixel of a 48 MP photo;
    the field is smooth, and bilinear interpolation between nodes is exact to well under 0.1%."""
    pred, mean = field
    Wo, Ho = size
    nx, ny = (n, max(9, int(round(n * Ho / Wo)))) if Wo >= Ho else (max(9, int(round(n * Wo / Ho))), n)
    half = 0.5 * max(Wo, Ho)
    gu, gv = np.meshgrid(np.linspace(-Wo / 2.0, Wo / 2.0, nx) / half, np.linspace(-Ho / 2.0, Ho / 2.0, ny) / half)
    return (mean / np.exp(pred(np.c_[gu.ravel(), gv.ravel()]))).reshape(ny, nx).astype(np.float32)


def _warp(lin, Hinv, size, k1, c, R, post=None, rows=128, offset=(0.0, 0.0), photo_size=None):
    """Resample linear RGB onto the output rectangle (size w, h). Hinv maps output px to undistorted input px.

    Interpolating cubic spline, clamped to the four nearest input pixels so no ringing halo appears; the output is
    sized so that nowhere is the input shrunk, so no prefilter is needed. post(chunk, y0) gets each block of rows
    (output rows y0...) to apply the light and colour correction. Blocks run in threads (scipy releases the GIL).
    lin may be a crop of the photo starting at offset; valid marks output px that fall inside the whole photo."""
    h0, w0 = lin.shape[:2]
    Wo, Ho = size
    ox, oy = offset
    Wp, Hp = photo_size or (w0, h0)
    workers = max(1, min(8, os.cpu_count() or 1))
    with ThreadPoolExecutor(workers) as pool:
        coef = list(pool.map(lambda ch: ndi.spline_filter(lin[..., ch], order=3, output=np.float32, mode="mirror"),
                             range(3)))
    out = np.empty((Ho, Wo, 3), np.float32)
    valid = np.ones((Ho, Wo), bool)
    us = np.arange(Wo, dtype=np.float64) + 0.5

    def block(y0):
        y1 = min(Ho, y0 + rows)
        vv, uu = np.meshgrid(np.arange(y0, y1, dtype=np.float64) + 0.5, us, indexing="ij")
        X = Hinv[0, 0] * uu + Hinv[0, 1] * vv + Hinv[0, 2]
        Y = Hinv[1, 0] * uu + Hinv[1, 1] * vv + Hinv[1, 2]
        Z = Hinv[2, 0] * uu + Hinv[2, 1] * vv + Hinv[2, 2]
        xs, ys = X / Z, Y / Z
        if k1:
            xs, ys = _distort(xs, ys, k1, c, R)
        valid[y0:y1] = (xs > -0.5) & (xs < Wp + 0.5) & (ys > -0.5) & (ys < Hp + 0.5)
        ix, iy = xs - ox - 0.5, ys - oy - 0.5
        fx = np.clip(np.floor(ix).astype(np.int32), 0, w0 - 2)
        fy = np.clip(np.floor(iy).astype(np.int32), 0, h0 - 2)
        co = [iy.astype(np.float32), ix.astype(np.float32)]
        for ch in range(3):
            v = ndi.map_coordinates(coef[ch], co, order=3, mode="mirror", prefilter=False, output=np.float32)
            a, b = lin[fy, fx, ch], lin[fy, fx + 1, ch]
            cc, d = lin[fy + 1, fx, ch], lin[fy + 1, fx + 1, ch]
            out[y0:y1, :, ch] = np.clip(v, np.minimum(np.minimum(a, b), np.minimum(cc, d)),
                                        np.maximum(np.maximum(a, b), np.maximum(cc, d)))
        if post is not None:
            post(out[y0:y1], y0)
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(block, range(0, Ho, rows)))
    return out, valid


# ---------------------------------------------------------------- checks

EDGE_PM, EDGE_PW = 50, 5
BLUR_FOCUS = 5.0       # working px (10-90% rise) of the work's boundary above which a photo is out of focus
BLUR_SHAKE = 4.0       # ... the width of the smeared sides that, at twice the others, means camera shake
BLUR_CRISP = 2.6       # ... and the crispest edges anywhere round the work must be at least this wide to agree
                       # (sharp synthetic photos: boundary 1.5-3.5, crispest 1.1-2.5; 3 px of blur: 7.7 and 2.9)


def _edge_widths(rgb, quad, scale):
    """How sharp the photo is round and inside the work, at working size (scale: working px per photo px): the
    10-90% rise of the sharpest edges, per edge direction (normals in 4 bins, 45 degrees apart), in working px;
    None for a direction with too few strong edges. A painting may be soft all over, but the work's own boundary,
    the wall, the frame and any crisp mark give sharp edges in a sharp photo; camera shake widens the edges across
    its direction only, defocus all of them."""
    q = np.asarray(quad, float)
    H, W = rgb.shape[:2]
    pad = 0.06 * max(np.ptp(q[:, 0]), np.ptp(q[:, 1]))
    x0, y0 = max(0, int(q[:, 0].min() - pad)), max(0, int(q[:, 1].min() - pad))
    x1, y1 = min(W, int(q[:, 0].max() + pad) + 1), min(H, int(q[:, 1].max() + pad) + 1)
    if x1 - x0 < 16 or y1 - y0 < 16:
        return [None] * 4
    s = min(1.0, scale)
    im = Image.fromarray(np.ascontiguousarray(rgb[y0:y1, x0:x1])).convert("L")
    if s < 1.0:
        im = im.resize((max(8, int(round((x1 - x0) * s))), max(8, int(round((y1 - y0) * s)))), Image.BOX)
    lum = np.asarray(im, np.float32) / 255.0
    g = ndi.gaussian_filter(lum, 0.7)
    gx, gy = ndi.sobel(g, 1) / 8.0, ndi.sobel(g, 0) / 8.0
    mag = np.hypot(gx, gy)
    th = np.arctan2(gy, gx) % np.pi
    h, w = mag.shape
    R = 14.0
    inner = np.zeros(mag.shape, bool)
    inner[int(R) + 1:h - int(R) - 1, int(R) + 1:w - int(R) - 1] = True
    region = io.poly_mask(mag.shape, (q - [x0, y0]) * [w / float(x1 - x0), h / float(y1 - y0)])
    region = ndi.distance_transform_edt(~region) <= max(2, 0.04 * max(h, w))
    cand = _thin(mag, th) & inner & region
    if cand.sum() < 50:
        return [None] * 4
    t = max(0.01, float(np.percentile(mag[cand], EDGE_PM)))
    ys, xs = np.nonzero(cand & (mag >= t))
    if len(ys) > 6000:
        k = np.random.default_rng(0).choice(len(ys), 6000, replace=False)
        ys, xs = ys[k], xs[k]
    nx_, ny_ = np.cos(th[ys, xs]), np.sin(th[ys, xs])
    ss = np.arange(-R, R + 0.01, 0.5)
    px, py = xs[:, None] + ss[None, :] * nx_[:, None], ys[:, None] + ss[None, :] * ny_[:, None]
    prof = ndi.map_coordinates(lum, [py.ravel(), px.ravel()], order=1, mode="nearest").reshape(px.shape)
    step_ = np.abs(prof[:, -8:].mean(1) - prof[:, :8].mean(1))
    widths = np.where(step_ > 0.04, _rise(prof, 0.5), np.nan)        # a real step of at least 4% of full scale
    b = (np.round(th[ys, xs] / (np.pi / 4)) % 4).astype(int)
    out = []
    for k in range(4):
        v = widths[(b == k) & np.isfinite(widths)]
        out.append(float(np.percentile(v, EDGE_PW)) if len(v) >= 40 else None)
    return out


def _glare(small, quad, sc):
    """Washed-out highlights inside the work, on the detection copy: clipped, colourless spots (every channel near
    white) clearly brighter than the paint around them, beyond the spot's own halo. A clipped region touching the
    work's border is a white paper margin or mat in strong light, not a reflection on the paint, and is left out.
    Returns (share of the work, boxes in photo px)."""
    q = np.asarray(quad, float) * sc
    h, w = small.shape[:2]
    x0, y0 = max(0, int(q[:, 0].min())), max(0, int(q[:, 1].min()))
    x1, y1 = min(w, int(math.ceil(q[:, 0].max()))), min(h, int(math.ceil(q[:, 1].max())))
    sub = small[y0:y1, x0:x1]
    if sub.shape[0] < 8 or sub.shape[1] < 8:
        return 0.0, []
    inside = io.poly_mask(sub.shape, q - [x0, y0])
    area = float(inside.sum())
    hot = (sub.min(-1) >= 0.94) & (sub.max(-1) >= 0.98) & inside
    if not hot.any() or area <= 0:
        return 0.0, []
    rim = inside & ~ndi.binary_erosion(inside, iterations=max(2, int(0.03 * math.sqrt(area))))
    lab, n = ndi.label(hot)
    sizes = ndi.sum(hot, lab, np.arange(1, n + 1))
    lum = io.luma(sub)
    boxes, total = [], 0.0
    for i in np.where(sizes >= max(12, 5e-4 * area))[0]:
        sl = ndi.find_objects((lab == i + 1).astype(np.int8))[0]
        blob = lab[sl] == i + 1
        bh, bw = blob.shape
        if (rim[sl] & blob).any() or sizes[i] < 0.35 * bh * bw or max(bh, bw) > 4 * min(bh, bw):
            continue            # touches the border, or a ring or strip (a white mat, a paper margin), not a spot
        r = math.sqrt(sizes[i] / math.pi)
        g = int(math.ceil(2.5 * r)) + 2
        ys0, ys1 = max(0, sl[0].start - g), min(sub.shape[0], sl[0].stop + g)
        xs0, xs1 = max(0, sl[1].start - g), min(sub.shape[1], sl[1].stop + g)
        m = np.zeros((ys1 - ys0, xs1 - xs0), bool)
        m[sl[0].start - ys0:sl[0].stop - ys0, sl[1].start - xs0:sl[1].stop - xs0] = blob
        dist = ndi.distance_transform_edt(~m)
        ring = (dist > max(2.0, 1.2 * r)) & (dist <= max(4.0, 2.5 * r)) & inside[ys0:ys1, xs0:xs1]
        if ring.sum() >= 8 and float(np.median(lum[ys0:ys1, xs0:xs1][ring])) < 0.8:
            total += sizes[i]
            boxes.append([(x0 + sl[1].start) / sc, (y0 + sl[0].start) / sc, (x0 + sl[1].stop) / sc,
                          (y0 + sl[0].stop) / sc])
    return total / area, boxes


# ---------------------------------------------------------------- review sheet

def _stops_colour(st):
    """Diverging colour for a change in stops: blue darkened, red brightened, white none."""
    t = np.clip(st / 1.0, -1, 1)[..., None]
    red, blue, white = np.array([0.85, 0.2, 0.15]), np.array([0.15, 0.35, 0.85]), np.array([1.0, 1.0, 1.0])
    return np.where(t > 0, white * (1 - t) + red * t, white * (1 + t) - blue * t)


def _review(path, photo, quads, evid, boxes, result, change, rep):
    """photo with the boundary drawn | corrected picture | where and how much light and colour changed."""
    Hh = 560

    def fit(a):
        s = Hh / float(a.shape[0])
        im = Image.fromarray(a if a.dtype == np.uint8 else io.to_u8(a))
        return im.resize((max(1, int(round(a.shape[1] * s))), Hh), Image.LANCZOS), s
    p1, s1 = fit(photo)
    d = ImageDraw.Draw(p1)
    if evid is not None and len(evid):           # a sample of the evidence cells, so the photo stays visible
        for x, y in evid[::max(1, len(evid) // 300)] * s1:
            d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=(60, 220, 90))
    for bx in boxes:
        d.rectangle([v * s1 for v in bx], outline=(255, 40, 40), width=2)
    for q, colr, wd in quads:
        pts = [tuple(v) for v in (np.asarray(q) * s1)]
        d.line(pts + pts[:1], fill=colr, width=wd)
    if quads:
        for name, (x, y) in zip(("TL", "TR", "BR", "BL"), np.asarray(quads[-1][0]) * s1):
            io.label(d, (x + 4, y + 4), name, size=12, bg=(200, 0, 200))
    panels = [(p1, "photo: boundary %s" % (rep["quad"]["source"] or "-"))]
    if result is not None:
        p2, _ = fit(result)
        panels.append((p2, "result %dx%d" % tuple(rep["size_out"])))
        g = io.luma(np.asarray(p2, np.float32) / 255.0)[..., None] * 0.5 + 0.25
        if change is not None:
            ch = np.asarray(Image.fromarray(change.astype(np.float32)).resize(p2.size, Image.BILINEAR))
            col = _stops_colour(ch)
            k = np.clip(np.abs(ch) / 0.15, 0, 1)[..., None] * 0.8
            m = g * (1 - k) + col * k
        else:
            m = np.repeat(g, 3, axis=2)
        p3 = Image.fromarray(io.to_u8(m))
        d3 = ImageDraw.Draw(p3)
        li, co = rep["light"], rep["color"]
        lines = ["light: " + ("%s, variation %.0f%% -> %.0f%%" % (li["reference"], 100 * li["variation_before"],
                                                                   100 * li["variation_after"])
                              if li["applied"] else "unchanged (%s)" % (li.get("why") or "no neutral evidence")),
                 "colour: " + ("R x%.2f  G x%.2f  B x%.2f (%s)" % (tuple(co["gains"]) + (co["reference"],))
                               if co["applied"] else "unchanged (%s)" % (co.get("why") or "no neutral evidence"))]
        if rep.get("exposure"):
            lines.append("exposure x%.2f: %s reference to paper white" % (rep["exposure"], li["reference"]))
        per = max(20, int(p3.size[0] / 7.2))          # characters per line at this label size
        wrapped = []
        for t in lines:
            while len(t) > per:
                cut = t.rfind(" ", 0, per)
                cut = cut if cut > per // 2 else per
                wrapped.append(t[:cut])
                t = "   " + t[cut:].lstrip()
            wrapped.append(t)
        for i, t in enumerate(wrapped):
            io.label(d3, (6, Hh - 20 * (len(wrapped) - i) - 4), t, size=13)
        # legend: -1 .. +1 stop
        bar = _stops_colour(np.linspace(-1, 1, 160)[None, :].repeat(10, 0))
        p3.paste(Image.fromarray(io.to_u8(bar)), (8, 30))
        io.label(d3, (8, 44), "-1 stop", size=11)
        io.label(d3, (128, 44), "+1", size=11)
        panels.append((p3, "light change (blue darker, red brighter)"))
    gap = 8
    Wt = sum(p.size[0] for p, _ in panels) + gap * (len(panels) - 1)
    sheet = Image.new("RGB", (Wt, Hh), (255, 255, 255))
    x = 0
    for p, title in panels:
        sheet.paste(p, (x, 0))
        io.label(ImageDraw.Draw(sheet), (x + 6, 6), title, size=14)
        x += p.size[0] + gap
    if sheet.size[0] > 2400:
        sheet = sheet.resize((2400, int(round(Hh * 2400.0 / sheet.size[0]))), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    sheet.save(path, "JPEG", quality=88)


# ---------------------------------------------------------------- the command

def _r(v, n=3):
    return None if v is None else round(float(v), n)


def _parse_corners(text):
    try:
        pts = [[float(t) for t in p.split(",")] for p in text.replace(" ", "").split(";") if p]
    except ValueError:
        raise ValueError("--corners wants four points as \"x,y;x,y;x,y;x,y\"")
    if len(pts) != 4 or any(len(p) != 2 for p in pts):
        raise ValueError("--corners wants four points as \"x,y;x,y;x,y;x,y\"")
    q = _order(np.array(pts))
    if not _convex(q):
        raise ValueError("--corners do not make a convex quadrilateral")
    return q


def _plausible(q, min_area, W, H):
    """Whether q could be a rectangle as a phone sees it: big enough, corners between 35 and 145 degrees, opposite
    sides within 3x of each other."""
    if _area(q) < min_area * W * H or not _convex(q):
        return False
    for i in range(4):
        u, v = q[(i - 1) % 4] - q[i], q[(i + 1) % 4] - q[i]
        c = float(u @ v) / max(1e-9, float(np.linalg.norm(u) * np.linalg.norm(v)))
        if abs(c) > math.cos(math.radians(35)):
            return False
    s = _sides(q)
    return max(s[0], s[2]) <= 3 * min(s[0], s[2]) and max(s[1], s[3]) <= 3 * min(s[1], s[3])


def _square_on(q):
    """Every side within 1.5 degrees of horizontal or vertical: no perspective and no turn to undo. In a picture
    without camera data that is a shape painted inside a flat picture, or a flat scan with its paper margin."""
    e = np.roll(q, -1, 0) - q
    ang = np.degrees(np.arctan2(e[:, 1], e[:, 0])) % 90
    return bool(np.minimum(ang, 90 - ang).max() < 1.5)


def _continues(small, q):
    """Sides of q across which the picture seems to go on: alike in colour on both sides, with marks on both, as a
    painted line inside a painting is and the edge of a work against a plain wall, mat or table is not."""
    Hm = _hom([[0, 0], [1, 0], [1, 1], [0, 1]], q)
    h, w = small.shape[:2]
    lab = io.rgb_to_lab(small) * 100.0
    t = np.linspace(0.12, 0.88, 120)
    out = []
    for side, name in enumerate(("top", "right", "bottom", "left")):
        bands = []
        for ds in (np.linspace(-0.05, -0.012, 5), np.linspace(0.012, 0.05, 5)):      # outside, inside
            uv = [(tt, d) if side == 0 else (1 - d, tt) if side == 1 else (tt, 1 - d) if side == 2 else (d, tt)
                  for d in ds for tt in t]
            p = _apply(Hm, np.array(uv, float))
            if (p[:, 0] < 0).any() or (p[:, 0] >= w).any() or (p[:, 1] < 0).any() or (p[:, 1] >= h).any():
                break
            bands.append(_sample(lab, p[:, 0], p[:, 1]).reshape(len(ds), len(t), 3))
        if len(bands) < 2:
            continue
        tex = [float(np.median(_texture(b_))) for b_ in bands]
        diff = float(np.linalg.norm(np.median(bands[0].reshape(-1, 3), 0) - np.median(bands[1].reshape(-1, 3), 0)))
        if diff < 10.0 and max(tex) > 6.0 and min(tex) > 2.5:       # a plain wall or mat scores below 1.5
            out.append(name)
    return out


def _boundary(photo, rgb, small, sc, maps, corners, camera_photo=True):
    """Candidate boundaries, best first: (list, note about Vision)."""
    H, W = rgb.shape[:2]
    diag = math.hypot(W, H)
    if corners is not None:
        q = _parse_corners(corners) if isinstance(corners, str) else _order(np.array(corners, float))
        return [{"quad": q, "ref": _refine(rgb, q, max(4.0, 0.004 * diag)), "source": "given", "conf": 1.0,
                 "area": _area(q) / (W * H)}], None
    mag, ang, edges, thr = maps
    win = 2.0 / sc + 3.0
    vq, note = _vision_quads(photo, (W, H))
    jobs = [(e["quad"] / sc, win) for e in _edge_quads(small, mag, ang, edges, thr)]
    jobs += [(v["quad"], max(6.0, 0.006 * diag)) for v in vq]
    with ThreadPoolExecutor(max(1, min(8, os.cpu_count() or 1))) as pool:
        refs = list(pool.map(lambda j: _refine(rgb, j[0], j[1]), jobs))
    ne = len(jobs) - len(vq)
    cands = [{"quad": ref["quad"], "ref": ref, "source": "edges"} for ref in refs[:ne]]
    for v, ref in zip(vq, refs[ne:]):
        same = [c for c in cands if np.abs(c["quad"] - ref["quad"]).max() < 0.015 * diag]
        for c in same:
            c["vision"] = max(c.get("vision", 0.0), v["confidence"])
        if not same:
            cands.append({"quad": ref["quad"], "ref": ref, "source": "vision", "vision": v["confidence"]})
    min_area = 0.05 if camera_photo else 0.10
    for c in cands:
        sup = c["ref"]["support"]
        res = [s["resid"] for s in c["ref"]["sides"] if s["resid"] is not None]
        straight = 1.0 if not res else float(np.clip(1.2 - max(res) / (0.0015 * diag + 0.5), 0, 1))
        conf = float(np.clip((sup - 0.3) / 0.5, 0, 1)) * straight
        if c.get("vision"):
            conf = max(conf, 0.5 * conf + 0.5 * c["vision"] * min(1.0, sup / 0.5))
            sups = sorted(s["support"] for s in c["ref"]["sides"])
            if camera_photo and c["vision"] >= 0.8 and sups[1] >= 0.8 and sups[0] < 0.5 and straight > 0.8:
                # three sides hold; Vision placed the fourth where the photo shows no edge (glare, a background
                # as light as the paper): usable, but say so
                conf = max(conf, 0.55)
                c["blind_side"] = ("top", "right", "bottom", "left")[int(np.argmin([s["support"]
                                                                                    for s in c["ref"]["sides"]]))]
        # a corner no edge reaches is where the lines of two objects cross (a painting's top edge running on to
        # its neighbour's side), not a corner of one
        conf *= 0.7 ** sum(1 for v in c["ref"].get("corners", []) if v is False)
        if not _plausible(c["quad"], min_area, W, H):
            conf = 0.0
        c["conf"] = conf
        c["area"] = _area(c["quad"]) / (W * H)
        c["score"] = conf * max(c["area"], 1e-6) ** 0.3
        ends = [e for s in c["ref"]["sides"] for e in (s.get("ends") or []) if e is not None]
        c["reach"] = min(ends) if len(ends) >= 6 else 0.0
    cands.sort(key=lambda c: -c["score"])
    # Lines of nested rectangles (frame, mat, painting) combine into many well-supported quads, some mixing an
    # inner side with an outer one. The outermost credible one is the object; the bands inside it are read later.
    # Lines of neighbouring objects combine too (a painting's top and bottom edges running on to its neighbour's
    # side): such a quad has a side whose edge stops well short of a corner, and gives way to quads whose edges
    # reach every corner.
    good = [c for c in cands if c["conf"] >= MIN_CONF]
    if good:
        top = max(c["conf"] for c in good)
        pool = [c for c in good if c["conf"] >= 0.75 * top]
        if any(c["reach"] >= 0.6 for c in pool):
            pool = [c for c in pool if c["reach"] >= 0.3]
        best = max(pool, key=lambda c: c["area"])
        cands = [best] + [c for c in cands if c is not best]
    return cands, note


def _evidence(bands, small_lin, mag, outer_small, sc, whole):
    """Neutral evidence as cells (input px, median linear colour, side faced): paper margin or mat inside the work
    first, else plain wall or table round it."""
    cells = _cells_from(small_lin, _band_evidence(bands))
    if cells is not None and len(set(cells["side"].tolist())) >= 3:
        cells["xy"] = cells["xy"] / sc
        cells["clipped_sides"] = {s for s, info in enumerate(bands["sides"]) if info.get("clipped_rows", 0) >= 2}
        return cells, "margin"
    if whole:
        return None, "none"
    sur = _surround_cells(small_lin, mag, outer_small)
    if sur is not None and sur["share"] >= 0.6:
        sur["xy"] = sur["xy"] / sc
        return sur, "surround"
    return None, "none"


def _light_colour(evid, kind, grid_uv, light, color, strict, camera_photo, warn):
    """Decide the light field, colour gains and exposure from the evidence (positions uv in work rectangle
    coordinates; grid_uv covers the work). Returns (field, gains, exposure, light report, colour report); field is
    (predict(uv) -> log light, its mean over the work) or None."""
    LUM64 = LUM.astype(np.float64)
    Y = evid["rgb"] @ LUM64
    hot = evid["clip"] & (Y > 0.003)
    low = (evid["uv"][hot], np.log(Y[hot]))               # clipped paper: the light is at least this bright there
    ok = (Y > 0.003) & ~evid["clip"]
    xy, Y, rgb, side = evid["uv"][ok], Y[ok], evid["rgb"][ok], evid["side"][ok]
    lrep = {"applied": False, "reference": kind, "variation_before": None, "variation_after": None}
    crep = {"applied": False, "reference": kind, "gains": [1.0, 1.0, 1.0], "cast_before": None, "cast_after": None}
    if len(Y) < 8:
        lrep["why"] = crep["why"] = "too little evidence"
        return None, None, None, lrep, crep
    sides_seen = set(side.tolist())
    pred, model, s_res, inl = _light_field(xy, np.log(Y), side, low=low)
    fw = np.exp(pred(grid_uv))
    missing = set(range(4)) - sides_seen
    if missing and model != "plane":
        # one side has no evidence: a quadratic may invent a dome or a pit there; keep it only while it stays
        # within the light actually seen round the work (brighter is allowed where that side's mat is clipped
        # white, so the light there is known to be strongest), else fall back to a plane
        fe0 = np.exp(pred(xy))
        bright_ok = missing <= set(evid.get("clipped_sides", ()))
        if (fw.max() > 1.1 * fe0.max() and not bright_ok) or fw.min() < fe0.min() / 1.1:
            pred, model, s_res, inl = _light_field(xy, np.log(Y), side, top=1, low=low)
            fw = np.exp(pred(grid_uv))
    fe = np.exp(pred(xy))
    rng = float((fw.max() - fw.min()) / fw.mean())
    cov = len(set(side[inl].tolist()))
    vb = _spread(Y[inl])
    lrep.update(variation_before=_r(vb), variation_after=_r(vb), field_range=_r(rng), noise=_r(s_res, 4),
                sides=cov, model=model)
    field = None
    need = (2 if strict else 1) * FIELD_MIN
    if s_res > 0.05 or (strict and cov < 4):
        # not one even material under smooth light (a wall with things on it, a lamp's hot spot, a shadow edge, a
        # small work on a big wall): nothing to measure light or colour against
        lrep["why"] = crep["why"] = ("the evidence is not one even surface (scatter %.0f%%)" % (100 * s_res)
                                     if s_res > 0.05 else "no paper margin on all four sides")
        if s_res > 0.05 and not strict:
            warn("the %s round the work is not evenly lit or not one material (scatter %.0f%%: a shadow edge, a "
                 "lamp's spot, things on the wall?), so light and colour were left as photographed. Reshoot in "
                 "even, indirect light" % ("margin or mat" if kind == "margin" else "wall or table", 100 * s_res))
        return None, None, None, lrep, crep
    if light == "off":
        lrep["why"] = "--light off"
    elif cov < 3:
        lrep["why"] = "evidence on only %d side(s) of the work" % cov
    elif rng < need or rng < (6 if strict else 5) * s_res:
        lrep["why"] = "already even (%.0f%% across the work, noise %.1f%%)" % (100 * rng, 100 * s_res)
    elif fw.max() / fw.min() > 4.0:
        lrep["why"] = "too uneven for a smooth correction (x%.1f)" % (fw.max() / fw.min())
        warn("the light across the work changes more than fourfold (a hard shadow or mixed light?); it was left "
             "as photographed. Reshoot in even, indirect light, with no lamp close by and no shadow across it")
    elif _spread(Y[inl] / fe[inl]) > 0.15:
        # a smooth field leaves too much: a hard shadow edge or a narrow hot spot it cannot follow; dividing by
        # it would trade the shadow for gradients the painting never had
        lrep["why"] = "a smooth correction cannot follow this light (%.0f%% left on the evidence)" % (
            100 * _spread(Y[inl] / fe[inl]))
        warn("the light has a hard edge or a hot spot (a shadow across the work, a lamp close by) that a smooth "
             "correction cannot follow, so it was left as photographed. Reshoot in even, indirect light with "
             "nothing casting a shadow")
    else:
        field = (pred, float(fw.mean()))
        lrep.update(applied=True, variation_after=_r(_spread(Y[inl] / fe[inl])))
        lift = float(fw.mean() / fw.min())
        if lift > 2.0:
            warn("the darkest part of the work was lifted %.1f stops to even the light, so its noise shows more. "
                 "Even, indirect light gives a cleaner photo" % math.log2(lift))
    # colour: the evidence with its light divided out
    norm = rgb[inl] / fe[inl][:, None]
    ref = np.median(norm, axis=0)
    lc = np.log(np.maximum(ref, 1e-6))
    cast = np.array([lc[0] - lc[1], lc[2] - lc[1]])
    along = float(cast @ WARM)
    perp = float(cast @ np.array([-WARM[1], WARM[0]]))
    unit = lambda c: c / max(1e-9, float(c @ LUM64)) * 0.5
    cb = _chroma(unit(ref))
    crep.update(cast_before=_r(cb, 1), cast_after=_r(cb, 1), warm_cool=_r(along), tint=_r(perp))
    gains = None
    # Lamps can be very warm (2700 K under a 3500 K balance), shade only mildly cool, so a strongly cool reference
    # is more likely blue paint than blue light; walls are a little less trusted than paper for chroma.
    warm_cap, cool_cap, chroma_cap = (1.0, 0.5, 50) if kind == "margin" else (1.0, 0.5, 45)
    if color == "off":
        crep["why"] = "--color off"
    elif strict and not (field is not None or camera_photo):
        crep["why"] = "evenly lit and no camera data: may be a scan, whose paper keeps its own tint"
    elif abs(along) < 0.05:
        crep["why"] = "already neutral"
    elif abs(perp) > max(0.04, 0.35 * abs(along)):
        crep["why"] = "the reference is tinted off the warm-cool axis (%s): not a lamp or shade cast" % (
            "greenish" if perp < 0 else "magenta")
    elif along > warm_cap or along < -cool_cap or cb > chroma_cap:
        crep["why"] = "the reference is too colourful to be a neutral surface under a %s light (C* %.0f)" % (
            "warm" if along > 0 else "cool", cb)
    else:
        corr = along * WARM                        # only along the axis: a tint of the material itself stays
        g = np.array([math.exp(-corr[0]), 1.0, math.exp(-corr[1])])
        gains = np.clip(g / float(g @ LUM64), 0.5, 2.5)
        crep.update(applied=True, gains=[_r(v) for v in gains], cast_after=_r(_chroma(unit(ref * gains)), 1))
        if gains.max() > 1.8:
            warn("the light was very %s: one colour channel was boosted x%.1f, so its noise may show. Daylight "
                 "from a window gives a cleaner photo" % ("warm" if along > 0 else "cool", gains.max()))
    expo = None
    if kind == "margin" and (not strict or field is not None):
        lvl = Y[inl] * (field[1] / fe[inl] if field is not None else 1.0)
        level = float(np.median(lvl)) * (float((ref * gains) @ LUM64) / float(ref @ LUM64) if gains is not None
                                         else 1.0)
        top = float(np.percentile(rgb[inl].max(1), 95))
        if level >= 0.3 and top < 0.95:            # a light reference, not already clipped
            e = float(np.clip(PAPER_WHITE / level, 0.8, 1.6))
            expo = e if abs(e - 1) >= 0.03 else None
    return field, gains, expo, lrep, crep


def rectify(photo, out, review=None, corners=None, frame="auto", lens="auto", light="auto", color="auto",
            max_side=4096):
    """Find the artwork in a photo, correct it and write out (PNG). Returns (exit code, report): 0 written,
    4 nothing to correct or no boundary found with confidence (nothing written). max_side caps the output's longer
    side (0: no cap); the output is never upsampled beyond the work's own detail in the photo."""
    t0 = time.time()
    pix, _ = io.load_image(photo)
    alpha = pix[..., 3] if pix.shape[-1] == 4 else None
    translucent = alpha is not None and bool((alpha < 0.999).any())
    rgb = _u8(pix[..., :3])            # the decoded 8-bit levels again: a quarter of the memory of floats
    del pix, alpha
    H, W = rgb.shape[:2]
    cam = camera(photo, (W, H))
    rep = {"changed": False, "written": None, "review": None, "size_in": [W, H], "size_out": None,
           "quad": {"corners": None, "source": None, "confidence": 0.0},
           "lens": {"applied": False, "k1": 0.0, "straightness_px_before": None, "straightness_px_after": None},
           "perspective": {"applied": False, "aspect": None, "aspect_method": None, "tilt_deg": None},
           "light": {"applied": False, "reference": "none", "variation_before": None, "variation_after": None},
           "color": {"applied": False, "reference": "none", "gains": [1.0, 1.0, 1.0], "cast_before": None,
                     "cast_after": None},
           "frame": {"found": False, "removed": False}, "exposure": None,
           "camera": {k: (_r(v, 1) if isinstance(v, float) else v) for k, v in cam.items()},
           "warnings": [], "note": ""}
    warn = rep["warnings"].append

    def done(code, note=None):
        if note:
            rep["note"] = note
        if code == 0 and not rep["changed"]:
            code = 4                   # the contract: exit 0 only when a corrected picture was made (and written)
        rep["seconds"] = _r(time.time() - t0, 2)
        return code, json.loads(json.dumps(rep))
    if translucent and corners is None:
        return done(4, "the picture has transparency, so it is a digital image, not a photo: left as it is")
    small, sc = _small(rgb)
    maps = _edge_maps(small)
    diag = math.hypot(W, H)
    c0, R0 = (W / 2.0, H / 2.0), diag / 2.0
    full = np.array([[0, 0], [W, 0], [W, H], [0, H]], float)

    # ---- 1. the boundary
    cands, vis_note = _boundary(photo, rgb, small, sc, maps, corners, cam["camera_photo"])
    if vis_note:
        rep["vision"] = vis_note
    best = cands[0] if cands and cands[0]["conf"] >= MIN_CONF else None
    flat = False
    if best is not None and best["source"] != "given" and not cam["camera_photo"] and _square_on(best["quad"]):
        flat, best = True, None
    rep["alternatives"] = [{"corners": [[_r(x, 1), _r(y, 1)] for x, y in c["quad"]], "source": c["source"],
                            "confidence": _r(c["conf"])} for c in cands[(1 if best else 0):4]]
    found = best
    whole = best is None or (best["source"] != "given" and
                             np.abs(best["quad"] - full).max() <= max(3.0, 0.006 * min(W, H)))
    if whole:              # the picture's own edges are the work's: only a paper margin inside could still help
        best = {"quad": full, "ref": None, "source": None, "conf": 1.0}
    outer = best["quad"]

    # ---- 2. frame, mat or paper margin
    asp0 = sum(_sides(outer)[0::2]) / max(1e-9, sum(_sides(outer)[1::2]))
    bands = _bands(small, outer * sc, asp0, whole)
    bs = bands["sides"]
    dims = (bands["size"][1], bands["size"][0])
    # A band of material that goes all round, ending at a straight edge on at least three sides (the fourth edge
    # is then found by the refinement below, which must hold on all four).
    banded = all(s["rows"] >= max(3, 0.01 * dims[i % 2]) for i, s in enumerate(bs))
    depths = [s["depth"] for s in bs]
    rep["frame"]["found"] = bool(banded and sum(s["straight"] for s in bs) >= 3 and max(depths) <= 0.33)
    if banded:
        rep["frame"]["widths"] = [_r(v) for v in depths]
    art, inner_ref = outer, None
    if rep["frame"]["found"] and frame != "keep" and best["source"] not in (None, "given"):
        iq = _apply(bands["H"], _band_quad(bands)) / sc
        inner_ref = _refine(rgb, iq, 2.0 / sc + 3.0)
        if inner_ref["support"] >= 0.5:
            art = inner_ref["quad"]
            rep["frame"]["removed"] = True
        else:
            inner_ref = None
    if frame == "remove" and not rep["frame"]["removed"] and best["source"] not in (None, "given"):
        # an ornate or uneven frame is no uniform band; take the outermost credible boundary well inside the outer
        margin = 0.01 * math.sqrt(_area(outer))
        inner = [c for c in cands if c is not best and c["conf"] >= MIN_CONF and
                 _area(c["quad"]) >= 0.3 * _area(outer) and _inside(c["quad"], outer, margin)]
        if inner:
            c = max(inner, key=lambda c: _area(c["quad"]))
            art, inner_ref = c["quad"], c["ref"]
            rep["frame"].update(found=True, removed=True)

    # ---- 3. evidence for light and colour
    small_lin = _lin_f(small).astype(np.float32)
    evid, kind = _evidence(bands, small_lin, maps[0], outer * sc, sc, whole)

    # ---- 4. lens
    k1 = 0.0
    pts_sides = [s["pts"] for r in (best.get("ref"), inner_ref) if r for s in r["sides"] if s["support"] >= 0.5]
    if pts_sides and lens != "off" and best["source"] is not None:
        k, sb, sa = _lens_fit(pts_sides, c0, R0)
        if sb is not None:
            rep["lens"].update(k1=_r(k, 4), straightness_px_before=_r(sb, 2), straightness_px_after=_r(sb, 2))
            px_work = max(W, H) / float(WORK)          # input px per working px
            if sb - sa > 0.1 * px_work and sa < 0.75 * sb and abs(k) < 0.29:
                k1 = k
                rep["lens"].update(applied=True, straightness_px_after=_r(sa, 2))
    sides_art = (inner_ref or best.get("ref") or {}).get("sides")
    qu = _lines_quad(sides_art, k1, c0, R0) if (sides_art and best["source"] not in (None, "given")) else None
    if qu is None or np.abs(qu - np.c_[_undistort(art[:, 0], art[:, 1], k1, c0, R0)]).max() > 0.02 * diag:
        qu = np.c_[_undistort(art[:, 0], art[:, 1], k1, c0, R0)]
    art_photo = np.c_[_distort(qu[:, 0], qu[:, 1], k1, c0, R0)]
    if best["source"] is not None:
        rep["quad"] = {"corners": [[_r(x, 1), _r(y, 1)] for x, y in art_photo], "source": best["source"],
                       "confidence": _r(best["conf"])}
        if best.get("blind_side"):
            warn("the %s edge of the work is not visible in the photo (glare, shadow or a background as light as "
                 "the paper); Apple Vision placed it. Check that side on the review sheet, or reshoot against a "
                 "darker background" % best["blind_side"])
    elif cands:
        rep["quad"]["confidence"] = _r(cands[0]["conf"])

    # ---- 5. proportions and output size
    top, right, bottom, left = _sides(qu)
    asp_sides = (top + bottom) / max(1e-9, left + right)
    f, f_src = cam["focal_px"], "exif"
    if not f:
        # No usable focal length in the file: the rectangle's own two vanishing points give it when the view is
        # oblique on both axes (taken only if it holds under corner jitter of 0.05% of the diagonal); otherwise a
        # phone's main camera (26 mm) is assumed. On 100 synthetic photos this cut the error of the mean of opposite
        # sides (which leans towards the near side) from a median 1.5% to 0.2%.
        f_vp = _zhang_he(qu, None, c0)[2]
        jit = np.random.default_rng(1).normal(0, 0.0005 * diag, (8, 4, 2))
        fj = [_zhang_he(qu + j, None, c0)[2] for j in jit] if f_vp else []
        if f_vp and all(fj) and 0.3 * diag < f_vp < 4 * diag and max(fj) / min(fj) < 1.15:
            f, f_src = f_vp, "vanishing points"
        else:
            f, f_src = PHONE_F35 / FILM_DIAG * diag, "assumed %d mm" % PHONE_F35
    asp, nrm, _ = _zhang_he(qu, f, c0)
    method = "focal" if asp and 0.1 < asp < 10 else "sides"
    if method == "sides":
        asp, f_src = asp_sides, None
        _, nrm, _ = _zhang_he(qu, PHONE_F35 / FILM_DIAG * diag, c0)
    tilt = math.degrees(math.acos(min(1.0, abs(float(nrm[2]))))) if nrm is not None else None
    Wo = max(top, bottom)
    Ho = Wo / asp
    if Ho < max(left, right):
        Ho = max(left, right)
        Wo = Ho * asp
    for _ in range(4):                 # grow until no corner of the input is shrunk
        Hinv = _hom([[0, 0], [Wo, 0], [Wo, Ho], [0, Ho]], qu)
        g = 0.0
        for (u, v) in ((0, 0), (Wo, 0), (Wo, Ho), (0, Ho)):
            p0, pu, pv = _apply(Hinv, [[u, v], [u + 1, v], [u, v + 1]])
            g = max(g, np.linalg.norm(pu - p0), np.linalg.norm(pv - p0))
        if g <= 1.01:
            break
        Wo, Ho = Wo * g, Ho * g
    if Wo * Ho > 1.5 * W * H:
        s = math.sqrt(1.5 * W * H / (Wo * Ho))
        Wo, Ho = Wo * s, Ho * s
    Wo, Ho = max(8, int(round(Wo))), max(8, int(round(Ho)))
    Hinv = _hom([[0, 0], [Wo, 0], [Wo, Ho], [0, Ho]], qu)
    identity = bool(np.abs(art_photo - full).max() <= (0.5 if best["source"] == "given" else
                                                       max(3.0, 0.006 * min(W, H))))
    if not identity:
        rep["perspective"].update(applied=True, aspect=_r(asp, 4), aspect_method=method, aspect_sides=_r(asp_sides, 4),
                                  focal=f_src, focal_35mm=_r(f / diag * FILM_DIAG, 1) if f_src else None,
                                  tilt_deg=_r(tilt, 1), focal_note=cam["focal_note"])

    # ---- 6. light and colour, modelled on the work's own plane: work rectangle coordinates (the output's, about
    # -1..1 over its longer side), where a lamp's falloff, a hotspot or a shadow is a smooth function
    field = gains = expo = None
    half = 0.5 * max(Wo, Ho)
    Hfwd = np.linalg.inv(Hinv)
    w_ctr = float((Hfwd @ np.r_[qu.mean(0), 1.0])[2])

    def to_out(p):
        """Photo px -> work rectangle coordinates; NaN behind the vanishing line or far beyond the work."""
        p = np.asarray(p, float).reshape(-1, 2)
        xu, yu = _undistort(p[:, 0], p[:, 1], k1, c0, R0)
        q = np.c_[xu, yu, np.ones(len(p))] @ Hfwd.T
        with np.errstate(divide="ignore", invalid="ignore"):
            uv = np.c_[(q[:, 0] / q[:, 2] - Wo / 2.0) / half, (q[:, 1] / q[:, 2] - Ho / 2.0) / half]
        uv[(q[:, 2] * w_ctr <= 0) | ~(np.abs(uv).max(1) <= 2.5)] = np.nan
        return uv
    if evid is not None:
        uv = to_out(evid["xy"])
        ok = np.isfinite(uv).all(1)
        evid = dict(evid, uv=uv[ok], xy=evid["xy"][ok], rgb=evid["rgb"][ok], side=evid["side"][ok],
                    clip=evid["clip"][ok])
        gu, gv = np.meshgrid(np.linspace(0.02, 0.98, 24) * Wo, np.linspace(0.02, 0.98, 24) * Ho)
        guv = np.c_[(gu.ravel() - Wo / 2.0) / half, (gv.ravel() - Ho / 2.0) / half]
        field, gains, expo, lrep, crep = _light_colour(evid, kind, guv, light, color, whole, cam["camera_photo"],
                                                       warn)
        rep["light"], rep["color"] = lrep, crep
        if expo is not None:          # never push the painting's own lightest paint into white
            yy, xx = np.nonzero(io.poly_mask(small.shape, art_photo * sc))
            step = max(1, len(yy) // 40000)
            yy, xx = yy[::step], xx[::step]
            v = small_lin[yy, xx]
            if len(v):
                mul = (gains if gains is not None else np.ones(3))
                if field is not None:
                    uvp = np.nan_to_num(to_out(np.c_[xx + 0.5, yy + 0.5] / sc))
                    v = v / (np.exp(field[0](uvp)) / field[1])[:, None]
                top_v = float(np.percentile((v * mul).max(1), 99.9))
                if top_v * expo > 0.995:
                    expo = max(1.0, 0.995 / max(top_v, 1e-6)) if expo > 1 else expo
                    expo = None if abs(expo - 1) < 0.03 else expo
    else:
        why = "no neutral paper margin, mat or plain wall to measure against" if not whole else \
            "no paper margin to measure against"
        rep["light"]["why"] = rep["color"]["why"] = why

    if not (not identity or field is not None or gains is not None or k1 or expo is not None):
        if flat:
            return done(4, "a rectangle was found, but square-on and without camera data, so it is taken as part of "
                           "a flat picture (a shape in the painting, or a scan's paper margin): kept as it is. Pass "
                           "--corners to crop it anyway")
        if whole and found is None:
            hint = ""
            for c in (cands[:3] if cam["camera_photo"] else []):     # a flat picture's painted lines are no hint
                sups = [sd["support"] for sd in c["ref"]["sides"]]
                q_ = c["quad"]
                out_ = (q_[:, 0] < 2) | (q_[:, 0] > W - 2) | (q_[:, 1] < 2) | (q_[:, 1] > H - 2)
                if min(sups) >= 0.8 and _area(q_) < 0.05 * W * H and _convex(q_):
                    hint = (" A small rectangle (%.0f%% of the photo) looks like the work: move closer, or pass its "
                            "corners (see alternatives)." % (100 * _area(q_) / (W * H)))
                    warn("too few pixels: the work seems to be a small rectangle (%.0f%% of the photo); move closer "
                         "and reshoot, or pass its corners" % (100 * _area(q_) / (W * H)))
                    break
                if sorted(sups)[1] >= 0.8 and (min(sups) < 0.5 or out_.any()) and _area(q_) >= 0.05 * W * H:
                    side = ("top", "right", "bottom", "left")[int(np.argmin(sups))]
                    hint = (" Three edges of a likely boundary were found but the %s one is not visible (glare, "
                            "shadow, a background like the paper, or cut off); see alternatives." % side)
                    warn("the work may be cut off or partly hidden: three of its edges were found, but not the %s "
                         "one%s. Reshoot with the whole work and a little wall or table round it" % (
                             side, " (a corner lies outside the photo)" if out_.any() else ""))
                    break
            if cam["camera_photo"]:
                # the photo itself may be why no boundary was found
                ew = [v for v in _edge_widths(rgb, full, min(1.0, WORK / float(max(W, H)))) if v is not None]
                if len(ew) >= 2 and max(ew) >= 2.5 * min(ew) and max(ew) >= BLUR_SHAKE:
                    warn("camera shake: the photo looks smeared in one direction; hold the phone still and reshoot")
                elif len(ew) >= 2 and min(ew) >= 1.3 * BLUR_CRISP:
                    warn("the photo looks out of focus (blur); tap the painting to focus and reshoot")
            return done(4, "no artwork boundary found with confidence: kept as it is.%s If this is a photo of a "
                           "painting, pass --corners \"x,y;x,y;x,y;x,y\" (top-left, top-right, bottom-right, "
                           "bottom-left), or reshoot with some wall or table showing round it" % hint)
        return done(4, "the artwork fills the whole picture and its light is even: nothing to correct")

    # ---- 7. resample and correct, in linear light, at most max_side px on the longer side
    gl = np.ones(3, np.float32) if gains is None else gains.astype(np.float32)
    mul = gl * np.float32(1.0 if expo is None else expo)
    grid = _field_grid(field, (Wo, Ho)) if field is not None else None
    # Only the part of the photo under the work is needed: its box (sides sampled, so a bowed side is inside)
    # plus the cubic spline's reach; a 48 MP photo of a work filling half of it then costs half.
    tt = np.linspace(0, 1, 33)[:, None]
    ring = np.concatenate([qu[i] + tt * (qu[(i + 1) % 4] - qu[i]) for i in range(4)])
    bx, by = _distort(ring[:, 0], ring[:, 1], k1, c0, R0)
    x0, x1 = int(np.clip(math.floor(bx.min()) - 4, 0, W - 2)), int(np.clip(math.ceil(bx.max()) + 5, 2, W))
    y0, y1 = int(np.clip(math.floor(by.min()) - 4, 0, H - 2)), int(np.clip(math.ceil(by.max()) + 5, 2, H))
    if x1 - x0 < 4 or y1 - y0 < 4:
        x0, x1, y0, y1 = 0, W, 0, H
    crop = rgb[y0:y1, x0:x1]
    cw, Rw, s_cap, off, psize = c0, R0, 1.0, (x0, y0), (W, H)
    if max_side and max(Wo, Ho) > max_side:
        # the photo is first reduced in linear light (Lanczos, so nothing aliases), one channel at a time, then
        # resampled as usual: the output still never shrinks the (reduced) photo anywhere
        s_cap = max_side / float(max(Wo, Ho))
        Wr, Hr = max(8, int(round(Wo * s_cap))), max(8, int(round(Ho * s_cap)))
        wi, hi = max(2, int(round((x1 - x0) * s_cap))), max(2, int(round((y1 - y0) * s_cap)))
        lin_in = np.dstack([np.asarray(Image.fromarray(_to_lin(np.ascontiguousarray(crop[..., ch]))).resize(
            (wi, hi), Image.LANCZOS), np.float32) for ch in range(3)])
        np.clip(lin_in, 0, None, out=lin_in)
        sx, sy = wi / float(x1 - x0), hi / float(y1 - y0)
        Hinv = np.diag([sx, sy, 1.0]) @ Hinv @ np.diag([Wo / float(Wr), Ho / float(Hr), 1.0])
        cw, Rw = (c0[0] * sx, c0[1] * sy), R0 * math.sqrt(sx * sy)
        off, psize = (x0 * sx, y0 * sy), (W * sx, H * sy)
        Wo, Ho = Wr, Hr
        rep["perspective"]["capped"] = max_side
    else:
        lin_in = _to_lin(crop)
    del crop
    if grid is not None:
        Mx = _lerp_weights(np.arange(Wo) + 0.5, grid.shape[1], Wo).T.copy()
        My = _lerp_weights(np.arange(Ho) + 0.5, grid.shape[0], Ho)

    def post(chunk, y0_):
        if grid is not None:
            chunk *= ((My[y0_:y0_ + len(chunk)] @ grid) @ Mx)[..., None]
        chunk *= mul
    res_lin, valid = _warp(lin_in, Hinv, (Wo, Ho), k1, cw, Rw, post=post, offset=off, photo_size=psize)
    del lin_in
    # how sharp the work's boundary is, at the working size (prepare's 1600 px): working px per photo px is the
    # output's own scale (its nearest part keeps the photo's pixels, times the cap) times the working reduction
    work_frac = min(1.0, WORK / float(max(Wo, Ho)))
    scale_w = s_cap * work_frac
    sw = _side_widths(rgb, sides_art, scale_w) if best["source"] is not None else [None] * 4
    known = [v for v in sw if v is not None]
    # trim the blurred rim: about half the edge's 10-90% width beyond the line, in output px
    trim = int(np.clip(round(1 + 0.6 * (float(np.median(known)) / work_frac if known else 1.0)), 1, 4)) \
        if not identity else 0
    if trim and min(Wo, Ho) > 8 * trim:
        res_lin, valid = res_lin[trim:-trim, trim:-trim], valid[trim:-trim, trim:-trim]
    else:
        trim = 0
    clipped = float((res_lin.max(-1) > 1.0).mean())
    a8 = _to_srgb8(res_lin)
    del res_lin
    Ho2, Wo2 = a8.shape[:2]
    if out is not None:
        if not out.lower().endswith(".png"):
            out = os.path.splitext(out)[0] + ".png"
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        if not valid.all():            # parts outside the photo stay transparent rather than smeared
            Image.fromarray(np.dstack([a8, (valid * 255).astype(np.uint8)]), "RGBA").save(out, "PNG", compress_level=3)
        else:
            Image.fromarray(a8, "RGB").save(out, "PNG", compress_level=3)
        rep["written"] = out
    rep.update(changed=True, size_out=[Wo2, Ho2], exposure=_r(expo))
    if clipped > 0.002:
        warn("%.1f%% of the corrected picture reached white; check the lightest paint" % (100 * clipped))

    # ---- 8. warnings: when a reshoot would do better than any correction
    if not identity:
        if max(top, bottom, left, right) < 1000 or _area(qu) < 0.5e6:
            warn("too few pixels: the painting covers only about %dx%d px of the photo (low resolution for a %d px "
                 "working picture); fill more of the frame with it, or move closer" % (
                     int(round(max(top, bottom))), int(round(max(left, right))), WORK))
        if tilt is not None and tilt > 20 and f_src and f_src.startswith("assumed"):
            warn("the file has no focal length and the view is oblique, so the proportions assume a phone's main "
                 "camera; with a wide or zoom lens they may be off by a few percent. Check them against the "
                 "painting's real size")
        if tilt is not None and tilt > 35:
            warn("taken at a steep angle (about %d degrees): the far side is stretched from fewer pixels. Reshoot "
                 "more square-on, the phone parallel to the painting" % int(round(tilt)))
        else:
            ratio = min(top / max(bottom, 1e-9), bottom / max(top, 1e-9), left / max(right, 1e-9),
                        right / max(left, 1e-9))
            if ratio < 0.7:
                warn("one side of the painting is %d%% shorter in the photo than its opposite, so its detail there "
                     "is thinner. Reshoot more square-on" % int(round(100 * (1 - ratio))))
        m = max(2.0, 0.004 * min(W, H))
        touch = [nm for nm, hit in (("top", (art_photo[:, 1] < m).any()), ("bottom", (art_photo[:, 1] > H - m).any()),
                                    ("left", (art_photo[:, 0] < m).any()), ("right", (art_photo[:, 0] > W - m).any()))
                 if hit]
        m2 = 0.015 * min(W, H)          # a whole side running along the photo's border: the work may go on past it
        for i in range(4):
            a_, b_ = art_photo[i], art_photo[(i + 1) % 4]
            for nm, hit in (("left", max(a_[0], b_[0]) < m2), ("top", max(a_[1], b_[1]) < m2),
                            ("right", min(a_[0], b_[0]) > W - m2), ("bottom", min(a_[1], b_[1]) > H - m2)):
                if hit and nm not in touch:
                    touch.append(nm)
        if touch:
            warn("the work touches the %s edge of the photo, so part of it may be cut off. Reshoot with a little "
                 "wall or table showing all round" % " and ".join(touch))
    if valid.mean() < 0.9995:
        warn("%.1f%% of the result lies outside the photo (cut off) and is left transparent" % (
            100 * (1 - valid.mean())))
    share, boxes = _glare(small, art_photo, sc)
    if share > 0.001:
        warn("glare: %.1f%% of the painting is washed out by reflections (%d spot%s, clipped to white); the paint "
             "under it cannot be recovered and was not painted over. Reshoot with the light from the side, no "
             "flash, turned a little so the reflection falls outside the work" % (
                 100 * share, len(boxes), "" if len(boxes) == 1 else "s"))
    # Sharpness, at the working size: the boundary's own edge along each side (a known straight edge), and the
    # crispest edges round and inside the work per direction. Either alone can mislead (a frame's bevel, a soft
    # painting on a white wall), so a warning needs both to agree.
    ew = _edge_widths(rgb, art_photo, scale_w)
    kn_e = [v for v in ew if v is not None]
    rep["sharpness"] = {"edge_width_work_px": [_r(v, 2) for v in sw],
                        "crispest_by_direction_work_px": [_r(v, 2) for v in ew]}
    if len(known) >= 3:
        lo, hi = min(known), max(known)
        if hi >= BLUR_SHAKE and hi >= 2.0 * lo and kn_e and max(kn_e) >= 1.6 * min(kn_e):
            soft = [("top", "right", "bottom", "left")[i] for i, v in enumerate(sw)
                    if v is not None and v >= 1.6 * lo]
            warn("camera shake: the photo looks smeared in one direction (the %s edges are %.1f px wide at working "
                 "size, the others %.1f). Hold the phone still, or lean it on something, and reshoot" % (
                     " and ".join(soft), hi, lo))
        elif lo >= BLUR_FOCUS and (not kn_e or min(kn_e) >= BLUR_CRISP):
            warn("the photo looks out of focus (blur): even the work's crisp edges are %.1f px wide at working size; "
                 "fine brushwork may be lost. Tap the painting to focus, hold the phone still, and reshoot" % lo)
    if len(kn_e) >= 2 and not any("shake" in w_ or "out of focus" in w_ for w_ in rep["warnings"]):
        # the crispest edges alone, when the boundary could not tell (not found, or a wrong one): only a strong
        # smear counts (sharp photos: at most 2.5 working px, and within 1.3x across directions)
        if max(kn_e) >= 5.0 and max(kn_e) >= 2.5 * min(kn_e):
            warn("camera shake: the photo looks smeared in one direction (its crispest edges are %.1f px wide across "
                 "one direction, %.1f across another). Hold the phone still and reshoot" % (max(kn_e), min(kn_e)))
        elif min(kn_e) >= 3.5:
            warn("the photo looks out of focus (blur): even its crispest edges are %.1f px wide at working size. "
                 "Tap the painting to focus and reshoot" % min(kn_e))
    if best["source"] not in (None, "given"):
        cont = _continues(small, art_photo * sc)
        if cont:
            warn("beyond the %s edge the picture seems to go on (marks of the same kind on both sides): check on the "
                 "review sheet that this is the real edge of the work; if the work runs past the photo, reshoot it "
                 "whole" % " and ".join(cont))
    if evid is None and not whole:
        warn("light and colour left as photographed: no paper margin, mat or plain wall was found to measure them "
             "against. If the photo looks warm or unevenly lit, reshoot in daylight, or with some plain white wall "
             "or paper showing round the work")
    elif kind == "surround" and gains is not None:
        warn("the colour was balanced on the wall or table round the work; if that surface is not really grey or "
             "white (a cream wall, a wooden table), rerun with --color off")

    # ---- 9. review sheet and note
    if review:
        change = None
        if field is not None or expo is not None:
            pw = min(Wo2, 360)
            ph = max(1, int(round(Ho2 * pw / float(Wo2))))
            m_ = np.full((ph, pw), 1.0 if expo is None else expo)
            if grid is not None:
                m_ = m_ * (_lerp_weights((np.arange(ph) + 0.5) * Ho2 / ph + trim, grid.shape[0], Ho) @ grid @
                           _lerp_weights((np.arange(pw) + 0.5) * Wo2 / pw + trim, grid.shape[1], Wo).T)
            change = np.log2(np.maximum(m_, 1e-6))
        quads = [(outer, (0, 220, 255), 2)] if rep["frame"]["removed"] else []
        if best["source"] is not None:
            quads.append((art_photo, (255, 0, 255), 3))
        _review(review, rgb, quads, evid["xy"] if evid is not None else None, boxes, a8, change, rep)
        rep["review"] = review
    notes = []
    if rep["frame"]["removed"]:
        notes.append("kept the painted surface inside a frame or mat")
    elif rep["frame"]["found"] and best["source"] is not None:
        notes.append("a frame or mat was found and kept")
    if not identity:
        notes.append("perspective corrected (tilt about %s degrees, aspect %.3f from %s)" % (
            "?" if tilt is None else int(round(tilt)), asp,
            {"exif": "the focal length in the file", "vanishing points": "the focal length its vanishing points give",
             None: "the opposite sides"}.get(f_src, "an assumed %d mm phone lens" % PHONE_F35)))
    if k1:
        notes.append("lens distortion removed (k1 %.3f)" % k1)
    if field is not None:
        notes.append("light evened out against the %s" % kind)
    elif light != "off":
        notes.append("light left as photographed (%s)" % (rep["light"].get("why") or "no neutral evidence"))
    if gains is not None:
        notes.append("colour cast removed against the %s" % kind)
    elif color != "off":
        notes.append("colour left as photographed (%s)" % (rep["color"].get("why") or "no neutral evidence"))
    if expo is not None:
        notes.append("exposure x%.2f so the %s is paper white" % (expo, kind))
    return done(0, "; ".join(notes) + ". Check the review sheet before animating.")
