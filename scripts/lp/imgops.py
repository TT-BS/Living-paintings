"""Small image helpers shared by the living-picture tools.

Colour algorithms use float32 sRGB arrays in 0..1 (H x W x 3); image I/O
also preserves straight alpha (H x W x 4). Only numpy, scipy and Pillow
are needed. On macOS, formats Pillow cannot decode (HEIC/HEIF, AVIF, JPEG XL,
camera RAW) are first converted with Apple ImageIO (scripts/macos/lpconvert.swift).
"""
import hashlib
import io as _bytes
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, ImageCms, UnidentifiedImageError
from scipy import ndimage as ndi

Image.MAX_IMAGE_PIXELS = None


# ---------------------------------------------------------------- load / save

# A format Pillow knows but this build cannot decode (AVIF without a codec, say). Any other load
# failure is a damaged file and stays an error: ImageIO decodes truncated files to grey or black.
_NO_DECODER = re.compile(r"no codec available|decoder \S+ not available", re.I)


def normalized_image(path):
    """Decode, orient, and convert embedded colour profiles to sRGB.

    Keep alpha separate from the colour transform. Untagged images are
    interpreted as sRGB; an invalid/unsupported embedded profile is an error,
    not permission to discard it. Returned pixels have no source metadata.
    Formats Pillow cannot decode go through Apple ImageIO on macOS.
    """
    # Opened from a file object, not the path, so that Pillow never memory-maps the pixels: it maps an
    # uncompressed greyscale TIFF whose orientation tag is 5-8 with the turned size, scrambling it.
    f = open(path, "rb")
    try:
        source = Image.open(f)
    except Exception as exc:
        f.close()
        if isinstance(exc, UnidentifiedImageError) or _NO_DECODER.search(str(exc)) or \
                _raw_name(path) or _tiff_raw(path):
            return _via_imageio(path, "cannot identify image file" if isinstance(exc, UnidentifiedImageError) else exc)
        raise
    with f, source:
        if source.format == "TIFF" and (_raw_name(path) or _tiff_raw(path)):
            return _via_imageio(path, "camera RAW, of which Pillow reads only the embedded preview")
        try:
            source.load()
        except Exception as exc:
            if not _NO_DECODER.search(str(exc)):
                raise
            return _via_imageio(path, exc)
        im = ImageOps.exif_transpose(source)
        profile = im.info.get("icc_profile")
        has_alpha = "A" in im.getbands() or "transparency" in im.info
        if source.format == "PNG" and "transparency" in im.info and im.mode != "P":
            alpha = _key_alpha(im, path)
        else:
            alpha = im.convert("RGBA").getchannel("A") if has_alpha else None
        colour = im.getchannel("L") if im.mode == "LA" else im
        if colour.mode.startswith("I"):
            colour = _grey16_to_l(colour, path)
        elif profile and profile[16:20] == b"GRAY" and colour.mode in ("1", "P", "RGB", "RGBA"):
            colour = _grey_channel(colour, path)
        if colour.mode not in ("RGB", "L", "CMYK", "LAB"):
            colour = colour.convert("RGB")
        if profile:
            try:
                colour = ImageCms.profileToProfile(
                    colour, ImageCms.ImageCmsProfile(_bytes.BytesIO(profile)),
                    ImageCms.createProfile("sRGB"), outputMode="RGB",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
            except Exception as exc:
                raise ValueError("cannot normalize embedded ICC profile for %s: %s" % (path, exc)) from exc
        else:
            colour = colour.convert("RGB")
        if alpha is not None:
            colour.putalpha(alpha)
        # frombytes deliberately removes EXIF, ICC and incidental source info.
        return Image.frombytes(colour.mode, colour.size, colour.tobytes())


def _key_alpha(im, path):
    """Alpha from a PNG colour key (tRNS), matched at the file's own bit depth.

    Pillow matches the key against 8-bit pixels, which is right for 8-bit files only: 16-bit grey
    gets the key's low byte against clipped samples, 16-bit RGB is decoded to its high bytes, and
    2- and 4-bit grey are scaled to 0..255 while the key is not.
    """
    key = im.info["transparency"]
    with open(path, "rb") as f:
        depth = f.read(25)[24]                              # IHDR bit depth
    if im.mode.startswith("I"):                             # 16-bit grey keeps whole samples
        hit = np.asarray(im) == key
    elif im.mode == "L":
        hit = np.asarray(im) == key * 255 // (2 ** depth - 1)
    elif im.mode == "RGB" and depth == 16:
        # Decode once more, keeping the low byte of each big-endian sample (the 16L unpacker takes
        # the second byte), and transpose it as the pixels were.
        with Image.open(path) as low:
            low.tile = [tuple(t[:3]) + ("RGB;16L",) for t in low.tile]
            low = ImageOps.exif_transpose(low)
        hit = (np.asarray(im).astype(np.int32) * 256 + np.asarray(low) == key).all(-1)
    else:
        return im.convert("RGBA").getchannel("A")
    return Image.fromarray(np.where(hit, 0, 255).astype(np.uint8))


def _grey16_to_l(im, path):
    """16-bit grey (Pillow modes I;16*, or I from older Pillow) to 8-bit L with rounding.

    Pillow's own conversion clips every value above 255 to white.
    """
    a = np.asarray(im).astype(np.int64)
    if im.mode == "I":
        if a.max(initial=0) <= 255 and a.min(initial=0) >= 0:
            return im
        if a.max() > 65535 or a.min() < 0:
            raise ValueError("%s has 32-bit integer pixels; save it as an 8- or 16-bit image first" % path)
    return Image.fromarray(((a + 128) // 257).astype(np.uint8))


def _grey_channel(im, path):
    """The single channel a greyscale ICC profile describes, from grey pixels Pillow decoded as colour.

    Pillow opens 16-bit grey+alpha PNG as RGBA; bilevel and palette files keep their own modes.
    Colour pixels contradict a grey profile, so they stay an error instead of losing two channels.
    """
    r, g, b = im.convert("RGB").split()
    if ImageChops.difference(r, g).getbbox() or ImageChops.difference(r, b).getbbox():
        raise ValueError("cannot normalize embedded ICC profile for %s: the profile is greyscale but "
                         "the picture has colour pixels" % path)
    return r


_MACOS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "macos")
_SRGB_ICC = "/System/Library/ColorSync/Profiles/sRGB Profile.icc"

# Camera RAW is often TIFF inside (DNG, NEF, CR2, ARW, PEF, SRW, ...), and Pillow then opens the small
# preview it carries as if it were the picture. ImageIO and sips pick their RAW decoder by file name.
_RAW_EXTENSIONS = {".dng", ".nef", ".nrw", ".cr2", ".cr3", ".crw", ".arw", ".srf", ".sr2", ".pef", ".srw",
                   ".3fr", ".fff", ".erf", ".kdc", ".dcr", ".mos", ".mef", ".iiq", ".rwl", ".raw", ".rw2",
                   ".orf", ".raf", ".mrw", ".x3f"}


def _raw_name(path):
    return os.path.splitext(path)[1].lower() in _RAW_EXTENSIONS


def _tiff_raw(path):
    """'dng' or 'sensor' when a TIFF-structured file holds camera RAW, else None.

    'dng' has a DNGVersion tag. 'sensor' has Canon's CR2 mark, or CFA or LinearRaw pixels in some
    directory (SubIFDs included), as NEF, ARW, PEF, SRW and most other RAW formats do.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(12)
        order = {b"II*\0": "<", b"MM\0*": ">"}.get(head[:4])
        if order is None or len(head) < 12:
            return None
        found = "sensor" if head[8:12] == b"CR\x02\x00" else None      # CR2 header, version 2.0
        todo, seen = [struct.unpack(order + "I", head[4:8])[0]], set()
        while todo and len(seen) < 64:
            pos = todo.pop()
            if pos in seen or not 8 <= pos <= size - 2:
                continue
            seen.add(pos)
            f.seek(pos)
            n = struct.unpack(order + "H", f.read(2))[0]
            table = f.read(12 * n + 4)
            for i in range(len(table) // 12):
                tag, kind, count = struct.unpack(order + "HHI", table[12 * i:12 * i + 8])
                value = table[12 * i + 8:12 * i + 12]
                if tag == 50706:                                     # DNGVersion
                    return "dng"
                if tag == 262 and (struct.unpack(order + "H", value[:2])[0] if kind == 3 else
                                   struct.unpack(order + "I", value)[0]) in (32803, 34892):
                    found = "sensor"                                 # PhotometricInterpretation CFA, LinearRaw
                if tag == 330 and kind in (4, 13) and 0 < count <= 64:      # SubIFDs, inline when one
                    if count > 1:
                        f.seek(struct.unpack(order + "I", value)[0])
                        value = f.read(4 * count)
                    todo += struct.unpack(order + "%dI" % (len(value) // 4), value[:len(value) // 4 * 4])
            if len(table) == 12 * n + 4:
                todo.append(struct.unpack(order + "I", table[-4:])[0])   # the next directory
    return found


def _via_imageio(path, why):
    """Read a format Pillow cannot decode here by converting it with Apple ImageIO to an sRGB PNG.

    The PNG then goes through the normal path, so the result has the same form as any other picture.
    """
    if sys.platform != "darwin":
        raise ValueError("cannot read %s (%s). Convert it to PNG, JPEG or TIFF first (camera RAW: export "
                         "it from a RAW developer); on macOS the tools convert HEIC, AVIF, JPEG XL and "
                         "camera RAW themselves." % (path, why))
    raw = _tiff_raw(path)
    if raw == "sensor" and not _raw_name(path):
        raise ValueError("%s is camera RAW under a name that does not say so, and ImageIO, like Pillow, "
                         "would read only the preview inside it. Give it its camera's extension (.nef, "
                         ".cr2, .arw, ...) or export it as TIFF, PNG or JPEG." % path)
    raw = raw or _raw_name(path)
    _check_boxes(path)
    tmp = tempfile.mkdtemp(prefix="lp-convert-")
    try:
        png, source = os.path.join(tmp, "picture.png"), path
        if raw == "dng" and not path.lower().endswith(".dng"):     # give the decoders the name they go by
            source = os.path.join(tmp, "source.dng")
            shutil.copyfile(path, source)
        tool, problem = swift_helper("lpconvert")
        if tool:
            p = subprocess.run([tool, source, png], capture_output=True, text=True)
            if p.returncode:
                raise ValueError("cannot read %s. Pillow: %s. Apple ImageIO: %s%s"
                                 % (path, why, p.stderr.strip().replace("lpconvert: ", "").replace(source, path),
                                    ". This macOS may not know the camera: export it from a RAW developer "
                                    "as TIFF, PNG or JPEG" if raw else ""))
            return normalized_image(png)
        # Without the Swift helper, sips (on every Mac) is as exact for opaque pictures, but it
        # premultiplies alpha at 8 bits and shifts translucent colours by up to 64 levels.
        p = subprocess.run(["sips", "-g", "hasAlpha", source], capture_output=True, text=True)
        if p.returncode or "hasAlpha:" not in p.stdout:
            raise ValueError("cannot read %s. Pillow: %s. sips: %s" % (path, why, (p.stderr or p.stdout).strip()))
        if "hasAlpha: yes" in p.stdout:
            raise ValueError("%s has transparency, and converting it exactly needs the lpconvert helper: %s"
                             % (path, problem))
        p = subprocess.run(["sips", "-s", "format", "png", "--matchTo", _SRGB_ICC, source, "--out", png],
                           capture_output=True, text=True)
        if p.returncode or not os.path.exists(png):
            raise ValueError("cannot read %s. Pillow: %s. sips: %s" % (path, why, (p.stderr or p.stdout).strip()))
        im = normalized_image(png)          # sips keeps the orientation as a tag; this applies it
        if not np.asarray(im).any():
            raise ValueError("%s decoded to an empty image: truncated, or a codec this Mac lacks" % path)
        sys.stderr.write("note: read %s with sips (%s)\n" % (os.path.basename(path), problem))
        return im
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Top-level boxes that hold or locate the picture: HEIF/AVIF meta, moov (sequences) and mdat;
# JPEG XL jxlc, or jxlp parts up to the one flagged last.
_PICTURE_BOXES = (b"meta", b"moov", b"mdat", b"jxlc", b"jxlp")


def _check_boxes(path):
    """ImageIO decodes a truncated HEIC/AVIF to black without an error, so walk its boxes first.

    A cut leaves a box running past the end of the file. Bytes after a complete picture are a
    trailer (some phone and camera apps append one), which ImageIO ignores, so the walk stops there.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if f.read(12)[4:8] not in (b"ftyp", b"JXL "):      # not an ISO media file (HEIF, AVIF, JPEG XL)
            return
        pos, seen = 0, set()
        while pos + 8 <= size:
            f.seek(pos)
            n, kind = struct.unpack(">I4s", f.read(8))
            head = 8
            if n == 1:                                       # a 64-bit size follows
                big, head = f.read(8), 16
                n = struct.unpack(">Q", big)[0] if len(big) == 8 else size + 1
            elif n == 0:
                n = size - pos                               # the last box may run to the end
            if head <= n <= size - pos:
                seen.add(kind)
                if kind == b"jxlp" and n >= head + 4 and f.read(4)[0] & 0x80:
                    seen.add(b"jxlp last")                   # the high bit of its index marks the final part
                pos += n
                continue
            done = b"jxlc" in seen or b"jxlp last" in seen or (b"mdat" in seen and seen & {b"meta", b"moov"})
            if kind in _PICTURE_BOXES or not done:
                raise ValueError("%s is truncated or damaged: its %s box runs past the end of the file"
                                 % (path, ascii(kind.decode("latin-1"))))
            return                                           # a trailer after the picture


def swift_helper(stem):
    """Path of scripts/macos/<stem>.swift compiled for this machine, and None or why not.

    Built once per source version into ~/Library/Caches/living-picture/<stem>-<sha256[:12]>,
    under a temporary name renamed into place, so parallel runs never see half a binary.
    """
    src = os.path.join(_MACOS, stem + ".swift")
    if sys.platform != "darwin":
        return None, "it runs only on macOS"
    with open(src, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:12]
    cache = os.path.expanduser("~/Library/Caches/living-picture")
    exe = os.path.join(cache, "%s-%s" % (stem, digest))
    if os.access(exe, os.X_OK):
        return exe, None
    # Without a developer directory, xcrun would pop up an install dialog instead of failing.
    if not (shutil.which("xcode-select") and shutil.which("xcrun")) or \
            subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
        return None, "building it needs the Xcode command line tools (run: xcode-select --install)"
    tmp = "%s.%d.tmp" % (exe, os.getpid())
    try:
        os.makedirs(cache, exist_ok=True)
        sys.stderr.write("building %s once (Swift) ...\n" % stem)
        p = subprocess.run(["xcrun", "swiftc", "-O", src, "-o", tmp], capture_output=True, text=True)
        if p.returncode:
            return None, "xcrun swiftc could not build %s: %s" % (src, p.stderr.strip()[-600:])
        os.replace(tmp, exe)
    except OSError as exc:
        return None, "could not build %s: %s" % (src, exc)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return exe, None


def load_image(path, max_side=None):
    """Return normalized RGB/RGBA float32 pixels and scale; never flatten alpha.

    path may also be an image normalized_image already returned, so a caller that
    needs the full size too decodes a HEIC or RAW (an ImageIO run) only once.
    """
    im = path if isinstance(path, Image.Image) else normalized_image(path)
    scale = 1.0
    if max_side and max(im.size) > max_side:
        scale = max_side / float(max(im.size))
        size = (max(1, int(round(im.size[0] * scale))), max(1, int(round(im.size[1] * scale))))
        im = im.resize(size, Image.LANCZOS)
    return np.asarray(im, dtype=np.float32) / 255.0, scale


def load_rgb(path, max_side=None):
    """RGB analysis view, explicitly flattened on white after normalization.

    Use load_image for preparation, cutting and publishable original pixels.
    """
    pixels, scale = load_image(path, max_side)
    if pixels.shape[-1] == 4:
        a = pixels[..., 3:4]
        pixels = pixels[..., :3] * a + (1 - a)
    return pixels, scale


def to_u8(a):
    return np.clip(np.asarray(a) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def to_pil(a):
    a = np.asarray(a)
    if a.dtype == bool:
        return Image.fromarray((a * 255).astype(np.uint8))
    if a.ndim == 2:
        return Image.fromarray(to_u8(a))
    if a.shape[2] == 4:
        return Image.fromarray(to_u8(a))
    return Image.fromarray(to_u8(a))


def save_image(a, path, quality=92):
    """Write by extension: .png (lossless), .webp, .jpg."""
    im = a if isinstance(a, Image.Image) else to_pil(a)
    p = str(path).lower()
    if p.endswith(".webp"):
        im.save(path, "WEBP", quality=quality, method=5, exact=False)
    elif p.endswith(".jpg") or p.endswith(".jpeg"):
        im.convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
    else:
        im.save(path, "PNG", optimize=True)


def resize(a, size, resample=Image.BILINEAR):
    """size = (w, h). Works for 2-D masks and 3-D colour arrays (float or bool)."""
    a = np.asarray(a)
    if a.dtype == bool:
        return np.asarray(Image.fromarray(a.astype(np.uint8) * 255).resize(size, resample)) > 127
    if a.ndim == 2:
        return np.asarray(Image.fromarray(a.astype(np.float32)).resize(size, resample))
    chans = [np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c]).astype(np.float32)).resize(size, resample))
             for c in range(a.shape[2])]
    return np.stack(chans, axis=-1)


