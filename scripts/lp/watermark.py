"""Remove a platform watermark from a picture before animating it, and restore the painting underneath.

Handles the common share-platform mark (for example Xiaohongshu/rednote): a translucent white pill with knocked-out
glyphs in the lower right, and a light "ID" line below it, right-aligned with it. Found automatically; every other
pixel of the picture is left exactly as it was.

Under the pill the painting is still there, lightened: observed = (1 - a) * paint + a * white. The opacity a comes
from seam continuity (averaged round the whole rim, the paint just inside equals the paint just outside), and
inverting it brings back the painting's light and dark. Inversion multiplies the picture's JPEG damage by 1/(1-a), so
the light is kept at the scale it survives at, colour is bridged in from round the pill (JPEG keeps colour at half
resolution and the white swamps it), and the glyph strokes, the rim and the ID line are repainted from the painting
around them. On busy, high-contrast grounds where the amplified damage would show, the pill is repainted from a ring
of the surrounding paint instead (chosen automatically from the ground's mid-frequency texture; thresholds measured on
real marks). The repaired areas finally get back the grain of the painting round them.

It is a repair, not a recovery of hidden truth: look at the review sheet (before | after | what was repainted) at
the display size and at 4x before animating.
"""
import json

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

from . import inpaint

W3 = np.array([0.299, 0.587, 0.114], np.float32)
BUSY = 0.14        # ring mid-frequency std x 1/(1-a): untint below (measured 0.02-0.09), repaint above (0.22-0.35)


def _gauss(a, s):
    return ndi.gaussian_filter(a, sigma=(s, s) + (0,) * (a.ndim - 2), mode="nearest", truncate=3.0)


