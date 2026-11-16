"""Build layers from a plan: select, lift, fill what was hidden, review.

Layers are peeled front to back. Each peel takes the current picture, lifts
one thing off it, paints in what that thing was hiding, and hands the result
to the next peel. Foreground colour is solved against independently owned
alpha and background. Reconstruction error is reported, never hidden by
writing foreground paint back into the background or inflating alpha.
"""
import hashlib
import json
import os

import numpy as np
from PIL import Image, ImageDraw

from . import imgops as io
from . import inpaint, segment


CUT_VERSION = 16     # ownership-preserving matte and immutable fill
_CONTEXT = {"plan_dir": None}     # set by build(), so mask parts resolve their image beside the plan


def _pts(p, s):
    return [(float(x) * s, float(y) * s) for x, y in p]


def _samples(items, s):
    """Colour samples: [x, y] points are scaled, [r, g, b] colours pass through."""
    return [list(it) if len(it) == 3 else [it[0] * s, it[1] * s] for it in items]


def _seed_mask(shape, items, s, width):
    m = np.zeros(shape[:2], dtype=bool)
    for it in items or []:
        if it and isinstance(it[0], (list, tuple)):
            m |= io.stroke_mask(shape, _pts(it, s), max(2.0, width * s))
        else:
            m |= io.stroke_mask(shape, _pts([it], s), max(2.0, width * s))
    return m


def _region(shape, part, s):
    """The rough area a part describes, as a bool mask (or None)."""
    if "poly" in part:
        return io.poly_mask(shape, _pts(part["poly"], s))
    if "ellipse" in part:
        e = part["ellipse"]
        r = e["r"] if isinstance(e["r"], (list, tuple)) else [e["r"], e["r"]]
        return io.ellipse_mask(shape, (e["c"][0] * s, e["c"][1] * s), (r[0] * s, r[1] * s))
    if "rect" in part:
        x0, y0, x1, y1 = [v * s for v in part["rect"]]
        m = np.zeros(shape[:2], dtype=bool)
        m[int(y0):int(round(y1)), int(x0):int(round(x1))] = True
        return m
    if "stroke" in part:
        return io.stroke_mask(shape, _pts(part["stroke"], s), part.get("width", 10) * s)
    return None


def _avoid_mask(shape, names, built, planned, scale):
    """Resolve the exact donor exclusion consumed by the patch-fill branch."""
    mask = np.zeros(shape[:2], dtype=bool)
    for name in names:
        previous = [b for b in built if b["id"] == name]
        if previous:
            for b in previous:
                mask |= io.dilate(b["alpha"] > 0.05, 3)
        else:
            for other in planned:
                if other["id"] == name:
                    region = np.zeros(shape[:2], dtype=bool)
                    for part in other.get("parts", []):
                        selected = _region(shape, part, scale)
                        if selected is not None:
                            region |= selected
                    mask |= io.dilate(region, 4)
    return mask


def part_alpha(rgb, part, s, default_feather):
    """Alpha (float 0..1) for one part of a layer."""
    shape = rgb.shape
    mode = part.get("mode", "snap")
    region = _region(shape, part, s)
    feather = part.get("feather", default_feather)
    if mode == "wand":
        hard = segment.wand(rgb, _pts(part["wand"], s), tol=part.get("tol", 0.08), limit=region,
                            contiguous=part.get("contiguous", True))
        return segment.soften(rgb, hard, feather * s) if feather > 0 else hard.astype(np.float32)
    if mode == "mask":
        # A selection made elsewhere (your own code, another tool): a grayscale
        # image at the working picture's size, white = this layer.
        path = part.get("image")
        if not isinstance(path, str) or not path:
            raise ValueError("mask parts need an image path")
        if not os.path.isabs(path):
            path = os.path.join(_CONTEXT.get("plan_dir") or ".", path)
        with Image.open(path) as im:
            m = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
        if m.shape != rgb.shape[:2]:
            raise ValueError("mask %s is %dx%d; the working picture is %dx%d (draw masks on work/picture.png)"
                             % (part["image"], m.shape[1], m.shape[0], rgb.shape[1], rgb.shape[0]))
        return m if region is None else np.where(region, m, 0).astype(np.float32)
    if region is None:
        raise ValueError("part needs poly, ellipse, rect or stroke: %r" % (part,))
    if mode == "level":
        # Coverage from value alone: a pale figure on a dark ground (or the
        # reverse) with a crisp edge. The region only bounds where to look.
        tone, lo, hi = part.get("tone", "light"), part.get("lo"), part.get("hi")
        if tone not in ("light", "dark"):
            raise ValueError("level parts need tone 'light' or 'dark'")
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (lo, hi)) or not 0 <= lo < hi <= 255):
            raise ValueError("level parts need 0 <= lo < hi <= 255 (luma)")
        a = io.smoothstep(lo, hi, io.luma(rgb) * 255.0)
        if tone == "dark":
            a = 1.0 - a
        return np.where(region, a, 0).astype(np.float32)
    if mode in ("key", "matte"):
        if "like" in part:
            a = segment.key_color(rgb, region, _samples(part["like"], s),
                                  _samples(part["unlike"], s) if part.get("unlike") else None,
                                  tol=part.get("tol", 0.10), soft=part.get("soft", 0.08), cap=part.get("cap"))
        else:
            a = segment.key_tone(rgb, region, tone=part.get("tone", "dark"),
                                 radius=max(2, int(round(part.get("radius", 10) * s))),
                                 gain=part.get("gain", 1.0), floor=part.get("floor", 0.04))
        if part.get("solid"):       # fill pinholes so a solid object is not see-through
            a = np.maximum(a, io.gaussian(a, part["solid"] * s) * 1.6).clip(0, 1)
        if mode == "matte":
            if part.get("structure"):
                structure = part["structure"]
                if not isinstance(structure,dict):
                    raise ValueError("matte.structure needs tone/radius/gain/floor settings")
                tonal = segment.key_tone(rgb,region,
                    tone=structure.get("tone","dark"),
                    radius=max(2,int(round(structure.get("radius",10)*s))),
                    gain=structure.get("gain",1),floor=structure.get("floor",0.02))
                a = np.maximum(a,tonal)
            if part.get("background"):
                # Explicitly declaring the observed background palette is
                # stronger than a relative like/unlike classifier. A dark
                # leaf far from BOTH palettes must not become a paper hole
                # merely because it is slightly nearer the unlike colours.
                background = segment.key_color(rgb, region,
                    _samples(part["background"],s),
                    tol=part.get("background_tol",0.07),
                    soft=part.get("background_soft",0.05))
                shade = (segment.shade_match(rgb,_samples(part["like"],s),
                         tolerance=part.get("shade_tol",0.20)) if part.get("like") else 1)
                a = np.maximum(a,np.where(region,(1-background)*shade,0))
            keep = _seed_mask(shape, part.get("keep"), s, part.get("seed_width", 2))
            drop = _seed_mask(shape, part.get("drop"), s, part.get("seed_width", 2))
            a = segment.matte(rgb, region, a, keep=keep, drop=drop,
                              radius=part.get("edge", 4.0)*s,
                              residual=part.get("residual", 0.035))
        return a
    if mode == "exact":
        m = region.astype(np.float32)
        return io.gaussian(m, feather * s * 0.7) if feather > 0 else m
    if mode == "snap":
        keep = _seed_mask(shape, part.get("keep"), s, part.get("seed_width", 6))
        drop = _seed_mask(shape, part.get("drop"), s, part.get("seed_width", 6))
        tol = part.get("tol")
        hard = segment.snap(rgb, region, keep if keep.any() else None, drop if drop.any() else None,
                            tol=(tol * s if tol else None), gamma=part.get("smooth", 30.0),
                            prior=part.get("prior", 0.6))
        return segment.soften(rgb, hard, feather * s) if feather > 0 else hard.astype(np.float32)
    raise ValueError("unknown mode %r" % mode)