# ---------------------------------------------------------------- colour

def luma(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def rgb_to_lab(rgb):
    """sRGB (0..1) to CIE Lab, scaled so that distances are roughly 0..1.

    L is 0..1, a and b about -1..1. Good enough for colour distance.
    """
    c = np.clip(rgb.astype(np.float32), 0, 1)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
    xyz = lin @ m.T
    xyz = xyz / np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(np.maximum(xyz, 1e-8)), 7.787 * xyz + 16.0 / 116.0)
    L = 1.16 * f[..., 1] - 0.16
    a = 5.0 * (f[..., 0] - f[..., 1])
    b = 2.0 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], axis=-1).astype(np.float32)


# ---------------------------------------------------------------- filters

def gaussian(a, sigma):
    if sigma <= 0:
        return a
    if a.ndim == 3:
        return ndi.gaussian_filter(a, sigma=(sigma, sigma, 0), mode="nearest")
    return ndi.gaussian_filter(a, sigma=sigma, mode="nearest")


def box(a, r):
    size = 2 * int(r) + 1
    if a.ndim == 3:
        return ndi.uniform_filter(a, size=(size, size, 1), mode="nearest")
    return ndi.uniform_filter(a, size=size, mode="nearest")


def guided_filter(guide, src, r, eps):
    """Edge-aware smoothing of `src` (2-D) following edges of `guide` (2-D grey)."""
    g = guide.astype(np.float32)
    s = src.astype(np.float32)
    mg, ms = box(g, r), box(s, r)
    cov = box(g * s, r) - mg * ms
    var = box(g * g, r) - mg * mg
    a = cov / (var + eps)
    b = ms - a * mg
    return box(a, r) * g + box(b, r)


