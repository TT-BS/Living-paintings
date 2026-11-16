"""Measuring and inspection helpers: they read the picture and the cut, and print what you would otherwise
work out by eye or with a throwaway script. Every number they print is for you to check, not to trust blindly.

  measure  rows / blobs / outline of the pixels that pass a tone or colour rule inside a box
  palette  colour samples at points, or the main colours of a region (for like / unlike lists)
  inspect  one cut layer over several grounds next to what it uncovers; or each of its pieces separately
"""
import json
import os

import numpy as np
from PIL import Image, ImageDraw

from . import imgops as io


# ------------------------------------------------------------------ shared

def _rule_mask(rgb, tone="light", level=150.0, near=None, tol=0.08):
    """The pixels that pass: brighter/darker than `level` (luma 0..255), or within `tol` of colour `near`."""
    if near is not None:
        ref = io.rgb_to_lab(np.asarray(near, np.float32).reshape(1, 1, 3) / 255.0)[0, 0]
        return np.sqrt(((io.rgb_to_lab(rgb) - ref) ** 2).sum(-1)) < tol
    lum = io.luma(rgb) * 255.0
    return lum >= level if tone == "light" else lum <= level


def _box(rgb, box):
    H, W = rgb.shape[:2]
    if not box:
        return 0, 0, W, H
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    return max(0, x0), max(0, y0), min(W, x1), min(H, y1)


def _trace(mask):
    """Outer boundary of the largest 8-connected blob, as pixel centres in order (Moore neighbour tracing)."""
    from scipy import ndimage as ndi
    lab, n = ndi.label(mask, structure=np.ones((3, 3)))
    if n == 0:
        return []
    sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    m = np.pad(lab == (int(np.argmax(sizes)) + 1), 1)
    ys, xs = np.nonzero(m)
    start = (ys.min(), xs[ys == ys.min()].min())
    nbr = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]     # clockwise from north
    pts, cur, back = [start], start, 6        # we arrived from the west
    for _ in range(4 * m.size):
        for i in range(8):
            d = (back + 1 + i) % 8
            ny, nx = cur[0] + nbr[d][0], cur[1] + nbr[d][1]
            if m[ny, nx]:
                back = (d + 4) % 8
                cur = (ny, nx)
                break
        else:
            break                              # an isolated pixel
        if cur == start:
            break
        pts.append(cur)
    return [(x - 1, y - 1) for y, x in pts]


def _simplify(pts, eps):
    """Ramer-Douglas-Peucker on a closed outline."""
    if len(pts) < 4 or eps <= 0:
        return pts
    def rdp(seg):
        if len(seg) < 3:
            return seg
        a, b = np.asarray(seg[0], float), np.asarray(seg[-1], float)
        ab = b - a
        n = np.hypot(*ab) or 1.0
        d = [abs(ab[0] * (p[1] - a[1]) - ab[1] * (p[0] - a[0])) / n for p in seg[1:-1]]
        i = int(np.argmax(d)) + 1
        if d[i - 1] > eps:
            return rdp(seg[:i + 1])[:-1] + rdp(seg[i:])
        return [seg[0], seg[-1]]
    far = int(np.argmax([np.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) for p in pts]))
    return rdp(pts[:far + 1])[:-1] + rdp(pts[far:] + [pts[0]])[:-1]


def _ellipse(cx, cy, a, b, angle_deg, n=12):
    t = np.radians(angle_deg)
    out = []
    for k in range(n):
        u = 2 * np.pi * k / n
        x, y = a * np.cos(u), b * np.sin(u)
        out.append([round(float(cx + x * np.cos(t) - y * np.sin(t)), 2), round(float(cy + x * np.sin(t) + y * np.cos(t)), 2)])
    return out


# ------------------------------------------------------------------ measure

