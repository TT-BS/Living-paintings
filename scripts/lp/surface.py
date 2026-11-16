"""Continue an observed surface behind its occluder, with explicit ownership.

This is a *single-surface* operation. A wall and a floor need separate calls
with separate donor domains. No edge pixel is implicitly a valid donor.
Masks describe source-image pixels, not object names or image-specific rules.
"""
from dataclasses import dataclass

import numpy as np
from scipy import ndimage as ndi

from . import inpaint


class SurfaceContractError(ValueError):
    """The declared domains cannot produce the requested surface."""


class SurfaceMatteError(SurfaceContractError):
    """The supplied aperture and background cannot reproduce the observation."""


def _mask(value, shape, name):
    a = np.asarray(value)
    if a.shape != shape or not np.all(np.isfinite(a)):
        raise SurfaceContractError("%s needs a finite H x W mask" % name)
    if not np.all((a == 0) | (a == 1)):
        raise SurfaceContractError(
            "%s is an ownership domain and must be binary; pass soft edges "
            "to split_aperture without thresholding them" % name)
    return a.astype(bool)


def _image(value):
    a = np.asarray(value)
    if (a.ndim != 3 or a.shape[2] != 3 or a.size == 0 or not np.issubdtype(a.dtype, np.floating)
            or not np.all(np.isfinite(a)) or a.min() < 0 or a.max() > 1):
        raise SurfaceContractError("image needs finite floating H x W x 3 RGB in 0..1")
    return a.astype(np.float32, copy=False)


@dataclass(frozen=True)
class Continuation:
    """A back plane. RGB outside support is zero, never copied front paint."""

    rgb: np.ndarray
    support: np.ndarray
    visible: np.ndarray
    filled: np.ndarray
    donors: np.ndarray
    evidence: dict

    @property
    def rgba(self):
        return np.dstack((self.rgb, self.support.astype(np.float32)))


def concealed_band(visible, occlusion, pixels):
    """Return a Euclidean reveal band measured from observed back-surface paint.

    ``pixels`` is an author-declared source-pixel budget. It is not inferred
    from a scene or a camera, and does not establish actual motion coverage.
    """
    visible = np.asarray(visible)
    v = _mask(visible, visible.shape, "visible")
    o = _mask(occlusion, v.shape, "occlusion")
    if v.ndim != 2 or not v.any():
        raise SurfaceContractError("visible needs an observed 2D surface")
    if isinstance(pixels, bool) or not np.isscalar(pixels) or not np.isfinite(pixels) or pixels < 0:
        raise SurfaceContractError("reveal pixels must be finite and nonnegative")
    return o & (ndi.distance_transform_edt(~v) <= float(pixels))


def continue_surface(image, *, surface_domain, donor_domain, occlusion,
                     protect, reveal_band, patch=7, quality=2, seed=0,
                     search=None):
    """Synthesize only declared concealed pixels from declared clean paint.

    surface_domain: complete allowed footprint of this one surface, including
        the area it is expected to occupy behind foreground objects.
    occlusion: pixels where source RGB belongs partly or wholly to another
        surface. Thus ``surface_domain & ~occlusion`` is observed, immutable
        back-surface paint. Soft alpha is *not* an ownership domain.
    donor_domain: observed, same-surface pixels that may supply entire patches.
        It must exclude occluded pixels and ``protect``; conflicts are errors.
    protect: lettering, leaves, or other observed details that must never be
        copied into new surface paint. Visible protected paint stays intact.
        Occluded protected pixels may be filled *under* their original owner.
    reveal_band: explicit binary subset of ``surface_domain & occlusion``.
        Use concealed_band to derive it from a declared displacement budget.

    Result support is exactly observed paint plus the requested fill. No RGB
    from other surfaces survives outside support. This function does not
    change an aperture, infer foreground mattes, or promise unseen structure.
    """
    p = _image(image)
    shape = p.shape[:2]
    domain = _mask(surface_domain, shape, "surface_domain")
    donors = _mask(donor_domain, shape, "donor_domain")
    occ = _mask(occlusion, shape, "occlusion")
    protected = _mask(protect, shape, "protect")
    band = _mask(reveal_band, shape, "reveal_band")
    visible = domain & ~occ
    if not visible.any():
        raise SurfaceContractError("surface_domain contains no observed surface")
    if np.any(band & ~(domain & occ)):
        raise SurfaceContractError("reveal_band must be inside the occluded surface domain")
    if np.any(donors & (~visible | protected)):
        raise SurfaceContractError("donor_domain includes occluded, protected, or other-surface pixels")
    if isinstance(patch, bool) or not isinstance(patch, (int, np.integer)) or patch < 3 or patch % 2 != 1:
        raise SurfaceContractError("patch must be an odd integer of at least 3")
    half = patch // 2
    centers = ndi.binary_erosion(donors, structure=np.ones((patch, patch), bool))
    # inpaint's patch-voting implementation needs at least 32 centers. Do not
    # silently accept an all-nearest-colour fallback for an underspecified fill.
    if band.any() and int(centers.sum()) < 32:
        raise SurfaceContractError("donor_domain needs at least 32 complete, protected-free patch centers")
    if search is not None and (not np.isfinite(search) or search <= 0):
        raise SurfaceContractError("search must be positive")
    if band.any():
        # Ensure the patch filler crop includes the declared donors. Its
        # `search` argument selects a context crop, not a donor distance limit.
        yy, xx = np.nonzero(band)
        dy, dx = np.nonzero(donors)
        needed = max(yy.min()-dy.min(), dy.max()-yy.max(),
                     xx.min()-dx.min(), dx.max()-xx.max(), half+1)
        margin = max(int(np.ceil(search or 0)), int(needed)+patch)
        filled_rgb = inpaint.fill(p, band, source=donors, patch=patch,
                                  quality=quality, seed=seed, search=margin)
    else:
        filled_rgb = p
    support = visible | band
    out = np.zeros_like(p)
    out[visible] = p[visible]
    out[band] = filled_rgb[band]
    return Continuation(out, support, visible, band, donors,
                        {"kind": "single-surface declared-domain continuation",
                         "observed_pixels": int(visible.sum()),
                         "filled_pixels": int(band.sum()),
                         "donor_pixels": int(donors.sum()),
                         "complete_donor_patch_centers": int(centers.sum()),
                         "protected_donor_pixels": 0,
                         "visible_pixels_changed": 0,
                         "motion_coverage": "author-declared, not runtime-verified"})