def guided_filter_color(guide, src, r, eps):
    """Guided filter with an RGB guide (He et al.). Better at coloured edges."""
    I = guide.astype(np.float32)
    p = src.astype(np.float32)
    mI = box(I, r)
    mp = box(p, r)
    cov_Ip = box(I * p[..., None], r) - mI * mp[..., None]
    # 3x3 covariance of the guide in each window
    var = np.empty(I.shape[:2] + (3, 3), dtype=np.float32)
    for i in range(3):
        for j in range(i, 3):
            v = box(I[..., i] * I[..., j], r) - mI[..., i] * mI[..., j]
            var[..., i, j] = v
            var[..., j, i] = v
    var += np.eye(3, dtype=np.float32) * eps
    a = np.linalg.solve(var, cov_Ip[..., None])[..., 0]
    b = mp - (a * mI).sum(-1)
    return (box(a, r) * I).sum(-1) + box(b, r)


# ---------------------------------------------------------------- masks

def poly_mask(shape, pts):
    h, w = shape[:2]
    im = Image.new("L", (w, h), 0)
    if len(pts) >= 3:
        ImageDraw.Draw(im).polygon([(float(x), float(y)) for x, y in pts], fill=255)
    return np.asarray(im) > 127


def ellipse_mask(shape, c, r):
    h, w = shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    rx, ry = (r if isinstance(r, (list, tuple)) else (r, r))
    return ((xx - c[0]) / max(rx, 1e-6)) ** 2 + ((yy - c[1]) / max(ry, 1e-6)) ** 2 <= 1.0