def measure(image, box=None, tone="light", level=150.0, near=None, tol=0.08, rows=0, blobs=False,
            outline=False, margin=3.0, min_area=6, simplify=1.0):
    rgb, _ = io.load_rgb(image)
    x0, y0, x1, y1 = _box(rgb, box)
    m = _rule_mask(rgb[y0:y1, x0:x1], tone, level, near, tol)
    res = {"box": [x0, y0, x1, y1],
           "rule": ("within %.3f of %s" % (tol, near)) if near is not None else ("luma %s %g" % (">=" if tone == "light" else "<=", level)),
           "passing_pixels": int(m.sum())}
    if rows:
        out = []
        for y in range(0, m.shape[0], int(rows)):
            r = np.concatenate([[False], m[y], [False]])
            edges = np.flatnonzero(r[1:] != r[:-1])
            out.append([y + y0, [[int(edges[i] + x0), int(edges[i + 1] - 1 + x0)] for i in range(0, len(edges), 2)]])
        res["rows"] = out
    if blobs:
        from scipy import ndimage as ndi
        lab, n = ndi.label(m, structure=np.ones((3, 3)))
        found = []
        for i in range(1, n + 1):
            ys, xs = np.nonzero(lab == i)
            if len(xs) < min_area:
                continue
            # The player's frame: pixel x covers [x, x+1], so a pixel's centre is x + 0.5. Index means would put the
            # blink domain half a pixel off the painted eye, and closing would leave a sliver of it behind.
            cx, cy = xs.mean() + x0 + 0.5, ys.mean() + y0 + 0.5
            cov = np.cov(np.stack([xs, ys])) if len(xs) > 2 else np.eye(2)
            ev, evec = np.linalg.eigh(cov)
            major = evec[:, 1]
            ang = float(np.degrees(np.arctan2(major[1], major[0])))
            a_ax, b_ax = 2.0 * np.sqrt(max(ev[1], 0.25)), 2.0 * np.sqrt(max(ev[0], 0.25))
            mask_poly = _ellipse(cx, cy, a_ax + 0.8, b_ax + 0.8, ang)
            pts = np.asarray(mask_poly)
            hx = float(np.abs(pts[:, 0] - cx).max()) + 1.5
            hy = float(np.abs(pts[:, 1] - cy).max()) + 1.5
            found.append({"centre": [round(float(cx), 2), round(float(cy), 2)], "area": int(len(xs)),
                          "bbox": [int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0), int(ys.max() + y0)],
                          "major_axis_deg": round(ang, 1), "half_axes": [round(float(a_ax), 2), round(float(b_ax), 2)],
                          "blink": {"type": "blink", "at": [round(float(cx), 2), round(float(cy), 2)],
                                    "r": [round(hx, 2), round(hy, 2)], "mask": mask_poly}})
        found.sort(key=lambda b: -b["area"])
        res["blobs"] = found[:12]
        res["note"] = ("centres and blink entries are in the player's frame (a pixel's centre is x+0.5, y+0.5); "
                       "blink entries are a starting point: check the mask covers the painted mark and its soft edge")
    if outline:
        grown = io.dilate(m, margin) if margin > 0 else m
        pts = _simplify(_trace(grown), simplify)
        res["outline"] = [[round(float(px) + x0, 1), round(float(py) + y0, 1)] for px, py in pts]
        res["outline_margin_px"] = margin
    return res


# ------------------------------------------------------------------ palette

