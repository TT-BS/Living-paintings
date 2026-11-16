"""Fill what a lifted layer was hiding, using the picture's own paint.

A patch-based fill (PatchMatch search + patch voting, coarse to fine). It only
ever copies pixels from elsewhere in the same picture, so brushwork, paper
grain and palette stay those of the original. No model, no network.
"""
import numpy as np
from scipy import ndimage as ndi

from . import imgops as io


def diffuse_fill(img, hole, soften=(8.0, 3.0), source=None):
    """Cheap fill: each hole pixel takes its nearest known colour, then the
    result is softened inside the hole. Colours therefore continue inward
    from whichever edge is closest (wall near the wall, floor near the floor)."""
    if not hole.any():
        return img.copy()
    known = ~hole if source is None else (~hole & source.astype(bool))
    if not known.any():
        raise ValueError("fill has no known pixels in its declared background source")
    idx = ndi.distance_transform_edt(~known, return_distances=False, return_indices=True)
    out = np.where(hole[...,None], img[idx[0], idx[1]], img).copy()
    dist = ndi.distance_transform_edt(hole)
    for sigma in soften:
        allowed = (hole | known).astype(np.float32)
        blur = io.gaussian(out*allowed[...,None], sigma)/np.maximum(io.gaussian(allowed,sigma)[...,None],1e-8)
        t = np.clip(dist / (sigma * 1.5), 0, 1)[..., None]
        out = np.where(hole[..., None], out * (1 - t) + blur * t, out)
    return out