def stroke_mask(shape, pts, width):
    h, w = shape[:2]
    im = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(im)
    pts = [(float(x), float(y)) for x, y in pts]
    rad = max(1.0, width / 2.0)
    if len(pts) >= 2:
        d.line(pts, fill=255, width=max(1, int(round(width))), joint="curve")
    for x, y in pts:
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=255)
    return np.asarray(im) > 127


def dilate(mask, r):
    if r <= 0:
        return mask.copy()
    return ndi.distance_transform_edt(~mask) <= r


def erode(mask, r):
    if r <= 0:
        return mask.copy()
    return ndi.distance_transform_edt(mask) > r


def bbox(mask, pad=0, shape=None):
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    h, w = (shape or mask.shape)[:2]
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + 1 + pad)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + 1 + pad)
    return int(x0), int(y0), int(x1), int(y1)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


# ---------------------------------------------------------------- drawing

def font(size):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def label(draw, xy, text, size=14, fg=(255, 255, 255), bg=(0, 0, 0)):
    f = font(size)
    x, y = xy
    try:
        l, t, r, b = draw.textbbox((x, y), text, font=f)
    except Exception:
        l, t, r, b = x, y, x + 7 * len(text), y + size
    draw.rectangle([l - 2, t - 1, r + 2, b + 1], fill=bg)
    draw.text((x, y), text, fill=fg, font=f)


def checker(h, w, cell=12, a=(0.92, 0.25, 0.75), b=(0.78, 0.15, 0.62)):
    """A loud magenta checkerboard: any leftover fringe or hole shows on it."""
    yy, xx = np.mgrid[0:h, 0:w]
    m = ((yy // cell + xx // cell) % 2).astype(bool)
    out = np.empty((h, w, 3), dtype=np.float32)
    out[m] = a
    out[~m] = b
    return out


def contour(mask):
    """One-pixel inner outline of a boolean mask."""
    return mask & ~ndi.binary_erosion(mask, iterations=1, border_value=0)