def layer_alpha(rgb, spec, s):
    feather = spec.get("feather", 1.0)
    a = np.zeros(rgb.shape[:2], dtype=np.float32)
    for part in spec.get("parts", []):
        a = np.maximum(a, part_alpha(rgb, part, s, feather))
    for part in spec.get("minus", []):
        a = a * (1.0 - part_alpha(rgb, part, s, feather))
    grow = spec.get("grow", 0)
    if grow:
        hard = a > 0.5
        hard = io.dilate(hard, grow * s) if grow > 0 else io.erode(hard, -grow * s)
        a = segment.soften(rgb, hard, feather * s) if feather > 0 else hard.astype(np.float32)
    min_area = spec.get("min_area", 24)
    if min_area:
        from scipy import ndimage as ndi
        lab, n = ndi.label(a > 0.25)
        if n > 1:
            sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
            small = np.isin(lab, np.nonzero(sizes < min_area * s * s)[0] + 1)
            near = io.dilate(a > 0.25, 2) & ~io.dilate((a > 0.25) & ~small, 2)
            a = np.where(small | (near & (a < 0.25) & small), 0.0, a)
    halo = spec.get("halo")
    if halo:
        # Watercolour wet edge: a thin soft rim round the solid paint, so the
        # pale bloom travels with the mark instead of staying behind as a ghost.
        hw = halo.get("width", 2.5) if isinstance(halo, dict) else float(halo)
        lv = halo.get("level", 0.5) if isinstance(halo, dict) else 0.5
        if hw <= 0 or not 0 < lv <= 1:
            raise ValueError("halo needs width > 0 and 0 < level <= 1")
        from scipy import ndimage as ndi
        dist = ndi.distance_transform_edt(~(a > 0.5))
        a = np.maximum(a, lv * np.clip(1 - dist / (hw * s), 0, 1) ** 1.5).astype(np.float32)
    contour = spec.get("contour")
    if contour:
        # The dark line hugging an outline belongs to the object. Kept inside
        # the parts' own regions, so it never reaches a neighbour of like tone.
        if isinstance(contour, bool) or not isinstance(contour, (int, float)) or contour <= 0:
            raise ValueError("contour is a width in px")
        rim = io.gaussian(io.dilate(a > 0.5, contour * s).astype(np.float32), 0.6 * s)
        regions = [_region(rgb.shape, part, s) for part in spec.get("parts", [])]
        if regions and all(r is not None for r in regions):
            rim = np.where(np.logical_or.reduce(regions), rim, 0)
        a = np.maximum(a, rim).astype(np.float32)
    # Explicit background ownership wins after all growing/softening.
    for part in spec.get("protect", []):
        m = _region(rgb.shape, part, s)
        if m is None:
            raise ValueError("protect entries need a geometric region")
        a[m] = 0
    return np.clip(a, 0, 1).astype(np.float32)


def _foreground_prior(P, alpha):
    """Propagate observed opaque paint, retaining opaque-source brush detail."""
    from scipy import ndimage as ndi
    core = alpha >= 0.98
    if not core.any():
        core = alpha >= 0.75
    if not core.any():
        return P.copy()
    near = ndi.distance_transform_edt(~core, return_distances=False, return_indices=True)
    prior = P[near[0],near[1]].copy()
    prior[core] = P[core]
    return prior


def solve_on_plate(P, alpha0, B, hole, confidence=0.0, regularization=0.04):
    """Decontaminate foreground with independently owned alpha and clean plate.

    Neither plate mismatch nor rest residual can grow alpha or write source
    paint back into the plate. Untrusted fill uses an observed foreground prior
    at soft edges. A verified plate can opt into confidence=1 (unregularized),
    but alpha still stays fixed; impossible colours clip. Remaining residual
    is reported honestly, not hidden in a static ghost or opaque fringe.
    """
    if not 0 <= confidence <= 1 or regularization < 0:
        raise ValueError("plate confidence must be 0..1; regularization must be nonnegative")
    alpha = np.where(hole, alpha0, 0).clip(0,1).astype(np.float32)
    if np.array_equal(P,B):
        return P.astype(np.float32).copy(), P.astype(np.float32).copy(), alpha
    a = alpha[...,None]
    prior = _foreground_prior(P, alpha)
    # When the fill already equals observed paint, F=P is a feasible split.
    # Fade the prior continuously there; an equality special case would make
    # infinitesimal plate noise produce discontinuous foreground fringes.
    disagreement = np.abs(P-B).max(-1)[...,None]
    weight = regularization*(1-confidence)*(1-a)**2*io.smoothstep(0,0.02,disagreement)
    den = a*a+weight
    colour = np.divide(a*(P-(1-a)*B)+weight*prior,den,
                       out=prior.copy(),where=den>1e-9).clip(0,1)
    colour = np.where((a>=0.999) | (a<=0), P, colour)
    plate = np.where(hole[...,None], B, P)
    return colour.astype(np.float32), plate.astype(np.float32), alpha