def _down(img, hole):
    h, w = hole.shape
    known = (~hole).astype(np.float32)
    k = known.reshape(h // 2, 2, w // 2, 2).sum((1, 3))
    c = (img * known[..., None]).reshape(h // 2, 2, w // 2, 2, 3).sum((1, 3))
    out = c / np.maximum(k, 1e-6)[..., None]
    return out.astype(np.float32), k < 3.5


def _offsets(half, step):
    r = list(range(-half, half + 1, step))
    return [(dy, dx) for dy in r for dx in r]


def _dist(pad, cpad, half, ty, tx, sy, sx, offs):
    """Patch difference, trusting real paint more than our own guesses.

    `cpad` is 1 on known pixels and small inside the hole, so a patch on the
    hole's edge is matched by the real context it touches, and good paint
    grows inward from the edge instead of an early guess reinforcing itself.
    """
    d = np.zeros(len(ty), dtype=np.float32)
    wsum = np.zeros(len(ty), dtype=np.float32)
    for dy, dx in offs:
        yy, xx = ty + half + dy, tx + half + dx
        a = pad[yy, xx]
        b = pad[sy + half + dy, sx + half + dx]
        df = a - b
        w = cpad[yy, xx]
        d += (df * df).sum(-1) * w
        wsum += w
    return d / np.maximum(wsum, 1e-6)


def _level(img, hole, valid_src, nnf, rng, half, iters, target, sharp, step, search):
    """One pyramid level: alternate nearest-patch search and voting."""
    H, W = hole.shape
    ty, tx = np.nonzero(target)
    n = len(ty)
    if n == 0:
        return img, nnf
    vy, vx = np.nonzero(valid_src)
    if len(vy) < 32:
        return img, nnf
    offs = _offsets(half, step)
    full = _offsets(half, 1)

    ny, nx = nnf
    cy, cx = ny[ty, tx].copy(), nx[ty, tx].copy()
    bad = ~valid_src[np.clip(cy, 0, H - 1), np.clip(cx, 0, W - 1)] | (cy < 0) | (cy >= H) | (cx < 0) | (cx >= W)
    if bad.any():
        pick = rng.integers(0, len(vy), int(bad.sum()))
        cy[bad], cx[bad] = vy[pick], vx[pick]
    dist_to_known = ndi.distance_transform_edt(hole)

    for it in range(iters):
        pad = np.pad(img, ((half, half), (half, half), (0, 0)), mode="edge")
        trust = min(0.6, 0.12 + 0.16 * it)
        cpad = np.pad(np.where(hole, trust, 1.0).astype(np.float32), ((half, half), (half, half)), mode="edge")
        d = _dist(pad, cpad, half, ty, tx, cy, cx, offs)
        ny[ty, tx], nx[ty, tx] = cy, cx

        # propagation: take a neighbour's match, shifted
        for s in (1, 2, 4, 8, 16):
            if s > max(H, W) // 4:
                break
            for dy, dx in ((0, 1), (1, 0), (0, -1), (-1, 0)):
                qy = np.clip(ty - dy * s, 0, H - 1)
                qx = np.clip(tx - dx * s, 0, W - 1)
                ay = ny[qy, qx] + dy * s
                ax = nx[qy, qx] + dx * s
                ok = (ay >= 0) & (ay < H) & (ax >= 0) & (ax < W)
                ok[ok] = valid_src[ay[ok], ax[ok]]
                if not ok.any():
                    continue
                dn = _dist(pad, cpad, half, ty[ok], tx[ok], ay[ok], ax[ok], offs)
                better = dn < d[ok]
                sel = np.nonzero(ok)[0][better]
                cy[sel], cx[sel], d[sel] = ay[ok][better], ax[ok][better], dn[better]
                ny[ty, tx], nx[ty, tx] = cy, cx

        # random search around the current match, shrinking radius
        rad = float(search)
        while rad >= 1.0:
            ay = cy + rng.integers(-int(rad), int(rad) + 1, n)
            ax = cx + rng.integers(-int(rad), int(rad) + 1, n)
            ok = (ay >= 0) & (ay < H) & (ax >= 0) & (ax < W)
            ok[ok] = valid_src[ay[ok], ax[ok]]
            if ok.any():
                dn = _dist(pad, cpad, half, ty[ok], tx[ok], ay[ok], ax[ok], offs)
                better = dn < d[ok]
                sel = np.nonzero(ok)[0][better]
                cy[sel], cx[sel], d[sel] = ay[ok][better], ax[ok][better], dn[better]
            rad *= 0.5
        ny[ty, tx], nx[ty, tx] = cy, cx

        # vote: every patch that covers a hole pixel proposes a colour for it
        last = it == iters - 1
        sig = np.percentile(d, 60) + 1e-6
        wq = np.exp(-d / sig * (4.0 if (sharp and last) else 1.0)).astype(np.float32)
        wq *= np.power(1.25, -np.minimum(dist_to_known[ty, tx], 24)).astype(np.float32)
        wmap = np.zeros((H, W), dtype=np.float32)
        wmap[ty, tx] = wq + 1e-8
        hy, hx = np.nonzero(hole & io.dilate(target, half))
        acc = np.zeros((len(hy), 3), dtype=np.float32)
        wsum = np.zeros(len(hy), dtype=np.float32)
        for dy, dx in full:
            qy, qx = hy - dy, hx - dx
            ok = (qy >= 0) & (qy < H) & (qx >= 0) & (qx < W)
            w = np.zeros(len(hy), dtype=np.float32)
            w[ok] = wmap[qy[ok], qx[ok]]
            ok &= w > 0
            if not ok.any():
                continue
            sy = np.clip(ny[qy[ok], qx[ok]] + dy, 0, H - 1)
            sx = np.clip(nx[qy[ok], qx[ok]] + dx, 0, W - 1)
            acc[ok] += img[sy, sx] * w[ok, None]
            wsum[ok] += w[ok]
        got = wsum > 0
        img = img.copy()
        img[hy[got], hx[got]] = acc[got] / wsum[got, None]
    return img, (ny, nx)


def fill(img, hole, source=None, patch=7, reach=None, quality=2, seed=0, search=None):
    """Fill `hole` (bool, True = unknown) in `img` (H x W x 3 float).

    source : optional bool mask of where paint may be copied from
    reach  : only the band of this many px inside the hole edge gets the full
             treatment; deeper pixels (never revealed by small motion) get a
             cheaper fill. None = treat the whole hole properly.
    search : how far (px) from a hole pixel paint may be fetched. Default: a
             few hundred px, so the fill uses nearby context first.
    Returns the filled image.
    """
    hole = hole.astype(bool)
    if not hole.any():
        return img.copy()
    H0, W0 = hole.shape
    rng = np.random.default_rng(seed)
    half = patch // 2

    # work on a crop around the hole: nearby paint is the most relevant
    bb = io.bbox(hole)
    span = max(bb[2] - bb[0], bb[3] - bb[1])
    margin = int(search or np.clip(span * 0.75, 96, 420))
    x0, y0 = max(0, bb[0] - margin), max(0, bb[1] - margin)
    x1, y1 = min(W0, bb[2] + margin), min(H0, bb[3] + margin)
    sub = img[y0:y1, x0:x1].astype(np.float32)
    hsub = hole[y0:y1, x0:x1]
    ssub = np.ones_like(hsub) if source is None else source[y0:y1, x0:x1].astype(bool)
    ssub = ssub & ~hsub

    rmax = float(ndi.distance_transform_edt(hsub).max())
    # a shallow pyramid: go too coarse and thin strips of context (a band of wall
    # between two things) vanish, leaving nothing true to copy from
    levels = int(np.clip(np.ceil(np.log2(max(rmax, 1.0) / 6.0)), 0, 3))
    while levels > 0 and min(sub.shape[:2]) / (2 ** levels) < 28:
        levels -= 1
    mult = 2 ** levels
    ph, pw = (-sub.shape[0]) % mult, (-sub.shape[1]) % mult
    real = np.ones(hsub.shape, dtype=bool)
    if ph or pw:
        sub = np.pad(sub, ((0, ph), (0, pw), (0, 0)), mode="edge")
        hsub = np.pad(hsub, ((0, ph), (0, pw)), mode="edge")
        ssub = np.pad(ssub, ((0, ph), (0, pw)), mode="constant")
        real = np.pad(real, ((0, ph), (0, pw)), mode="constant")

    pyr = [(sub, hsub, ssub)]
    for _ in range(levels):
        i, h, s = pyr[-1]
        i2, h2 = _down(i, h)
        s2 = s.reshape(s.shape[0] // 2, 2, s.shape[1] // 2, 2).all((1, 3))
        # A narrow, valid donor strip must not disappear merely because the
        # requested hole suggested a deeper pyramid. Work at the finest level
        # that still has observed donor paint.
        if not (s2 & ~h2).any():
            break
        pyr.append((i2, h2, s2))
    levels = len(pyr)-1

    cur = None
    nnf = None
    for li in range(levels, -1, -1):
        im, h, s = pyr[li]
        Hh, Ww = h.shape
        if cur is None:
            cur = diffuse_fill(im, h, soften=(1.5,), source=s)
            yy, xx = np.mgrid[0:Hh, 0:Ww]
            nnf = (yy.astype(np.int32), xx.astype(np.int32))
        else:
            up = np.repeat(np.repeat(cur, 2, axis=0), 2, axis=1)[:Hh, :Ww]
            cur = np.where(h[..., None], up, im)
            ny = np.repeat(np.repeat(nnf[0] * 2, 2, axis=0), 2, axis=1)[:Hh, :Ww]
            nx = np.repeat(np.repeat(nnf[1] * 2, 2, axis=0), 2, axis=1)[:Hh, :Ww]
            yy, xx = np.mgrid[0:Hh, 0:Ww]
            nnf = ((ny + (yy % 2)).astype(np.int32), (nx + (xx % 2)).astype(np.int32))
        half = (patch // 2) if li < max(2, levels) else max(1, patch // 2 - 1)
        inner = np.zeros_like(h)
        inner[half:Hh - half, half:Ww - half] = True
        valid = ndi.minimum_filter(s, size=2*half+1, mode="constant", cval=0).astype(bool) & inner & ~io.dilate(h, half + 0.5)
        target = io.dilate(h, half) & inner
        scale = 2 ** li
        if reach is not None and li <= 1:
            band = ndi.distance_transform_edt(h) <= max(reach / scale, half + 2)
            target &= io.dilate(band & h, half)
        n_t = int(target.sum())
        if li == 0:
            iters = {1: 1, 2: 2, 3: 3}.get(quality, 2)
        elif li == 1:
            iters = {1: 2, 2: 3, 3: 4}.get(quality, 3)
        else:
            iters = {1: 3, 2: 5, 3: 7}.get(quality, 5)
        step = 1 if n_t < 50000 else 2
        cur, nnf = _level(cur, h, valid, nnf, rng, half, iters, target, sharp=(li == 0),
                          step=step, search=max(Hh, Ww) / 2.0)

    cur = cur[:sub.shape[0] - ph if ph else None, :sub.shape[1] - pw if pw else None]
    out = img.copy()
    region = hole[y0:y1, x0:x1]
    patch_out = out[y0:y1, x0:x1]
    patch_out[region] = cur[region]
    return out