def split_aperture(image, aperture, continuation, *, tolerance=2e-6):
    """Return (front RGBA, back RGBA, evidence) without changing soft alpha.

    ``aperture`` is back visibility: 1 = open, 0 = opaque front. At a soft
    edge, solve the front RGB against the independently continued back plate.
    If this requires out-of-gamut paint, reject the decomposition. An author
    must then improve the matte or supply a verified background; this helper
    never hardens watercolor edges, clips substantive errors, or writes the
    original mixed pixel back into a clean background to conceal a mismatch.
    """
    p = _image(image)
    a = np.asarray(aperture, dtype=np.float32)
    if a.shape != p.shape[:2] or not np.all(np.isfinite(a)) or a.min() < 0 or a.max() > 1:
        raise SurfaceContractError("aperture needs finite H x W alpha in 0..1")
    if not isinstance(continuation, Continuation) or continuation.rgb.shape != p.shape:
        raise SurfaceContractError("continuation must match the source image")
    if not np.isfinite(tolerance) or not 0 <= tolerance <= 1e-4:
        raise SurfaceContractError("tolerance is only for floating roundoff (0..1e-4)")
    if np.any((a > 0) & ~continuation.support):
        raise SurfaceContractError("back support does not cover the entire aperture")
    b = continuation.rgb
    opened = a == 1
    if np.any(np.abs(p[opened] - b[opened]) > tolerance):
        raise SurfaceMatteError("the fully open aperture changes observed original paint")
    alpha = 1-a
    soft = (a > 0) & (a < 1)
    f = p.copy()
    # Tiny foreground opacity amplifies source roundoff. Measure feasibility
    # in observed composite units, and solve the division in float64.
    f[soft] = ((p[soft].astype(np.float64) - a[soft,None]*b[soft].astype(np.float64))
               / alpha[soft,None])
    candidate = np.clip(f, 0, 1)
    residual = np.abs(alpha[...,None]*candidate + a[...,None]*b-p)
    bad = soft & np.any(residual > tolerance, axis=2)
    if bad.any():
        raise SurfaceMatteError(
            "infeasible soft aperture at %d pixels (clipped composite error %.6f); "
            "provide a valid matte/independent background, do not threshold the edge"
            % (bad.sum(), residual[bad].max()))
    f = candidate  # composite roundoff only; feasibility was checked above
    front = np.dstack((f, alpha))
    back = continuation.rgba
    recomposed = f*alpha[..., None] + b*a[..., None]
    error = np.abs(recomposed-p)
    return front, back, {"soft_pixels": int(soft.sum()),
                         "aperture_changed_pixels": 0,
                         "rest_max_error": float(error.max()),
                         "rest_mean_error": float(error.mean()),
                         "soft_policy": "exact feasible solve; inconsistent inputs rejected"}