def _surface_map(spec, shape, s):
    """Plan-level background surfaces: [{"id", poly|rect|ellipse|stroke, "texture"}], later entries win.

    Label 0 is "rest": everything no entry claims. Returns (labels, names, textures)
    or (None, None, None) when the plan declares none.
    """
    if not spec:
        return None, None, None
    labels = np.zeros(shape[:2], np.int16)
    names, textures = ["rest"], ["smooth"]
    for i, entry in enumerate(spec):
        sid = entry.get("id")
        if not isinstance(sid, str) or not sid or sid in names:
            raise ValueError("surfaces need unique string ids (and 'rest' is reserved)")
        region = _region(shape, entry, s)
        if region is None:
            raise ValueError("surface %s needs poly, rect, ellipse or stroke" % sid)
        tex = entry.get("texture", "smooth")
        if tex not in ("smooth", "patch"):
            raise ValueError("surface %s texture must be 'smooth' or 'patch'" % sid)
        names.append(sid)
        textures.append(tex)
        labels[region] = len(names) - 1
    return labels, names, textures


def _gauss(a, sigma):
    from scipy import ndimage as ndi
    return ndi.gaussian_filter(a, sigma=(sigma, sigma) + (0,) * (a.ndim - 2), mode="nearest", truncate=3.0)


def _smooth_from(img, target, donors, s, lines="drop"):
    """Continue a surface into `target` from its own donors: multiscale normalised
    convolution, the finest scale with enough nearby paint decides."""
    from scipy import ndimage as ndi
    sigmas = [v * s for v in (60, 30, 15, 8, 4, 2)]
    x0, y0, x1, y1 = io.bbox(target, pad=int(3 * sigmas[0]) + 4, shape=target.shape)
    sl = (slice(y0, y1), slice(x0, x1))
    im, m, d = img[sl], target[sl], donors[sl].copy()
    if lines == "drop":                  # thin dark lines and specks are not the surface's own colour
        med = ndi.median_filter(im, size=(7, 7, 1))
        d &= np.abs(im - med).max(-1) < 0.12
    d = d.astype(np.float32)
    out = img.copy()
    if d.sum() < 1:
        return out
    est = np.broadcast_to((im * d[..., None]).sum((0, 1)) / d.sum(), im.shape).copy()
    for sg in sigmas:
        den = _gauss(d, sg)
        num = _gauss(im * d[..., None], sg)
        w = io.smoothstep(0.02, 0.25, den)[..., None]
        est = est * (1 - w) + (num / np.maximum(den, 1e-6)[..., None]) * w
    region = out[sl]
    region[m] = est[m]
    return out


def _fill_surfaces(plate, plate_alpha, alpha, hole, labels, names, textures, fill_spec, s, avoid_mask, log, lid):
    """Repaint each hidden pixel from the visible paint of its own surface.

    rim 'repaint' (default) repaints everything the layer covers: right for a
    layer that leaves its place. rim 'keep' repaints only the solid paint and
    keeps the observed pixels under the soft rim: right for a layer that only
    shifts a little (rest stays exact, lines under the rim survive). `as`
    repaints the whole hole as another surface (round an opening, parallax
    uncovers more of the interior). The layer's own coverage is never a donor.
    """
    from scipy import ndimage as ndi
    rim = fill_spec.get("rim", "repaint")
    if rim not in ("repaint", "keep"):
        raise ValueError("fill.rim must be 'repaint' or 'keep'")
    if rim == "keep":
        level = fill_spec.get("rim_level", 0.5)
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 < level < 1:
            raise ValueError("fill.rim_level must be in (0, 1)")
        area = hole & io.dilate(alpha > level, 0.5 * s)
    else:
        area = hole.copy()
    own = io.dilate(alpha > 0.02, max(1.0, fill_spec.get("spread", 3)) * s)
    donors = ~area & ~own & (plate_alpha > 0.01)
    if avoid_mask is not None:
        donors &= ~avoid_mask
    lab = labels
    if fill_spec.get("as") is not None:
        if fill_spec["as"] not in names:
            raise ValueError("fill.as %r is not a declared surface (have %s)" % (fill_spec["as"], ", ".join(names)))
        lab = np.where(area, names.index(fill_spec["as"]), labels)
    reach = fill_spec.get("reach")
    work = area & (ndi.distance_transform_edt(area) <= reach * s) if reach else area
    out, stats = plate.copy(), {"rim": rim, "repainted_pixels": int(work.sum())}
    tex_override = fill_spec.get("texture")
    if tex_override not in (None, "smooth", "patch"):
        raise ValueError("fill.texture must be 'smooth' or 'patch'")
    for k in np.unique(lab[work]):
        m = work & (lab == k)
        d = donors & (lab == k)
        name, tex = names[k], tex_override or textures[k]
        borrowed = int(d.sum()) < 20
        if borrowed:
            d = donors
            log("  %-14s surface %r shows no paint of its own here; borrowing all surfaces - check the map" % (lid, name))
        if tex == "patch":
            filled = inpaint.fill(plate, m, source=d, patch=fill_spec.get("patch", 7), quality=fill_spec.get("quality", 2),
                                  search=(fill_spec["search"] * s if fill_spec.get("search") else None))
        else:
            filled = _smooth_from(plate, m, d, s, fill_spec.get("lines", "drop"))
        out[m] = filled[m]
        stats[name] = {"pixels": int(m.sum()), "donor_pixels": int(d.sum()), "texture": tex, "borrowed": bool(borrowed)}
    return out, stats


def _external_identity(spec, plan_dir):
    """Mask parts read files: their pixels, not their names, decide whether a cache is valid."""
    ids = []
    for group in ("parts", "minus", "protect"):
        for part in spec.get(group, []):
            if part.get("mode") == "mask" and part.get("image"):
                path = part["image"] if os.path.isabs(part["image"]) else os.path.join(plan_dir, part["image"])
                with open(path, "rb") as f:
                    ids.append(hashlib.sha256(f.read()).hexdigest())
    return ids