def _bridge(img, target, known, sigmas=(12.0, 6.0, 3.0, 1.5, 0.8)):
    """Fill thin bands from the known pixels on both sides: multiscale normalised convolution, finest scale wins."""
    out = img.copy()
    ys, xs = np.nonzero(target)
    if not len(ys):
        return out
    pad = int(3 * sigmas[0]) + 2
    y0, y1 = max(0, ys.min() - pad), min(img.shape[0], ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(img.shape[1], xs.max() + pad + 1)
    im, t = img[y0:y1, x0:x1], target[y0:y1, x0:x1]
    k = (known[y0:y1, x0:x1] & ~t).astype(np.float32)
    est = np.broadcast_to((im * k[..., None]).sum((0, 1)) / max(1.0, k.sum()), im.shape).copy()
    for s in sigmas:
        den = _gauss(k, s)
        num = _gauss(im * k[..., None], s)
        w = np.clip((den - 0.02) / 0.23, 0, 1)[..., None]
        est = est * (1 - w) + (num / np.maximum(den, 1e-6)[..., None]) * w
    region = out[y0:y1, x0:x1]
    region[t] = est[t]
    return out


def _add_grain(out, orig, target, donors, block=6, seed=7):
    """Give repaired areas the grain of the paint round them: fine detail (original minus a light blur) copied from
    random blocks of untouched paint, outliers clipped. Seeded, so runs repeat exactly."""
    if not target.any():
        return out
    hp = orig - _gauss(orig, 1.2)
    if donors.any():
        sd = 1.4826 * np.median(np.abs(hp[donors]), axis=0) + 1e-4
        hp = np.clip(hp, -3 * sd, 3 * sd)
    ok = ndi.binary_erosion(donors, structure=np.ones((block, block)), border_value=0)
    dy, dx = np.nonzero(ok)
    if len(dy) == 0:
        return out
    rng = np.random.default_rng(seed)
    ys, xs = np.nonzero(target)
    res = out.copy()
    h2 = block // 2
    for by in range(ys.min(), ys.max() + 1, block):
        for bx in range(xs.min(), xs.max() + 1, block):
            t = target[by:by + block, bx:bx + block]
            if not t.any():
                continue
            i = rng.integers(len(dy))
            src = hp[dy[i] - h2:dy[i] - h2 + t.shape[0], dx[i] - h2:dx[i] - h2 + t.shape[1]]
            if src.shape[:2] != t.shape:
                continue
            region = res[by:by + block, bx:bx + block]
            region[t] = np.clip(region[t] + src[t], 0, 1)
    return res


def _stadium(shape, rect, grow=0.0):
    x0, y0, x1, y1 = rect
    r = (y1 - y0) / 2.0
    m = Image.new("L", (shape[1], shape[0]), 0)
    ImageDraw.Draw(m).rounded_rectangle([x0 - grow, y0 - grow, x1 - 1 + grow, y1 - 1 + grow],
                                        radius=max(0.0, r + grow), fill=255)
    return np.asarray(m) > 127


# ------------------------------------------------------------------ finding the mark

def _text_strokes(sub, size=7):
    """Thin, light, near-neutral strokes that are among the brightest things nearby (platform text is near white).
    size must exceed twice the stroke width (larger type, larger size)."""
    L = sub @ W3
    S = sub.max(-1) - sub.min(-1)
    top = L - ndi.median_filter(L, size=int(size) | 1)
    return (top > 0.05) & (S < 0.22) & (L > max(0.55, float(np.percentile(L, 97)) - 0.14))


def _refine_pill(img, rect, reach=5):
    """Snap a rough pill box to the pill's crisp edges: straight top and bottom (light below / above), round ends.
    Brightness thresholds merge with light paint next to the pill; its rendered edge does not. An edge moves only
    where a clear step is found."""
    H, W = img.shape[:2]
    L = img @ W3
    x0, y0, x1, y1 = [int(v) for v in rect]
    for _ in range(1):
        h = max(8, y1 - y0)
        cx = np.arange(max(0, x0 + h // 2 + 2), min(W, x1 - h // 2 - 2))
        if len(cx) < 3:
            break
        def edge_row(y, sign):
            if not (1 <= y < H):
                return -9.0
            return float(np.median((L[y, cx] - L[y - 1, cx]) * sign))
        top = max(range(max(1, y0 - reach), min(H, y0 + reach + 1)), key=lambda y: edge_row(y, 1))
        bot = max(range(max(1, y1 - reach), min(H, y1 + reach + 1)), key=lambda y: edge_row(y, -1))
        if edge_row(top, 1) < 0.04:
            top = y0
        if edge_row(bot, -1) < 0.04:
            bot = y1
        if bot - top < 8:
            break
        y0, y1 = top, bot
        r = (y1 - y0) / 2.0
        yc = (y0 + y1) / 2.0
        ang = np.linspace(-np.pi / 2 + 0.25, np.pi / 2 - 0.25, 15)
        def end_score(xc, side):
            # radial step across the half circle: inside (closer to the centre) is lighter
            xi = np.clip(np.round(xc + side * (r - 1.5) * np.cos(ang)).astype(int), 0, W - 1)
            xo = np.clip(np.round(xc + side * (r + 1.5) * np.cos(ang)).astype(int), 0, W - 1)
            yy = np.clip(np.round(yc + r * np.sin(ang)).astype(int), 0, H - 1)
            return float(np.median(L[yy, xi] - L[yy, xo]))
        left = max(range(x0 - reach, x0 + reach + 1), key=lambda x: end_score(x + r, -1))
        right = max(range(x1 - reach, x1 + reach + 1), key=lambda x: end_score(x - r, 1))
        if end_score(left + r, -1) < 0.04:
            left = x0
        if end_score(right - r, 1) < 0.04:
            right = x1
        if right - left < 1.2 * (y1 - y0):
            break
        x0, x1 = left, right
    return [max(0, x0), max(0, y0), min(W, x1), min(H, y1)]


def _verify_pill(img, search):
    """Refine a candidate and check it is a pill with knocked-out glyphs: (rect, stadium fit, glyph share) or None."""
    try:
        rect, g, b = _find_pill(img, search)
    except ValueError:
        return None
    rect = _refine_pill(img, rect)
    x0, y0, x1, y1 = rect
    h, w = y1 - y0, x1 - x0
    if h < 10 or not 1.6 <= w / max(1, h) <= 3.8 or b - g < 0.04:
        return None
    sub = img[y0:y1, x0:x1] @ W3
    shape = _stadium(img.shape[:2], rect)[y0:y1, x0:x1]
    level = float(np.percentile(sub[shape], 90))
    light = sub > g + 0.5 * (level - g)
    fit = float((light & shape).sum() / max(1, (light | shape).sum()))   # how well the light area fills a stadium
    core = _stadium(img.shape[:2], rect, grow=-max(2.0, h / 6.0))[y0:y1, x0:x1]
    glyphs = float((core & ~light).sum() / max(1, core.sum()))           # knocked-out strokes in the middle
    if fit < 0.55 or not 0.03 <= glyphs <= 0.5:
        return None
    return rect, fit, glyphs


def _line_under(img, rect):
    """Is there an ID text line directly under this pill, ending at its right edge? Looks in a tight window only, for
    the brightest thin neutral strokes (the text is near white), finds their row band and walks left from the right
    end across letter and word gaps. Returns (box, letters) or None."""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = rect
    ph = y1 - y0
    wx0, wy0 = max(0, int(x1 - 15 * ph)), min(H - 1, int(y1 + 0.1 * ph))
    wx1, wy1 = min(W, int(x1 + 0.5 * ph)), min(H, int(y1 + 1.7 * ph))
    if wy1 - wy0 < 6 or wx1 - wx0 < 4 * ph:
        return None
    sub = img[wy0:wy1, wx0:wx1]
    strokes = _text_strokes(sub, size=max(7, 0.35 * ph))
    prof = strokes[:, max(0, int(x1 - wx0 - 6 * ph)):].sum(1).astype(float)    # rows, in the part next to the edge
    if prof.max() < 3:
        return None
    on = prof >= 0.25 * prof.max()
    lab, n = ndi.label(on)
    sums = ndi.sum(prof, lab, index=np.arange(1, n + 1))
    band = np.nonzero(lab == int(np.argmax(sums)) + 1)[0]
    by0, by1 = max(0, int(band.min()) - 2), min(strokes.shape[0], int(band.max()) + 3)
    bh = int(band.max()) + 1 - int(band.min())
    if not 0.3 * ph <= bh <= 0.95 * ph:
        return None
    # Letters: stroke fragments joined vertically, letter-sized (paint beside the text is not letter-shaped).
    joined = ndi.binary_closing(strokes[by0:by1], structure=np.ones((max(2, int(0.4 * bh)), 1)))
    l2, _ = ndi.label(joined)
    letters = sorted([s for s in ndi.find_objects(l2) if s is not None
                      and 0.4 * bh <= s[0].stop - s[0].start <= 1.35 * bh and s[1].stop - s[1].start <= 1.6 * bh],
                     key=lambda s: s[1].start)
    if len(letters) < 8:
        return None
    end_letter = max(letters, key=lambda s: s[1].stop)
    if abs(end_letter[1].stop + wx0 - x1) > max(6, 0.35 * ph):
        return None
    chain = [end_letter]
    for s in sorted([s for s in letters if s[1].stop <= end_letter[1].start + 1], key=lambda s: -s[1].stop):
        if chain[-1][1].start - s[1].stop <= 1.6 * bh:      # letter and word gaps (a colon is not a letter), not wider
            chain.append(s)
    start, end = min(s[1].start for s in chain), end_letter[1].stop
    if end - start < 3.5 * ph or len(chain) < 8:
        return None
    # It must read as type: letters standing on one baseline, in near-neutral light strokes.
    bottoms = np.array([s[0].stop for s in chain], float)
    on_baseline = float(np.mean(np.abs(bottoms - np.median(bottoms)) <= max(2.0, 0.2 * bh)))
    piece = strokes[by0:by1, start:end]
    sp = sub[by0:by1, start:end][piece]
    neutral = float(np.mean(sp.max(-1) - sp.min(-1))) if len(sp) else 1.0
    if on_baseline < 0.6 or neutral > 0.13:
        return None
    return [start + wx0, by0 + wy0, end + wx0, by1 + wy0], int(len(chain))


def detect(img):
    """Find the share-platform mark: pill candidates by shape (light inside compared with a ring round it, stadiums
    of several sizes, in the lower right), each verified as a pill with knocked-out glyphs AND confirmed by an ID
    text line directly under it, right-aligned. Both are required, so neither a light patch of paint nor a light
    signature is taken for a watermark. Returns (pill_search, line_search, info) or None."""
    from scipy.signal import fftconvolve
    H, W = img.shape[:2]
    x0, y0 = int(W * 0.40), int(H * 0.60)
    sub = img[y0:, x0:]
    L = sub @ W3
    S = sub.max(-1) - sub.min(-1)
    cands = []
    for h in np.unique(np.round(np.geomspace(max(10, 0.018 * H), max(12, 0.085 * H), 9)).astype(int)):
        for asp in (1.9, 2.25, 2.6):
            w = int(round(h * asp))
            if h + 10 >= L.shape[0] or w + 10 >= L.shape[1]:
                continue
            k = (h + 10, w + 10)
            inner = np.asarray(_stadium(k, [5, 5, 5 + w, 5 + h], grow=-1.0), np.float32)
            ring = np.asarray(_stadium(k, [5, 5, 5 + w, 5 + h], grow=4.0), np.float32) - \
                np.asarray(_stadium(k, [5, 5, 5 + w, 5 + h], grow=1.0), np.float32)
            inner /= inner.sum()
            ring /= max(1.0, ring.sum())
            score = (fftconvolve(L, inner[::-1, ::-1], mode="same") - fftconvolve(L, ring[::-1, ::-1], mode="same")
                     - 2.0 * np.maximum(0, fftconvolve(S, inner[::-1, ::-1], mode="same") - 0.12))
            peaks = (score == ndi.maximum_filter(score, size=(h, w))) & (score > 0.03)
            for py, px in zip(*np.nonzero(peaks)):
                cands.append((float(score[py, px]), int(px - w // 2) + x0, int(py - h // 2) + y0, w, h))
    cands.sort(key=lambda c: -c[0])
    # Also anchor on text: a long line of light strokes in the bottom strip ending near the right edge gives a pill
    # search box above its right end. Shape and text candidates go through the same two checks.
    boxes = []
    yb, xb = int(H * 0.78), int(W * 0.30)
    strokes = _text_strokes(img[yb:, xb:])
    for d in (5, 9, 13):
        lab, _ = ndi.label(ndi.binary_dilation(strokes, structure=np.ones((3, d))))
        for sl in ndi.find_objects(lab):
            if sl is None:
                continue
            h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
            if 5 <= h <= max(12, 0.07 * H) and w >= 5 * h and sl[1].stop + xb >= 0.88 * W:
                lx1, ly0 = sl[1].stop + xb, sl[0].start + yb
                boxes.append([max(0, lx1 - 8 * h), max(0, ly0 - 5 * h), min(W, lx1 + 2 * h), max(1, ly0)])
    seen = []
    for c in [(None, b) for b in boxes] + [(sc, [max(0, cx - 8), max(0, cy - 8), min(W, cx + w + 8), min(H, cy + h + 8)])
                                           for sc, cx, cy, w, h in cands[:200]]:
        sc, search = c
        key = ((search[0] + search[2]) / 2, (search[1] + search[3]) / 2)
        if any(abs(key[0] - a) < 6 and abs(key[1] - b) < 6 for a, b in seen):
            continue
        seen.append(key)
        v = _verify_pill(img, search)
        if v is None:
            continue
        rect, fit, glyphs = v
        lu = _line_under(img, rect)
        if lu is None:
            continue
        lbox, letters = lu
        lh = lbox[3] - lbox[1]
        pill = [max(0, rect[0] - 8), max(0, rect[1] - 8), min(W, rect[2] + 8), min(H, rect[3] + 8)]
        m = max(2, lh // 4)                    # the detected line itself, with room for anti-aliasing
        line = [max(0, lbox[0] - m), max(0, lbox[1] - 1), min(W, lbox[2] + m), min(H, lbox[3] + 2)]
        return pill, line, {"pill_box": rect, "stadium_fit": round(fit, 3), "glyph_share": round(glyphs, 3),
                            "line_box": lbox, "letters": letters, "found_by": "text" if sc is None else "shape"}
    return None


def _find_pill(img, search):
    x0, y0, x1, y1 = search
    L = img[y0:y1, x0:x1] @ W3
    ring = np.ones(L.shape, bool)
    ring[3:-3, 3:-3] = False
    g, b = float(np.median(L[ring])), float(np.percentile(L, 95))
    bright = ndi.binary_closing(L > g + 0.5 * (b - g), structure=np.ones((3, 3)), iterations=3)
    lab, n = ndi.label(bright)
    if n == 0:
        raise ValueError("no pill in %s" % (search,))
    sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    blob = ndi.binary_fill_holes(lab == (int(np.argmax(sizes)) + 1))
    hh = np.nonzero(blob.any(1))[0]
    k = max(2, int((hh.max() - hh.min() + 1) // 6))
    blob = ndi.binary_opening(blob, structure=np.hypot(*np.mgrid[-k:k + 1, -k:k + 1]) <= k)   # drop touching streaks
    ys, xs = np.nonzero(blob)
    if not len(ys):
        raise ValueError("no pill in %s" % (search,))
    return [int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0 + 1), int(ys.max() + y0 + 1)], g, b


def _find_line(img, search, right_edge=None, exact=False):
    """The ID line's strokes and their soft shadow. exact=True: the box is the detected line itself; take its strokes
    as they are. Otherwise the box is a loose search area: chain the strokes into lines and pick the line that ends
    under the pill's right edge (or the heaviest)."""
    x0, y0, x1, y1 = search
    sub = img[y0:y1, x0:x1]
    L = sub @ W3
    med = ndi.median_filter(L, size=9)
    strokes = _text_strokes(sub, size=max(7, 0.7 * (y1 - y0)) if exact else 7)
    if exact:
        ys, xs = np.nonzero(strokes)
        if not len(ys):
            raise ValueError("no text strokes in %s" % (search,))
        full = np.zeros(img.shape[:2], bool)
        full[y0:y1, x0:x1] = strokes
        shadow = np.zeros(img.shape[:2], bool)
        shadow[y0:y1, x0:x1] = (ndi.distance_transform_edt(~strokes) <= 2.5) & ~strokes & ((med - L) > 0.02)
        return [int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0 + 1), int(ys.max() + y0 + 1)], full, shadow
    # letter height from the strokes' own pieces (not the row span, which a tall light object would inflate)
    lab0, n0 = ndi.label(strokes)
    heights = [s[0].stop - s[0].start for s in ndi.find_objects(lab0) if s is not None and (s[0].stop - s[0].start) >= 3]
    hgt = max(6, int(np.median(heights))) if heights else 10
    # glyphs of one line join across letter and word gaps, not across wider gaps (a cuff, a highlight beside the text)
    chain = ndi.binary_dilation(strokes, structure=np.ones((3, max(5, int(0.9 * hgt)))))
    lab, n = ndi.label(chain)
    if n == 0:
        raise ValueError("no text line in %s" % (search,))
    weights = ndi.sum(strokes, lab, index=np.arange(1, n + 1))
    pick = int(np.argmax(weights)) + 1
    if right_edge is not None:                 # the ID line ends under the pill's right edge
        ends = ndi.maximum(np.broadcast_to(np.arange(L.shape[1]), L.shape), lab, index=np.arange(1, n + 1)) + x0
        ok = [i for i in range(n) if abs(ends[i] - right_edge) <= max(8, hgt)]
        if ok:
            pick = max(ok, key=lambda i: weights[i]) + 1
    strokes &= lab == pick
    ys, xs = np.nonzero(strokes)
    bb = [int(xs.min() + x0), int(ys.min() + y0), int(xs.max() + x0 + 1), int(ys.max() + y0 + 1)]
    full = np.zeros(img.shape[:2], bool)
    full[y0:y1, x0:x1] = strokes
    shadow = np.zeros(img.shape[:2], bool)
    shadow[y0:y1, x0:x1] = (ndi.distance_transform_edt(~strokes) <= 2.5) & ~strokes & ((med - L) > 0.02)
    return bb, full, shadow


# ------------------------------------------------------------------ removing it

def remove(img, pill_search=None, line_search=None, mode="auto", right_edge=None, line_exact=False):
    """img: float RGB 0..1. Returns (restored image, repaired-pixel mask, report)."""
    if mode not in ("auto", "untint", "fill", "hybrid"):
        raise ValueError("mode must be auto, untint, fill or hybrid")
    H, W = img.shape[:2]
    out = img.copy()
    hole = np.zeros((H, W), bool)
    keepout = np.zeros((H, W), bool)
    grain = np.zeros((H, W), bool)
    changed = np.zeros((H, W), bool)          # everything this repair touches, for the report and review sheet
    rep = {}
    if pill_search:
        rect, g, b = _find_pill(img, pill_search)
        inside = _stadium((H, W), rect, grow=-1.0)
        rim = _stadium((H, W), rect, grow=3.0) & ~_stadium((H, W), rect, grow=-2.0)   # JPEG halo of the bright edge
        LI = img @ W3
        hgt = rect[3] - rect[1]

        def knockouts(alpha):
            # nearer to the body level (lightened) or to the paint under it (knocked out)?
            w = max(2, hgt // 8)
            level = ndi.grey_dilation(np.where(inside, LI, 0), size=(2 * w + 1, 2 * w + 1))
            k = inside & (LI < 0.5 * (level + (level - alpha) / (1 - alpha)))
            lab, n = ndi.label(k)
            if n:
                sizes = ndi.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
                k &= ~np.isin(lab, np.nonzero(sizes < 3)[0] + 1)
            return k

        # Opacity from seam continuity: the paint 4-8 px inside the rim equals the paint 4-8 px outside, on average;
        # averaging cancels texture, the band skips the rim's JPEG ringing, pixels near glyphs are left out. (Fitting
        # the overlay's grey as well is biased by paint differences between paired pixels; the overlay is white.)
        alpha = float(np.clip((b - g) / max(1e-3, 1 - g), 0.2, 0.9))
        dist_in = ndi.distance_transform_edt(_stadium((H, W), rect))
        dist_out = ndi.distance_transform_edt(~_stadium((H, W), rect))
        outer = (dist_out >= 4) & (dist_out <= 8)
        pairs = 0
        for _ in range(3):
            near = ndi.binary_dilation(knockouts(alpha), iterations=3)
            inner = (dist_in >= 4) & (dist_in <= 8) & ~near
            pairs = int(min(inner.sum(), outer.sum()))
            if pairs < 30:
                break
            mi, mo = img[inner].mean(0), img[outer].mean(0)
            alpha = float(np.clip((mi - mo).sum() / max(1e-3, (1 - mo).sum()), 0.15, 0.95))
        ko = knockouts(alpha)
        core_ko = ndi.binary_erosion(ko, iterations=1)
        edge = ndi.binary_dilation(ko, iterations=2) & ~core_ko
        body = inside & ~edge & ~core_ko & ~rim
        pill3 = _stadium((H, W), rect, grow=3.0)
        ring = _stadium((H, W), rect, grow=15) & ~_stadium((H, W), rect, grow=5)
        busy = float((LI - _gauss(LI, 4.0))[ring].std() / (1 - alpha))
        chosen = mode
        if mode == "auto":               # (with few seam pairs, a small pill, the opacity comes from the levels)
            chosen = "untint" if busy <= BUSY else "fill"
        if chosen in ("untint", "hybrid"):
            U = np.clip((img - alpha) / (1 - alpha), 0, 1)
            sig = max(0.8, hgt / 30.0)
            wgt = _gauss(body.astype(np.float32), sig)
            Um = ndi.median_filter(U, size=(3, 3, 1))
            U_lo = _gauss(np.where(body[..., None], Um, 0), sig) / np.maximum(wgt, 1e-6)[..., None]
            tau = 2.5 * (1.2 / 255.0) / (1 - alpha)           # finer detail kept only where it clears amplified noise
            detail = Um - U_lo
            U_lo = np.clip(U_lo + np.sign(detail) * np.maximum(np.abs(detail) - tau, 0), 0, 1)
        if chosen == "untint":
            # light from the inversion, colour bridged in from round the pill; glyphs, rim and edges bridged over
            Y = U_lo @ W3
            lum = img @ W3
            chroma = np.stack([img[..., 2] - lum, img[..., 0] - lum, np.zeros((H, W), np.float32)], -1)
            chroma = _bridge(chroma, pill3, ~pill3, sigmas=(16.0, 8.0, 4.0, 2.0))
            R, B = Y + chroma[..., 1], Y + chroma[..., 0]
            G = (Y - 0.299 * R - 0.114 * B) / 0.587
            rec = np.clip(np.stack([R, G, B], -1), 0, 1)
            out[body] = rec[body]
            bridge = (rim | edge | core_ko) & pill3
            out = _bridge(out, bridge, ~bridge)
            grain |= bridge
            changed |= pill3
            keepout |= _stadium((H, W), rect, grow=3.5)
        elif chosen == "hybrid":
            # texture from the paint round the pill, broad light and dark from the inversion
            hole_p = pill3 & ~core_ko
            src = (_stadium((H, W), rect, grow=43.5) & ~_stadium((H, W), rect, grow=3.5))
            F = inpaint.fill(out, hole_p, source=src, patch=7, quality=3)
            sigL = max(1.5, hgt / 6.0)
            wB = _gauss(body.astype(np.float32), sigL)
            LUlo = _gauss(np.where(body, U_lo @ W3, 0), sigL) / np.maximum(wB, 1e-6)
            F2 = np.clip(F + ((LUlo - _gauss(F @ W3, sigL)) * np.clip((wB - 0.15) / 0.35, 0, 1))[..., None], 0, 1)
            out[hole_p] = F2[hole_p]
            changed |= hole_p
            keepout |= _stadium((H, W), rect, grow=3.5)
        else:
            # repaint from a ring right round the pill (far donors bring unrelated masses); the hole covers the
            # bright edge's JPEG halo
            hole_p = _stadium((H, W), rect, grow=4.5)
            donors = _stadium((H, W), rect, grow=17.0) & ~_stadium((H, W), rect, grow=5.0)
            F = inpaint.fill(out, hole_p, source=donors, patch=5, quality=3)
            out[hole_p] = F[hole_p]
            grain |= hole_p
            changed |= hole_p
            keepout |= _stadium((H, W), rect, grow=5.0)
        rep["pill"] = {"rect": rect, "opacity": round(alpha, 3), "knockout_px": int(ko.sum()), "mode": chosen,
                       "ground_busyness": round(busy, 3), "seam_pairs": pairs}
    line_near = None
    if line_search:
        if right_edge is None and "pill" in rep:
            right_edge = rep["pill"]["rect"][2]
        bb, strokes, shadow = _find_line(out, line_search, right_edge, exact=line_exact)
        line_near = ndi.binary_dilation(strokes | shadow, iterations=4)
        hole |= ndi.binary_dilation(strokes | shadow, iterations=2 if (bb[3] - bb[1]) >= 18 else 1)
        rep["line"] = {"rect": bb, "stroke_px": int(strokes.sum())}
    if hole.any():
        ys, xs = np.nonzero(hole)
        win = np.zeros((H, W), bool)
        win[max(0, ys.min() - 40):ys.max() + 40, max(0, xs.min() - 40):xs.max() + 40] = True
        out = inpaint.fill(out, hole, source=win & ~ndi.binary_dilation(hole, iterations=2) & ~keepout,
                           patch=7, quality=3)
    if line_near is not None:
        # light, colourless specks left next to the text (stray anti-aliasing), never the painting's own lights
        x0, y0, x1, y1 = rep["line"]["rect"]
        box = np.zeros((H, W), bool)
        box[max(0, y0 - 5):min(H, y1 + 6), max(0, x0 - 6):min(W, x1 + 6)] = True
        L = out @ W3
        speck = box & line_near & ((L - ndi.median_filter(L, size=7)) > 0.06) & ((out.max(-1) - out.min(-1)) < 0.2)
        if speck.any():
            speck = ndi.binary_dilation(speck, iterations=1)
            out = inpaint.fill(out, speck, source=~ndi.binary_dilation(speck | hole, iterations=2) & ~keepout,
                               patch=5, quality=3)
            hole |= speck
    grain |= hole
    if grain.any():
        ys, xs = np.nonzero(grain)
        win = np.zeros((H, W), bool)
        win[max(0, ys.min() - 30):ys.max() + 30, max(0, xs.min() - 30):xs.max() + 30] = True
        out = _add_grain(out, img, grain, win & ~ndi.binary_dilation(grain | keepout, iterations=3))
        hole |= grain
    changed |= hole
    rep["repaired_px"] = int(changed.sum())
    return out, changed, rep


def review_sheet(before, after, hole, rep, path):
    """before | after | what was repaired (magenta), enlarged to about 1800 px wide."""
    rects = [v["rect"] for v in rep.values() if isinstance(v, dict) and "rect" in v]
    if not rects:
        return
    x0 = max(0, min(r[0] for r in rects) - 16)
    y0 = max(0, min(r[1] for r in rects) - 16)
    x1 = min(before.shape[1], max(r[2] for r in rects) + 16)
    y1 = min(before.shape[0], max(r[3] for r in rects) + 16)
    a, b = before[y0:y1, x0:x1], after[y0:y1, x0:x1]
    m = np.where(hole[y0:y1, x0:x1, None], np.array([1.0, 0.0, 1.0]), b * 0.55)
    gap = np.ones((y1 - y0, 4, 3))
    row = np.concatenate([a, gap, b, gap, m], 1)
    z = max(1, min(5, 1800 // max(1, row.shape[1])))
    im = Image.fromarray((np.clip(row, 0, 1) * 255).astype(np.uint8))
    im.resize((im.width * z, im.height * z), Image.NEAREST).save(path, quality=92)


def run(src, dst, review=None, pill=None, line=None, mode="auto"):
    """Command-line entry: find (or take) the boxes, remove, write dst (PNG) and a review sheet; return a report."""
    from . import imgops as io
    pix, _ = io.load_image(src)
    img = pix[..., :3].astype(np.float32)
    info, exact = None, False
    if pill is None and line is None:
        found = detect(img)
        if found is None:
            return {"found": False, "written": None,
                    "note": "no platform watermark found in the lower right; nothing written. Pass --pill/--line boxes to force."}
        pill, line, info = found
        exact = True
    out, hole, rep = remove(img, pill, line, mode, line_exact=exact)
    Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8)).save(dst)
    if review:
        review_sheet(img, out, hole, rep, review)
    unchanged = int((np.abs(out - img).max(-1)[~hole] > 0).sum())
    rep.update({"found": True, "detected": info, "written": dst, "review": review,
                "pixels_changed_outside_repair": unchanged,
                "note": "check the review sheet at 4x and the result at display size before animating"})
    return json.loads(json.dumps(rep))