def _kmeans(x, k, iters=12, seed=0):
    rng = np.random.default_rng(seed)
    c = x[rng.choice(len(x), size=min(k, len(x)), replace=False)].copy()
    for _ in range(iters):
        d = ((x[:, None, :] - c[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(len(c)):
            if (lab == j).any():
                c[j] = x[lab == j].mean(0)
    d = ((x[:, None, :] - c[None]) ** 2).sum(-1)
    return c, d.argmin(1)


def palette(image, points=None, box=None, poly=None, k=6):
    rgb, _ = io.load_rgb(image)
    H, W = rgb.shape[:2]
    if points:
        cols = []
        for x, y in points:
            x, y = int(round(x)), int(round(y))
            p = rgb[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 3).mean(0)
            cols.append([int(round(v * 255)) for v in p])
        return {"colours": cols, "note": "3x3 means at the points, in order"}
    x0, y0, x1, y1 = _box(rgb, box)
    sel = np.zeros((H, W), bool)
    sel[y0:y1, x0:x1] = True
    if poly:
        sel &= io.poly_mask((H, W), [(float(a), float(b)) for a, b in poly])
    px = rgb[sel]
    if len(px) == 0:
        raise ValueError("palette: the region is empty")
    if len(px) > 40000:
        px = px[np.random.default_rng(1).choice(len(px), 40000, replace=False)]
    c, lab = _kmeans(px.astype(np.float32), k)
    share = np.bincount(lab, minlength=len(c)) / float(len(lab))
    order = np.argsort(-share)
    return {"colours": [[int(round(v * 255)) for v in c[i]] for i in order],
            "shares": [round(float(share[i]), 3) for i in order],
            "note": "main colours of the region, most common first; split them into like / unlike yourself"}


# ------------------------------------------------------------------ inspect

def _hex(c):
    c = c.lstrip("#")
    return np.array([int(c[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255.0


def _load_layer(work, entry):
    with Image.open(os.path.join(work, entry["src"])) as im:
        return np.asarray(im.convert("RGBA"), np.float32) / 255.0


def _over(dst, rgba, x, y, blend):
    h, w = rgba.shape[:2]
    a = rgba[..., 3:]
    region = dst[y:y + h, x:x + w]
    if blend == "multiply":
        region[:] = region * (1 - a * (1 - rgba[..., :3]))
    else:
        region[:] = rgba[..., :3] * a + region * (1 - a)


def inspect(work, layer_id, out, grounds=("#3fa0a0", "#f0ede8", "#2b2b2f"), pieces=False, box=None, zoom=None):
    from scipy import ndimage as ndi
    with open(os.path.join(work, "layers.json")) as f:
        man = json.load(f)
    order = [l["id"] for l in man["layers"]]                 # back to front
    if layer_id not in order:
        raise SystemExit("inspect: no layer %r (have %s)" % (layer_id, ", ".join(order)))
    W, H = man["size"]
    entry = man["layers"][order.index(layer_id)]
    blend = entry.get("blend", "normal")
    rgba = _load_layer(work, entry)
    lx, ly, lw, lh = entry["rect"]
    picture = np.asarray(Image.open(os.path.join(work, "picture.png")).convert("RGB"), np.float32) / 255.0
    behind = np.zeros((H, W, 3), np.float32)
    for e in man["layers"][:order.index(layer_id)]:
        _over(behind, _load_layer(work, e), e["rect"][0], e["rect"][1], e.get("blend", "normal"))
    gcols = [_hex(g) for g in grounds]
    summary = {"layer": layer_id, "blend": blend, "rect": entry["rect"]}
    if not pieces:
        pad = int(max(16, 0.12 * max(lw, lh)))
        x0, y0, x1, y1 = box or [lx - pad, ly - pad, lx + lw + pad, ly + lh + pad]
        x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(W, int(x1)), min(H, int(y1))
        panels = [picture[y0:y1, x0:x1]]
        for g in gcols:
            canvas = np.ones((H, W, 3), np.float32) * g
            _over(canvas, rgba, lx, ly, blend)
            panels.append(canvas[y0:y1, x0:x1])
        panels.append(behind[y0:y1, x0:x1])
        names = ["original"] + ["on %s" % g for g in grounds] + ["what it uncovers"]
        gap = np.ones((y1 - y0, 6, 3), np.float32)
        row = np.concatenate(sum([[p, gap] for p in panels], [])[:-1], axis=1)
        im = Image.fromarray(io.to_u8(row))
        z = zoom or max(1.0, min(4.0, 1700.0 / im.width))
        if abs(z - 1) > 0.01:
            im = im.resize((int(im.width * z), int(im.height * z)), Image.NEAREST if z >= 2 else Image.LANCZOS)
        d = ImageDraw.Draw(im)
        xcur = 0
        for p, name in zip(panels, names):
            io.label(d, (int(xcur * z) + 4, 4), name, size=13)
            xcur += p.shape[1] + 6
        im.save(out, quality=92)
        summary.update({"out": out, "box": [x0, y0, x1, y1], "panels": names})
        return summary
    lab, n = ndi.label(ndi.binary_dilation(rgba[..., 3] > 0.1, iterations=3))
    tiles, info = [], []
    for i, sl in enumerate(ndi.find_objects(lab)):
        sub = rgba[sl].copy()
        sub[..., 3] *= (lab[sl] == i + 1)
        if (sub[..., 3] > 0.3).sum() < 8:
            continue
        core = sub[..., 3] > 0.6
        info.append({"piece": len(info), "home": [int(sl[1].start + lx), int(sl[0].start + ly)],
                     "size": [int(sl[1].stop - sl[1].start), int(sl[0].stop - sl[0].start)],
                     "core_colour": [int(round(v * 255)) for v in sub[..., :3][core].mean(0)] if core.any() else None,
                     "coverage_max": round(float(sub[..., 3].max()), 2)})
        tiles.append(sub)
    if not tiles:
        raise SystemExit("inspect: layer %r has no pieces" % layer_id)
    z = zoom or 4
    th = max(t.shape[0] for t in tiles) * z + 22
    rows = []
    for g in gcols:
        cells = []
        for k, sub in enumerate(tiles):
            h, w = sub.shape[:2]
            canvas = np.ones((h, w, 3), np.float32) * g
            _over(canvas, sub, 0, 0, blend)
            t = Image.fromarray(io.to_u8(canvas)).resize((w * z, h * z), Image.NEAREST)
            cell = Image.new("RGB", (max(t.width, 40) + 8, th), tuple(int(v * 255) for v in g))
            cell.paste(t, (4, 18))
            io.label(ImageDraw.Draw(cell), (2, 2), str(k), size=11)
            cells.append(cell)
        rows.append(cells)
    Wd = max(sum(c.width for c in r) for r in rows)
    sheet = Image.new("RGB", (min(Wd, 4000), th * len(rows)), (30, 30, 30))
    for j, r in enumerate(rows):
        xcur = 0
        for c in r:
            sheet.paste(c, (xcur, j * th))
            xcur += c.width
    sheet.save(out, quality=92)
    summary.update({"out": out, "pieces": info, "grounds": list(grounds),
                    "note": "each piece alone on each ground: only the object, whole, carrying none of its old ground"})
    return summary