def _save_surfaces_review(orig, labels, names, path):
    rng = np.random.default_rng(5)
    cols = rng.uniform(0.15, 1.0, (len(names), 3)).astype(np.float32)
    cols[0] = 0.5
    ov = Image.fromarray(io.to_u8(orig * 0.45 + cols[labels] * 0.55))
    d = ImageDraw.Draw(ov)
    H, W = labels.shape
    for k, name in enumerate(names):
        ys, xs = np.nonzero(labels == k)
        if len(xs):
            io.label(d, (int(np.median(xs)), int(np.median(ys))), name, size=max(12, W // 90))
    _save_fit(ov, path, 1568)


def _rest_clusters(comp, orig, path, limit=12):
    """Where the stacked layers differ from the picture at rest: a heat map and the largest clusters."""
    from scipy import ndimage as ndi
    d = np.abs(comp - orig).max(-1)
    Image.fromarray((np.clip(d * 4, 0, 1) * 255).astype(np.uint8)).save(path)
    lab, n = ndi.label(d > 0.1)
    if n == 0:
        return []
    objs = ndi.find_objects(lab)
    sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    out = []
    for i in np.argsort(-sizes)[:limit]:
        sl = objs[i]
        out.append({"box": [int(sl[1].start), int(sl[0].start), int(sl[1].stop), int(sl[0].stop)],
                    "pixels": int(sizes[i]), "max": round(float(d[sl].max()), 3)})
    return out


def _paint_from_reference(rects, picture, s):
    """The paint's own colour, read where the same paint lies thick enough to hide its ground.

    Each rect is drawn round such a mark with a margin of bare ground. The
    border median is that ground; the paint is the mean of the pixels farthest
    from it. It is only as good as the author's choice of a thick, clean mark.
    """
    P2s, B2s, ws = [], [], []
    H, W = picture.shape[:2]
    for r in rects:
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            raise ValueError("pigment.reference entries are [x0, y0, x1, y1] rects")
        x0, y0, x1, y1 = [int(round(v * s)) for v in r]
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if x1 - x0 < 6 or y1 - y0 < 6:
            raise ValueError("pigment.reference rect %r is too small" % (r,))
        box = picture[y0:y1, x0:x1]
        frame = np.ones(box.shape[:2], bool); frame[2:-2, 2:-2] = False
        b2 = np.median(box[frame], axis=0)
        d2 = np.abs(box - b2).max(-1)
        sel2 = d2 >= np.quantile(d2, 0.85)
        P2s.append(box[sel2].mean(0)); B2s.append(b2); ws.append(sel2.sum())
    w = np.array(ws, float) / sum(ws)
    P2, B2 = (np.array(P2s) * w[:, None]).sum(0), (np.array(B2s) * w[:, None]).sum(0)
    spread = float(np.abs(np.array(P2s) - P2).max()) if len(P2s) > 1 else 0.0
    u8 = lambda v: [int(round(c * 255)) for c in v]
    return P2.astype(np.float32), {"paint": u8(P2), "reference_ground": u8(B2), "rects": len(P2s),
                                   "spread_between_rects": round(spread * 255, 1),
                                   "meaning": "a large spread means the reference marks disagree about the paint"}


def solve_layer(P, alpha0, B, hole, confidence=0.0, regularization=0.04):
    """Procedural fills obey the same ownership contract as supplied plates."""
    return solve_on_plate(P,alpha0,B,hole,confidence,regularization)


def reveal_contract(spec, scale=1.0):
    """Check an AUTHOR-DECLARED conservative envelope, not actual motion.

    Bounds are absolute source-pixel displacements per axis. All three causes
    must be stated, including [0,0] where absent. A later scene/view change must
    re-author/recheck these values; no scene is inspected by this cut tool.
    """
    r = spec.get("reveal")
    if r is None:
        return {"status": "not_declared", "motion_verified": False}
    if not isinstance(r, dict):
        raise ValueError("reveal must declare local, parent and camera bounds")
    components = {}
    for name in ("local", "parent", "camera"):
        value = r.get(name)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("reveal.%s needs two nonnegative source-pixel bounds" % name)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not np.isfinite(v) or v < 0 for v in value):
            raise ValueError("reveal.%s needs finite nonnegative bounds" % name)
        components[name] = [float(v)*scale for v in value]
    sampling = r.get("sampling", 1)
    if isinstance(sampling, bool) or not isinstance(sampling, (int,float)) or not np.isfinite(sampling) or sampling < 0:
        raise ValueError("reveal.sampling must be finite and nonnegative")
    axes = np.sum(list(components.values()),axis=0)
    required = float(np.hypot(*axes)+sampling*scale)
    fill = spec.get("fill") or {}
    reach = fill.get("reach")
    if reach is not None and (not isinstance(reach,(int,float)) or isinstance(reach,bool) or not np.isfinite(reach) or reach<0):
        raise ValueError("fill.reach must be finite and nonnegative")
    # Supplied plates cover the whole consumed hole; a limited procedural fill
    # must cover this conservative inward reveal band, including resampling.
    if fill.get("method", "patch") not in ("plate", "smooth", "none") and reach is not None and reach*scale+1e-6 < required:
        raise ValueError("fill.reach is smaller than declared joint reveal budget (%.3f working px)" % required)
    return {"status":"declared_only", "components_working_px":components,
            "sampling_working_px":float(sampling*scale), "required_band_working_px":required,
            "motion_verified":False, "meaning":"bounds are supplied, not measured from the runtime"}


def _hash(*parts):
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, np.ndarray):
            h.update(np.ascontiguousarray(p).tobytes())
        else:
            h.update(json.dumps(p, sort_keys=True).encode("utf-8"))
    return h.hexdigest()[:20]


def split_coverage(source_alpha, selection):
    """Distribute existing coverage without turning transparent pixels opaque.

    RGB solving uses selection s: C = F*s + B*(1-s). Source-over then needs
    foreground alpha A*s and background alpha A*(1-s)/(1-A*s). For an
    opaque, fully selected pixel the hidden coverage is underdetermined;
    use the nearest unselected coverage (opaque for opaque source images). This
    preserves the rest image; it cannot establish what an unseen background
    actually contains.
    """
    front = source_alpha * selection
    den = 1 - front
    hidden_coverage = np.ones_like(source_alpha)
    if (source_alpha < 1).any():
        region = io.dilate(selection > 0.02, 3)
        if region.all():
            hidden_coverage[:] = 0
        else:
            from scipy import ndimage as ndi
            near = ndi.distance_transform_edt(region, return_distances=False, return_indices=True)
            hidden_coverage = source_alpha[near[0], near[1]].copy()
    behind = np.divide(source_alpha * (1-selection), den,
                       out=hidden_coverage, where=den > 1e-7)
    behind = np.where(source_alpha <= 0, 0, behind)
    return front.astype(np.float32), np.clip(behind, 0, 1).astype(np.float32)


def _rest_metrics(colour_premult, alpha, source_rgb, source_alpha):
    delta = np.maximum(np.abs(colour_premult-source_rgb*source_alpha[..., None]).max(-1),
                       np.abs(alpha-source_alpha))
    return {"max": float(delta.max()), "mean": float(delta.mean()),
            "space": "premultiplied sRGB plus alpha, normalized 0..1"}


