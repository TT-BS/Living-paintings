"""Turn a rough outline into a precise mask.

The agent can see what a thing is and roughly where its outline runs, but it
cannot paint a pixel-accurate mask. These functions close that gap:

  snap()  - GrabCut-style graph cut started from a rough polygon
  key()   - soft matte for thin or wispy things (twigs, hair, lettering)
  wand()  - flood selection for flat-colour art
  soften()- edge-aware soft alpha from a hard mask
"""
import numpy as np
from scipy import ndimage as ndi
from scipy import sparse
from scipy.sparse import csgraph

from . import imgops as io


# ---------------------------------------------------------------- colour models

def _kmeans(x, k, rng, iters=8):
    n = len(x)
    if n <= k:
        return np.arange(n) % k
    cent = x[rng.choice(n, k, replace=False)].copy()
    lab = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        d = ((x[:, None, :] - cent[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        for j in range(k):
            sel = lab == j
            if sel.any():
                cent[j] = x[sel].mean(0)
            else:
                cent[j] = x[rng.integers(n)]
    return lab


class _GMM(object):
    def __init__(self, x, k, rng):
        if len(x) > 6000:
            x = x[rng.choice(len(x), 6000, replace=False)]
        k = max(1, min(k, len(x) // 8 or 1))
        lab = _kmeans(x, k, rng)
        self.w, self.mu, self.icov, self.logdet = [], [], [], []
        for j in range(k):
            pts = x[lab == j]
            if len(pts) < 4:
                continue
            mu = pts.mean(0)
            cov = np.cov((pts - mu).T) + np.eye(3) * 2e-4
            self.w.append(len(pts) / float(len(x)))
            self.mu.append(mu)
            self.icov.append(np.linalg.inv(cov))
            self.logdet.append(np.log(np.linalg.det(cov)))
        if not self.w:
            mu = x.mean(0)
            self.w, self.mu = [1.0], [mu]
            self.icov, self.logdet = [np.eye(3) / 0.01], [np.log(0.01 ** 3)]

    def nll(self, x):
        """Negative log likelihood per row of x (N x 3)."""
        best = None
        for w, mu, icov, ld in zip(self.w, self.mu, self.icov, self.logdet):
            d = x - mu
            m = np.einsum("ni,ij,nj->n", d, icov, d)
            ll = np.log(w) - 0.5 * ld - 0.5 * m
            best = ll if best is None else np.logaddexp(best, ll)
        return -best


# ---------------------------------------------------------------- graph cut

def _min_cut(cost_fg, cost_bg, img, hard_fg, hard_bg, gamma):
    """Label each pixel fg/bg minimising unary + contrast-sensitive smoothness."""
    h, w = cost_fg.shape
    n = h * w
    idx = np.arange(n).reshape(h, w)
    S = 1000.0
    BIG = int(1e8)

    rows, cols, caps = [], [], []
    diffs = []
    pairs = [((slice(None), slice(0, w - 1)), (slice(None), slice(1, w)), 1.0),
             ((slice(0, h - 1), slice(None)), (slice(1, h), slice(None)), 1.0),
             ((slice(0, h - 1), slice(0, w - 1)), (slice(1, h), slice(1, w)), 1.4142),
             ((slice(0, h - 1), slice(1, w)), (slice(1, h), slice(0, w - 1)), 1.4142)]
    for a, b, _ in pairs:
        diffs.append(((img[a] - img[b]) ** 2).sum(-1))
    mean_d = np.concatenate([d.ravel() for d in diffs]).mean() + 1e-8
    beta = 1.0 / (2.0 * mean_d)
    for (a, b, dist), d in zip(pairs, diffs):
        wgt = gamma * np.exp(-beta * d) / dist
        c = np.maximum((wgt * S).astype(np.int64), 1).ravel()
        ia, ib = idx[a].ravel(), idx[b].ravel()
        rows += [ia, ib]
        cols += [ib, ia]
        caps += [c, c]

    src, snk = n, n + 1
    cs = np.clip(cost_bg * S, 0, 5e6).astype(np.int64).ravel()   # source -> p : pay if p ends up background
    ct = np.clip(cost_fg * S, 0, 5e6).astype(np.int64).ravel()   # p -> sink   : pay if p ends up foreground
    hf, hb = hard_fg.ravel(), hard_bg.ravel()
    cs[hf], ct[hf] = BIG, 0
    cs[hb], ct[hb] = 0, BIG
    allp = np.arange(n)
    rows += [np.full(n, src), allp]
    cols += [allp, np.full(n, snk)]
    caps += [cs, ct]

    r = np.concatenate(rows)
    c = np.concatenate(cols)
    v = np.concatenate(caps).astype(np.int32)
    keep = v > 0
    g = sparse.csr_matrix((v[keep], (r[keep], c[keep])), shape=(n + 2, n + 2), dtype=np.int32)
    res = csgraph.maximum_flow(g, src, snk)
    resid = (g - res.flow).tocsr()
    resid.data[resid.data < 0] = 0
    resid.eliminate_zeros()
    reach = csgraph.breadth_first_order(resid, src, directed=True, return_predecessors=False)
    lab = np.zeros(n + 2, dtype=bool)
    lab[reach] = True
    return lab[:n].reshape(h, w)


def snap(rgb, init, keep=None, drop=None, tol=None, iters=3, work=380, gamma=30.0,
         prior=0.6, seed=0):
    """Refine a rough mask to the object's real edge.

    rgb   : H x W x 3 float image
    init  : H x W bool, the rough outline filled in
    keep  : H x W bool, pixels that are certainly the object (optional)
    drop  : H x W bool, pixels that are certainly not (optional)
    tol   : how far (px) the true edge may be from the rough outline.
            Default scales with the object's size.
    Returns a bool mask at full resolution.
    """
    H, W = init.shape
    bb = io.bbox(init)
    if bb is None:
        return init.copy()
    x0, y0, x1, y1 = bb
    diag = float(np.hypot(x1 - x0, y1 - y0))
    if tol is None:
        tol = float(np.clip(0.035 * diag, 5, 36))
    pad = int(tol * 2 + 8)
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), min(H, y1 + pad)
    sub = rgb[y0:y1, x0:x1]
    m0 = init[y0:y1, x0:x1]
    k_ = keep[y0:y1, x0:x1] if keep is not None else np.zeros_like(m0)
    d_ = drop[y0:y1, x0:x1] if drop is not None else np.zeros_like(m0)

    sh, sw = m0.shape
    s = min(1.0, work / float(max(sh, sw)))
    if s < 1.0:
        size = (max(8, int(round(sw * s))), max(8, int(round(sh * s))))
        sub_s = io.resize(sub, size, io.Image.BILINEAR)
        m_s = io.resize(m0, size, io.Image.NEAREST)
        k_s = io.resize(k_, size, io.Image.NEAREST) if k_.any() else np.zeros(size[::-1], bool)
        d_s = io.resize(d_, size, io.Image.NEAREST) if d_.any() else np.zeros(size[::-1], bool)
    else:
        sub_s, m_s, k_s, d_s = sub, m0, k_, d_
    t = max(2.0, tol * s)

    din = ndi.distance_transform_edt(m_s)
    dout = ndi.distance_transform_edt(~m_s)
    sure_fg = (din > t) | k_s
    sure_bg = ((dout > t) | d_s) & ~k_s
    sure_fg &= ~d_s
    signed = np.where(m_s, din, -dout) / t          # +1 well inside .. -1 well outside

    lab_img = io.rgb_to_lab(sub_s)
    x = lab_img.reshape(-1, 3).astype(np.float64)
    rng = np.random.default_rng(seed)
    cur = m_s.copy()
    cur[sure_fg] = True
    cur[sure_bg] = False
    for _ in range(iters):
        fg_px, bg_px = x[cur.ravel()], x[~cur.ravel()]
        if len(fg_px) < 16 or len(bg_px) < 16:
            break
        gf, gb = _GMM(fg_px, 5, rng), _GMM(bg_px, 5, rng)
        cf = gf.nll(x).reshape(cur.shape)
        cb = gb.nll(x).reshape(cur.shape)
        # weak pull toward the rough outline where colour cannot decide
        cf = cf + prior * np.clip(-signed, 0, 1.5)
        cb = cb + prior * np.clip(signed, 0, 1.5)
        base = np.minimum(cf, cb)
        new = _min_cut(cf - base, cb - base, lab_img, sure_fg, sure_bg, gamma)
        if (new != cur).mean() < 0.002:
            cur = new
            break
        cur = new

    if s < 1.0:
        up = io.resize(cur.astype(np.float32), (sw, sh), io.Image.BILINEAR)
        g = io.guided_filter(io.luma(sub), up, max(2, int(round(1.5 / s))), 1e-3)
        cur_full = g > 0.5
    else:
        cur_full = cur
    cur_full = (cur_full | k_) & ~d_
    out = np.zeros((H, W), dtype=bool)
    out[y0:y1, x0:x1] = cur_full
    return out


# ---------------------------------------------------------------- other selectors

def wand(rgb, seeds, tol=0.08, limit=None, contiguous=True):
    """Flood selection: everything within `tol` (Lab distance) of the seed colours.

    Suits flat-colour illustration. `limit` optionally confines it to a region.
    """
    lab = io.rgb_to_lab(rgb)
    h, w = lab.shape[:2]
    close = np.zeros((h, w), dtype=bool)
    pts = []
    for sx, sy in seeds:
        sx, sy = int(np.clip(round(sx), 0, w - 1)), int(np.clip(round(sy), 0, h - 1))
        # the median of the 3x3 around the seed: a seed on a thin line still reads the line's colour
        ref = np.median(lab[max(0, sy - 1):sy + 2, max(0, sx - 1):sx + 2].reshape(-1, 3), axis=0)
        close |= np.sqrt(((lab - ref) ** 2).sum(-1)) <= tol
        pts.append((sx, sy))
    if limit is not None:
        close &= limit
    if not contiguous:
        return close
    lab_, _ = ndi.label(close, structure=np.ones((3, 3)))
    ids = set(int(lab_[y, x]) for x, y in pts if lab_[y, x] > 0)
    return np.isin(lab_, list(ids)) if ids else np.zeros((h, w), dtype=bool)


def key_tone(rgb, region, tone="dark", radius=10, gain=1.0, floor=0.04):
    """Soft matte for thin structures that are darker or lighter than what is
    behind them (bare twigs on sky, white lettering, ripple lines).

    Estimates the local backdrop by a morphological closing/opening of the
    luminance, then measures how far each pixel departs from it.
    Returns float alpha 0..1 (zero outside `region`).
    """
    L = io.luma(rgb)
    size = 2 * int(radius) + 1
    if tone == "dark":
        back = ndi.grey_closing(L, size=(size, size))
        diff = back - L
    else:
        back = ndi.grey_opening(L, size=(size, size))
        diff = L - back
    back = io.gaussian(back, radius * 0.5)
    inside = diff[region]
    if inside.size == 0:
        return np.zeros_like(L)
    span = np.percentile(inside, 99.0)
    a = np.clip((diff - floor) / max(span * 0.6 - floor, 1e-3), 0, 1) * gain
    a = np.clip(a, 0, 1)
    a[~region] = 0
    return a.astype(np.float32)


def key_color(rgb, region, like, unlike=None, tol=0.10, soft=0.08, cap=None):
    """Soft matte by colour: pixels resembling the `like` samples.

    A sample is either a point [x, y] in the picture or a colour [r, g, b]
    (0..255). With `unlike` samples the matte is relative: a pixel belongs if
    it is nearer to a like-colour than to any unlike-colour.
    """
    lab = io.rgb_to_lab(rgb)
    h, w = lab.shape[:2]

    def sample(pts):
        out = []
        for p in pts:
            if len(p) == 3:
                out.append(io.rgb_to_lab(np.array([[p]], dtype=np.float32) / 255.0)[0, 0])
                continue
            px, py = int(np.clip(round(p[0]), 0, w - 1)), int(np.clip(round(p[1]), 0, h - 1))
            out.append(lab[max(0, py - 1):py + 2, max(0, px - 1):px + 2].reshape(-1, 3).mean(0))
        return np.array(out)

    def mind(refs):
        d = None
        for r in refs:
            dd = np.sqrt(((lab - r) ** 2).sum(-1))
            d = dd if d is None else np.minimum(d, dd)
        return d

    df = mind(sample(like))
    if unlike:
        db = mind(sample(unlike))
        a = io.smoothstep(0.5 - soft * 2, 0.5 + soft * 2, db / (df + db + 1e-6))
        if cap:                              # and never farther than this from a like-colour
            a = a * (1.0 - io.smoothstep(cap, cap + soft, df))
    else:
        a = 1.0 - io.smoothstep(tol, tol + soft, df)
    a = a.astype(np.float32)
    a[~region] = 0
    return a


def soften(rgb, mask, feather=1.0, radius=None):
    """Hard mask -> soft alpha that follows the picture's own edges.

    A guided filter lets the alpha fall off along real edges (so brush hairs
    and deckled watercolour edges keep their shape) instead of a uniform blur.
    """
    m = mask.astype(np.float32)
    if feather <= 0:
        return m
    r = int(radius or max(2, round(feather * 2.0)))
    a = io.guided_filter_color(rgb, m, r, 2e-3)
    a = np.clip((a - 0.08) / 0.84, 0, 1)
    # never grow far beyond the hard mask, never eat far into it
    grow = io.dilate(mask, r + 1)
    core = io.erode(mask, r + 1)
    a = np.where(grow, a, 0.0)
    a = np.where(core, 1.0, a)
    return a.astype(np.float32)


def shade_match(rgb, samples, tolerance=0.20):
    """Match chromatic direction across lightness/shadow changes.

    This is only an additional seed gate inside a semantic region. It must not
    infer object identity from hue or turn arbitrary unlike colours opaque.
    Near-grey references cannot establish a useful chromatic direction.
    """
    low = rgb.min(-1,keepdims=True)
    span = rgb.max(-1,keepdims=True)-low
    chroma = (rgb-low)/np.maximum(span,1e-6)
    best = np.full(rgb.shape[:2],np.inf,np.float32)
    h,w = rgb.shape[:2]
    for sample in samples:
        if len(sample)==3:
            ref = np.asarray(sample,np.float32)/255
        else:
            x,y = int(np.clip(round(sample[0]),0,w-1)),int(np.clip(round(sample[1]),0,h-1))
            ref = rgb[y,x]
        width = float(np.ptp(ref))
        if width < 0.03:
            continue
        direction = (ref-ref.min())/width
        best = np.minimum(best,np.sqrt(((chroma-direction)**2).sum(-1)))
    confidence = 1-io.smoothstep(tolerance,tolerance+0.10,best)
    return np.where(span[...,0]>=0.015,confidence,0).astype(np.float32)


def matte(rgb, region, initial, keep=None, drop=None, radius=4.0, residual=0.035):
    """Local colour-line matting inside an author-owned semantic region.

    The selector confidence is not physical opacity. Opaque foreground and
    background samples come from the observed source, never the inpainted
    plate. Several spatially local F/B pairs allow different backgrounds.
    keep/drop are definite opaque-foreground/background scribbles. region is
    the maximum support, including anticipated soft edges. A missing leaf or
    a fully translucent object without foreground samples is not inferable.
    """
    from scipy.spatial import cKDTree
    region = np.asarray(region, bool)
    initial = np.clip(initial, 0, 1).astype(np.float32)
    keep = np.zeros_like(region) if keep is None else keep.astype(bool)
    drop = np.zeros_like(region) if drop is None else drop.astype(bool)
    if (keep & drop).any():
        raise ValueError("matte foreground and background seeds overlap")
    initial = np.where(region & ~drop, initial, 0)
    initial[keep & region] = 1
    radius = max(1.0, float(radius))
    footprint = initial > 0.02
    core = io.erode(initial > 0.98, 1.5) | (keep & region)
    if not core.any():
        core = (initial > 0.9) | (keep & region)
    if not core.any():
        return initial
    support = io.dilate(footprint, radius) & region & ~drop
    bg = (~io.dilate(footprint, radius)) | drop
    if not bg.any():
        raise ValueError("matte needs observed background outside its support")
    unknown = support & ~core
    if not unknown.any():
        return np.where(region & ~drop, initial, 0).astype(np.float32)
    fy, fx = np.nonzero(core)
    by, bx = np.nonzero(bg)
    uy, ux = np.nonzero(unknown)
    fcoords = np.stack([fy, fx], -1).astype(np.float32)
    bcoords = np.stack([by, bx], -1).astype(np.float32)
    ftree, btree = cKDTree(fcoords), cKDTree(bcoords)
    out = np.zeros_like(initial)
    out[core] = 1
    offsets = np.array([[0,0],[-radius*2,0],[radius*2,0],[0,-radius*2],[0,radius*2]], np.float32)
    for start in range(0, len(uy), 12000):
        y, x = uy[start:start+12000], ux[start:start+12000]
        q = np.stack([y,x],-1).astype(np.float32)
        _, fi = ftree.query(q[:,None,:] + offsets[None,:,:], k=1)
        _, bi = btree.query(q[:,None,:] + offsets[None,:,:], k=1)
        F, B, P = rgb[fy[fi],fx[fi]], rgb[by[bi],bx[bi]], rgb[y,x]
        best = np.full(len(q), np.inf, np.float32)
        value = initial[y,x].copy()
        best_residual = best.copy()
        for f in range(F.shape[1]):
            for b in range(B.shape[1]):
                delta = F[:,f]-B[:,b]
                contrast = (delta*delta).sum(-1)
                a = np.clip(((P-B[:,b])*delta).sum(-1)/np.maximum(contrast,1e-8),0,1)
                err = ((P-(a[:,None]*F[:,f]+(1-a[:,None])*B[:,b]))**2).mean(-1)
                fd = np.linalg.norm(fcoords[fi[:,f]]-q,axis=-1)
                bd = np.linalg.norm(bcoords[bi[:,b]]-q,axis=-1)
                score = err + 0.00004*(a*fd+(1-a)*bd)/radius
                score += np.where(contrast<0.0025,0.02,0)
                take = score < best
                best[take],value[take],best_residual[take] = score[take],a[take],err[take]
        # Model mismatch is uncertainty, not evidence of opacity.
        reliable = best_residual <= residual*residual
        out[y,x] = np.where(reliable,value,initial[y,x])
    out[drop | ~region] = 0
    return np.clip(out,0,1).astype(np.float32)