def encoded_rest_metrics(manifest, work, source_rgb, source_alpha):
    """Measure the files actually saved, independently of float cut results."""
    H, W = source_alpha.shape
    colour, alpha = np.zeros((H,W,3), np.float32), np.zeros((H,W), np.float32)
    for layer in manifest["layers"]:
        with Image.open(os.path.join(work,layer["src"])) as im:
            rgba = np.asarray(im.convert("RGBA"), dtype=np.float32)/255
        x,y,w,h = layer["rect"]
        if rgba.shape[:2] != (h,w):
            raise ValueError("layer file dimensions do not match its rect: %s" % layer["id"])
        a = rgba[...,3]
        if layer.get("blend") == "multiply":
            colour[y:y+h,x:x+w] = colour[y:y+h,x:x+w]*(1-a[...,None]*(1-rgba[...,:3]))
            continue
        colour[y:y+h,x:x+w] = rgba[...,:3]*a[...,None] + colour[y:y+h,x:x+w]*(1-a[...,None])
        alpha[y:y+h,x:x+w] = a + alpha[y:y+h,x:x+w]*(1-a)
    return _rest_metrics(colour,alpha,source_rgb,source_alpha)


def build(plan, plan_dir, log=print):
    """Run a layer plan. Returns the manifest dict (also written to disk)."""
    src = plan["image"]
    if not os.path.isabs(src):
        src = os.path.normpath(os.path.join(plan_dir, src))
    out = plan.get("out", "work")
    if not os.path.isabs(out):
        out = os.path.normpath(os.path.join(plan_dir, out))
    os.makedirs(os.path.join(out, "layers"), exist_ok=True)
    os.makedirs(os.path.join(out, "cache"), exist_ok=True)
    os.makedirs(os.path.join(out, "review"), exist_ok=True)
    _CONTEXT["plan_dir"] = plan_dir

    # One normalizing open gives both the full size after orientation and the pixels
    # (a HEIC or RAW decodes through ImageIO there, not in Pillow, and only once)
    normalized = io.normalized_image(src)
    full = normalized.size
    source_pixels, s = io.load_image(normalized, plan.get("max_side", 1600))
    orig = source_pixels[..., :3]
    orig_alpha = source_pixels[..., 3] if source_pixels.shape[-1] == 4 else np.ones(orig.shape[:2],np.float32)
    H, W = orig.shape[:2]
    log("picture %dx%d, working at %dx%d (scale %.4f)" % (full[0], full[1], W, H, s))
    io.save_image(source_pixels, os.path.join(out,"picture.png"))

    fmt = plan.get("format", "png")
    if fmt not in ("png", "webp"):
        raise ValueError("cut layers require PNG or WebP to retain selection alpha")
    plate = orig.copy()
    plate_alpha = orig_alpha.copy()
    manifest = {"source_size": [full[0], full[1]], "size": [W, H], "scale": s,
                "normalization": "EXIF orientation, embedded ICC to sRGB, source alpha retained",
                "source_has_transparency": bool((orig_alpha<1).any()), "cut_version": CUT_VERSION, "layers": []}
    built = []
    surf_labels, surf_names, surf_tex = _surface_map(plan.get("surfaces"), (H, W), s)
    if surf_labels is not None:
        _save_surfaces_review(orig, surf_labels, surf_names, os.path.join(out, "review", "surfaces.jpg"))
        manifest["surfaces"] = surf_names[1:]
    for spec in plan["layers"]:
        lid = spec["id"]
        fill_spec = dict(spec.get("fill") or {})
        envelope = reveal_contract(spec, s)
        supplied_plate = None
        plate_identity = None
        plate_stats = None
        if fill_spec.get("method") == "plate":
            plate_path = fill_spec.get("image")
            if not isinstance(plate_path, str) or not plate_path:
                raise ValueError("fill.method=plate requires fill.image")
            if not os.path.isabs(plate_path):
                plate_path = os.path.normpath(os.path.join(plan_dir, plate_path))
            provided, _ = io.load_image(plate_path)
            if provided.shape[:2] != (H, W):
                raise ValueError("supplied plate must match normalized working dimensions; no implicit resize")
            if (plate_alpha < 1).any() or (provided.shape[-1] == 4 and (provided[...,3] < 1).any()):
                raise ValueError("supplied plate mode currently requires an opaque source and opaque plate")
            supplied_plate = provided[...,:3]
            plate_identity = {"image":fill_spec["image"], "normalized_rgb_sha256":hashlib.sha256(io.to_u8(supplied_plate).tobytes()).hexdigest(),
                              "ownership":"author-supplied background for this peel; semantic correctness not inferred"}
        # Motion evidence changes do not invalidate an unchanged cut. A supplied
        # plate's pixels DO invalidate it even when its filename is unchanged.
        # Selection and nearest-neighbour fill can inspect the whole picture;
        # no fixed margin proves the dependency bound for arbitrary settings.
        # Fingerprint exact current inputs; store the actual solved extent.
        cut_spec = {k:v for k,v in spec.items() if k != "reveal"}
        avoid_mask = None
        avoid_identity = None
        if fill_spec.get("method", "patch") not in ("plate", "none"):
            if fill_spec.get("avoid"):
                avoid_mask = _avoid_mask((H,W),fill_spec["avoid"],built,plan["layers"],s)
                avoid_identity = _hash(avoid_mask)
        elif "fill" in cut_spec and isinstance(cut_spec["fill"],dict):
            # These paths do not read avoid, so neither changed references nor
            # the ignored avoid list should invalidate their asset cache.
            cut_spec["fill"] = {k:v for k,v in cut_spec["fill"].items() if k != "avoid"}
        key = _hash(cut_spec, plate, plate_alpha, plate_identity, avoid_identity, s, [W,H], CUT_VERSION,
                    _external_identity(spec, plan_dir),
                    plan.get("surfaces") if fill_spec.get("method") == "surfaces" else None)
        cache = os.path.join(out, "cache", "%s-%s.npz" % (lid, key))
        if os.path.exists(cache):
            alpha = np.zeros((H, W), dtype=np.float32)
            hole = np.zeros((H,W),dtype=bool)
            fg = plate.copy()
            new_plate = plate.copy()
            with np.load(cache) as z:
                rx0,ry0,rx1,ry1 = [int(v) for v in z["rect"]]
                alpha[ry0:ry1,rx0:rx1] = z["alpha"]
                hole[ry0:ry1,rx0:rx1] = z["hole"]
                fg[ry0:ry1,rx0:rx1] = z["fg"]
                new_plate[ry0:ry1,rx0:rx1] = z["plate"]
                plate_stats = json.loads(str(z["plate_stats"]))
            log("  %-14s cached" % lid)
        else:
            import time as _time
            _t0 = _time.time()
            alpha = layer_alpha(plate, spec, s)
            alpha = np.where(plate_alpha > 0, alpha, 0).astype(np.float32)
            protect = np.zeros((H,W),bool)
            for part in spec.get("protect", []):
                protect |= _region((H,W),part,s)
            hole = io.dilate(alpha > 0.004, max(0.0, fill_spec.get("spread", 3) * s)) & ~protect
            method = fill_spec.get("method", "patch")
            surf_stats = None
            if supplied_plate is not None:
                new_plate = np.where(hole[...,None], supplied_plate, plate)
            elif method == "none" or not hole.any():
                new_plate = plate.copy()        # nothing repainted: the layer is lifted as it is and the plate keeps the picture
            elif method == "smooth":
                source = plate_alpha > 0.01
                if fill_spec.get("from"):
                    specified_source = np.zeros((H,W),bool)
                    for polygon in fill_spec["from"]:
                        specified_source |= io.poly_mask((H,W),_pts(polygon,s))
                    source &= specified_source
                if avoid_mask is not None:
                    source &= ~avoid_mask
                new_plate = inpaint.diffuse_fill(plate, hole, source=source)
            elif method == "surfaces":
                if surf_labels is None:
                    raise ValueError("fill.method 'surfaces' needs a plan-level 'surfaces' map")
                new_plate, surf_stats = _fill_surfaces(plate, plate_alpha, alpha, hole, surf_labels, surf_names,
                                                       surf_tex, fill_spec, s, avoid_mask, log, lid)
            elif method == "regions":
                # Each revealed surface owns a separate set of donors. A wall
                # patch cannot borrow a window, wire, plant or adjacent floor.
                covered = np.zeros((H,W),bool)
                new_plate = plate.copy()
                for region in fill_spec.get("regions", []):
                    target = np.zeros((H,W),bool)
                    for part in region.get("target", []):
                        target |= _region((H,W),part,s)
                    target &= hole
                    if (target & covered).any():
                        raise ValueError("fill.regions targets overlap inside the hole")
                    covered |= target
                    if not target.any():
                        continue
                    source = np.zeros((H,W),bool)
                    for polygon in region.get("from", []):
                        source |= io.poly_mask((H,W),_pts(polygon,s))
                    source &= ~hole & (plate_alpha > 0.01)
                    if avoid_mask is not None:
                        source &= ~avoid_mask
                    if not source.any():
                        raise ValueError("fill.regions needs unoccluded donors for each surface")
                    if region.get("method", "patch") == "smooth":
                        filled = inpaint.diffuse_fill(plate,target,source=source)
                    else:
                        filled = inpaint.fill(plate,target,source=source,
                            reach=fill_spec.get("reach",23)*s,
                            quality=fill_spec.get("quality",2),
                            patch=fill_spec.get("patch",7),
                            search=fill_spec.get("search",160)*s)
                    new_plate[target] = filled[target]
                if (hole & ~covered).any():
                    raise ValueError("fill.regions must assign every hole pixel a background surface")
            else:
                # Unpainted transparent pixels are not background-colour samples.
                source = plate_alpha > 0.01 if (plate_alpha < 1).any() else None
                if fill_spec.get("from"):
                    specified_source = np.zeros((H, W), dtype=bool)
                    for poly in fill_spec["from"]:
                        specified_source |= io.poly_mask((H, W), _pts(poly, s))
                    source = specified_source if source is None else source & specified_source
                if avoid_mask is not None:
                    source = (np.ones((H, W), bool) if source is None else source) & ~avoid_mask
                reach = fill_spec.get("reach")
                if source is not None and not (source & ~hole).any():
                    # No painted reference exists. Preserve hidden colour, never
                    # invent a white matte; coverage is distributed below.
                    new_plate = plate.copy()
                    log("  %-14s no painted fill reference; no colour invented" % lid)
                else:
                    new_plate = inpaint.fill(plate, hole, source=source,
                                             reach=(reach * s if reach else None),
                                             quality=fill_spec.get("quality", 2),
                                             patch=fill_spec.get("patch", 7),
                                             search=(fill_spec["search"] * s if fill_spec.get("search") else None))
            # Spread excludes contaminated edge pixels from donor sampling;
            # it does not grant ownership of visible background to the layer.
            # Keep observed background exact wherever no foreground covers it.
            new_plate = np.where((alpha > 0)[...,None],new_plate,plate)
            alpha0 = alpha.copy()
            estimated_plate = new_plate.copy()
            fg, new_plate, alpha = solve_on_plate(plate, alpha, new_plate, hole,
                confidence=fill_spec.get("confidence",0.0),
                regularization=fill_spec.get("regularization",0.04))
            added = alpha-alpha0
            reconstructed = fg*alpha[...,None]+new_plate*(1-alpha[...,None])
            residual = np.abs(reconstructed-plate).max(-1)
            plate_stats = {"policy":"immutable-alpha-and-plate-regularized-foreground",
                "hole_pixels":int(hole.sum()), "alpha_increase_max":float(added.max()),
                "alpha_increase_mean_in_hole":float(added[hole].mean()) if hole.any() else 0,
                "alpha_increase_pixels":int((added>1/255).sum()),
                "support_added_pixels":int(((alpha0<=0.004)&(alpha>0.004)).sum()),
                "plate_rgb_error_inside_hole":float(np.abs(new_plate[hole]-estimated_plate[hole]).max()) if hole.any() else 0,
                "rest_residual_max":float(residual.max()),
                "rest_residual_mean_in_hole":float(residual[hole].mean()) if hole.any() else 0,
                "protected_pixels":int(protect.sum()),
                "plate_confidence":fill_spec.get("confidence",0.0),
                "visual_status":"unreviewed; inspect matte, exposed plate and displaced edge"}
            if surf_stats is not None:
                plate_stats["surfaces"] = surf_stats
            rx0,ry0,rx1,ry1 = _roi(alpha,hole,plate,new_plate)
            # Exact float intermediates keep warm and cold builds on the same
            # pipeline; 8-bit cache payloads introduced extra rounding stages.
            np.savez_compressed(cache, rect=[rx0,ry0,rx1,ry1],
                                alpha=alpha[ry0:ry1,rx0:rx1],hole=hole[ry0:ry1,rx0:rx1],
                                plate=new_plate[ry0:ry1,rx0:rx1],fg=fg[ry0:ry1,rx0:rx1],
                                plate_stats=json.dumps(plate_stats))
            log("  %-14s lifted (%d px, %.0f s)" % (lid, int((alpha > 0.5).sum()), _time.time() - _t0))
        if plate_stats is not None:
            # Prior masks may change while RGB/coverage stay identical. Reuse
            # the cut but recompute this context-dependent diagnostic each run.
            previous = np.zeros((H,W),bool)
            for b in built:
                previous |= b["alpha"] > 0.004
            plate_stats["overlap_previous_lifts_pixels"] = int((hole&previous).sum())
        blend, pigment_stats = "normal", None
        if spec.get("pigment") not in (None, False):     # true or an options object, {} included
            # A mark that will leave its ground must carry its paint, not the
            # mixture of paint and the ground it was laid on.
            pig = spec["pigment"] if isinstance(spec["pigment"], dict) else {}
            if (plate_alpha < 1).any():
                raise ValueError("pigment layers currently require an opaque source")
            model = pig.get("model", "glaze")
            if model == "glaze":
                # Transparent glaze: observed = ground x transmittance. Store the
                # transmittance (the paint on white paper) and composite with
                # multiply, so on any other ground it darkens it as a glaze would.
                floor = pig.get("floor", 0.03)
                grow = pig.get("grow", 1)
                if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 <= floor < 0.5:
                    raise ValueError("pigment.floor must be in [0, 0.5)")
                if isinstance(grow, bool) or not isinstance(grow, (int, float)) or grow < 0:
                    raise ValueError("pigment.grow must be a nonnegative number of px")
                support = io.dilate(alpha > 0.02, grow * s)
                ratio = plate / np.maximum(new_plate, 1e-3)
                absorbed = np.clip(1 - ratio, 0, 1)
                # a dead zone keeps the old ground's own texture (ratios near 1) out of the pigment
                absorbed = np.clip((absorbed - floor) / (1 - floor), 0, 1)
                fg = np.where(support[..., None], 1 - absorbed, 1).astype(np.float32)
                alpha = support.astype(np.float32)
                lighter = support & (ratio.max(-1) > 1 + 2.0 / 255)
                share = float(lighter.sum()) / max(1, int(support.sum()))
                blend = "multiply"
                pigment_stats = {"model": "glaze: observed = ground x transmittance; stored RGB is transmittance (paint on white)",
                                 "floor": float(floor), "grow_px": float(grow),
                                 "support_pixels": int(support.sum()),
                                 "lighter_than_ground_pixels": int(lighter.sum()),
                                 "lighter_share": share,
                                 "meaning": "a glaze can only darken its ground; lighter pixels are clipped to no paint. "
                                            "A large share means the paint has body: use model 'body'"}
                if share > 0.2:
                    log("  %-14s pigment: %.0f%% of the mark is lighter than its ground - not a pure glaze; "
                        "consider pigment.model 'body'" % (lid, 100 * share))
            elif model == "body":
                # Paint with body: observed = a*paint + (1-a)*ground. Given the
                # paint colour (where it lies thick enough to hide its ground),
                # each pixel's coverage a is how far it has moved from its ground
                # toward that colour. Without a paint colour, a constant coverage.
                paint, ref_stats, k = pig.get("paint"), None, pig.get("coverage")
                if paint is None and pig.get("reference"):
                    paint, ref_stats = _paint_from_reference(pig["reference"], orig, s)
                elif paint is not None:
                    if (not isinstance(paint, (list, tuple)) or len(paint) != 3 or
                            any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in paint)):
                        raise ValueError("pigment.paint must be an [r, g, b] colour (0..255)")
                    paint = np.clip(np.asarray(paint, np.float32) / 255.0, 0, 1)
                unseparable = np.zeros(alpha.shape, bool)
                if paint is not None:
                    dC = paint[None, None, :] - new_plate
                    n2 = (dC * dC).sum(-1)
                    unseparable = (alpha > 0.02) & (n2 < 0.03 ** 2)   # paint and ground too alike to tell apart
                    proj = np.clip(((plate - new_plate) * dC).sum(-1) / np.maximum(n2, 1e-6), 0, 1)
                    a_k = np.where(unseparable, 1.0, proj) * alpha
                elif k is not None:
                    if isinstance(k, bool) or not isinstance(k, (int, float)) or not 0.05 <= k <= 1:
                        raise ValueError("pigment.coverage must be in [0.05, 1]")
                    a_k = alpha * k
                else:
                    raise ValueError("pigment model 'body' needs paint, reference rects or coverage")
                a_k = np.clip(a_k, 0, 1).astype(np.float32)
                a3 = a_k[..., None]
                raw = np.divide(plate - (1 - a3) * new_plate, a3, out=plate.copy(), where=a3 > 1e-4)
                clipped = (a_k > 0.05) & ((raw < -2.0 / 255) | (raw > 1 + 2.0 / 255)).any(-1)
                fg = np.clip(raw, 0, 1)
                if paint is not None:          # thin coverage carries little evidence: lean on the paint colour there
                    w = io.smoothstep(0.05, 0.25, a_k)[..., None]
                    fg = fg * w + paint[None, None, :] * (1 - w)
                fg = fg.astype(np.float32)
                alpha = a_k
                supp = a_k > 0.02
                pigment_stats = {"model": "body: observed = coverage*paint + (1-coverage)*ground; normal compositing",
                                 "paint": None if paint is None else [int(round(v * 255)) for v in paint],
                                 "paint_from": "reference" if ref_stats else ("author" if paint is not None else None),
                                 "reference": ref_stats,
                                 "coverage": None if paint is not None else float(k),
                                 "coverage_mean": float(a_k[supp].mean()) if supp.any() else 0.0,
                                 "coverage_max": float(a_k.max()),
                                 "unseparable_pixels": int(unseparable.sum()),
                                 "clipped_pixels": int(clipped.sum()),
                                 "meaning": "clipped: the observed colour is off the ground-to-paint line there "
                                            "(texture, another paint, or a wrong plate); unseparable: paint and ground alike"}
            else:
                raise ValueError("pigment.model must be 'glaze' or 'body'")
        # Crop padding has no foreground coverage. Canonical hidden RGB keeps
        # cold/warm emitted PNGs identical, not merely equivalent over a matte.
        fg = np.where(alpha[...,None] > 0, fg, 0).astype(np.float32)
        render_alpha, next_alpha = split_coverage(plate_alpha, alpha)
        bb = io.bbox(render_alpha > 0.004, pad=3, shape=alpha.shape)
        if bb is None:
            log("  %-14s EMPTY - nothing selected, skipped" % lid)
            continue
        x0, y0, x1, y1 = bb
        rgba = np.concatenate([fg[y0:y1, x0:x1], render_alpha[y0:y1, x0:x1, None]], axis=-1)
        name = "layers/%s.%s" % (lid, fmt)
        io.save_image(rgba, os.path.join(out, name), quality=plan.get("quality", 92))
        built.append({"id": lid, "alpha": render_alpha, "before": plate, "after": new_plate, "fg": fg,
                      "before_alpha": plate_alpha, "after_alpha": next_alpha, "rect": [x0, y0, x1 - x0, y1 - y0],
                      "blend": blend})
        entry = {"id": lid, "src": name, "rect": [x0, y0, x1 - x0, y1 - y0],
                 "area": int((alpha > 0.5).sum()), "reveal":envelope,
                 "plate_source":plate_identity, "plate_solve":plate_stats,
                 "background_method":fill_spec.get("method", "patch"),
                 "moving_edge_status":"unreviewed"}
        if blend != "normal":
            entry["blend"] = blend
        if pigment_stats is not None:
            entry["pigment"] = pigment_stats
        manifest["layers"].append(entry)
        plate = new_plate
        plate_alpha = next_alpha

    base_name = "layers/%s.%s" % (plan.get("base", "base"), "png" if fmt == "png" else fmt)
    base_pixels = np.concatenate([plate,plate_alpha[...,None]],axis=-1) if (plate_alpha<1).any() else plate
    io.save_image(base_pixels, os.path.join(out, base_name), quality=plan.get("quality", 92))
    manifest["layers"].append({"id": plan.get("base", "base"), "src": base_name, "rect": [0, 0, W, H], "area": W * H})
    manifest["layers"].reverse()          # back to front, the order a renderer draws them

    # rest check: stack everything back and compare with the picture
    comp, comp_alpha = plate*plate_alpha[...,None], plate_alpha.copy()
    for b in reversed(built):
        a3 = b["alpha"][..., None]
        if b.get("blend") == "multiply":
            comp = comp * (1 - a3 * (1 - b["fg"]))
            continue
        comp = b["fg"] * a3 + comp * (1 - a3)
        comp_alpha = b["alpha"] + comp_alpha*(1-b["alpha"])
    manifest["rest_error"] = _rest_metrics(comp,comp_alpha,orig,orig_alpha)
    manifest["rest_clusters"] = _rest_clusters(comp, orig * orig_alpha[..., None],
                                               os.path.join(out, "review", "rest-residual.png"))
    manifest["encoded_rest_error"] = encoded_rest_metrics(manifest,out,orig,orig_alpha)
    log("float rest: max %.4f; saved-file rest: max %.4f (not a motion-quality check)" %
        (manifest["rest_error"]["max"],manifest["encoded_rest_error"]["max"]))

    with open(os.path.join(out, "layers.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    review(orig, built, plate, os.path.join(out, "review"), s, log,orig_alpha,plate_alpha)
    return manifest


def to_key(a):
    """Fingerprint of an image region, for caching."""
    return hashlib.sha256(io.to_u8(a).tobytes()).hexdigest()[:16]


def _roi(alpha, hole, before, after):
    """Actual output support; never infer it from a fixed expansion radius."""
    support = (alpha != 0) | hole | np.any(before != after,axis=-1)
    bounds = io.bbox(support)
    return tuple(bounds) if bounds is not None else (0,0,0,0)


# ---------------------------------------------------------------- review sheets

def review(orig, built, base, out_dir, s, log=print, orig_alpha=None, base_alpha=None):
    """Pictures for the agent to look at: is each cut right, is each fill clean?"""
    H, W = orig.shape[:2]
    backdrop = io.checker(H,W)
    def shown(rgb,alpha):
        return rgb if alpha is None else rgb*alpha[...,None]+backdrop*(1-alpha[...,None])
    # 1. overview: every layer's outline on the picture
    ov = Image.fromarray(io.to_u8(shown(orig,orig_alpha) * 0.75 + 0.25))
    d = ImageDraw.Draw(ov)
    palette = [(255, 60, 60), (40, 200, 255), (255, 210, 0), (120, 255, 90), (255, 110, 230),
               (255, 150, 40), (150, 130, 255), (0, 240, 190)]
    arr = np.asarray(ov).copy()
    for i, b in enumerate(built):
        edge = io.dilate(io.contour(b["alpha"] > 0.5), max(1.0, W / 900.0))
        arr[edge] = palette[i % len(palette)]
    ov = Image.fromarray(arr)
    d = ImageDraw.Draw(ov)
    for i, b in enumerate(built):
        x, y, w, h = b["rect"]
        io.label(d, (x + 4, y + 4), b["id"], size=max(13, W // 80), fg=(0, 0, 0), bg=palette[i % len(palette)])
    _save_fit(ov, os.path.join(out_dir, "overview.jpg"), 1568)

    # 2. per layer: outline on the picture | the lifted layer | what is left behind
    for b in built:
        x, y, w, h = b["rect"]
        pad = int(max(24, 0.2 * max(w, h)))
        x0, y0, x1, y1 = max(0, x - pad), max(0, y - pad), min(W, x + w + pad), min(H, y + h + pad)
        crop = shown(b["before"],b.get("before_alpha"))[y0:y1, x0:x1]
        a = b["alpha"][y0:y1, x0:x1]
        p1 = io.to_u8(crop).copy()
        edge = io.contour(a > 0.5)
        p1[io.dilate(edge, 1.2)] = (0, 0, 0)
        p1[edge] = (0, 255, 255)
        a3 = a[..., None]
        if b.get("blend") == "multiply":       # a glaze: show it darkening the checkerboard, as it will darken any ground
            p2 = io.to_u8(io.checker(y1 - y0, x1 - x0) * (1 - a3 * (1 - b["fg"][y0:y1, x0:x1])))
        else:
            p2 = io.to_u8(b["fg"][y0:y1, x0:x1] * a3 + io.checker(y1 - y0, x1 - x0) * (1 - a3))
        p3 = io.to_u8(shown(b["after"],b.get("after_alpha"))[y0:y1, x0:x1])
        gap = np.full((y1 - y0, 6, 3), 255, dtype=np.uint8)
        sheet = Image.fromarray(np.concatenate([p1, gap, p2, gap, p3], axis=1))
        scale = min(3.0, max(1.0, 520.0 / max(1, (y1 - y0))))
        if scale > 1.01:
            sheet = sheet.resize((int(sheet.width * scale), int(sheet.height * scale)), Image.LANCZOS)
        d = ImageDraw.Draw(sheet)
        io.label(d, (6, 6), "%s  | outline on picture | lifted layer | left behind (filled)" % b["id"], size=15)
        _save_fit(sheet, os.path.join(out_dir, "layer-%s.jpg" % b["id"]), 1800)
        # the same "left behind" at full size, to zoom into with `look --box`
        Image.fromarray(io.to_u8(shown(b["after"],b.get("after_alpha")))).save(os.path.join(out_dir, "plate-after-%s.jpg" % b["id"]), "JPEG", quality=92)
    _save_fit(Image.fromarray(io.to_u8(shown(base,base_alpha))), os.path.join(out_dir, "base.jpg"), 1568)
    log("review sheets in %s" % out_dir)


def _save_fit(im, path, max_side):
    if max(im.size) > max_side:
        k = max_side / float(max(im.size))
        im = im.resize((int(im.width * k), int(im.height * k)), Image.LANCZOS)
    im.convert("RGB").save(path, "JPEG", quality=88)
