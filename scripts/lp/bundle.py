"""Assemble a scene into something a browser can play.

Writes two forms:
  <out>/site/        scene.json + layers/ + living-picture.js + index.html
                     (the form to copy into a website; needs to be served over http)
  <out>/index.html   everything inlined in one file (opens by double-click,
                     and is what the frame-capture tool loads)
"""
import base64
import io as _io
import json
import os

from PIL import Image, ImageChops
from . import imgops as image_io

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.normpath(os.path.join(HERE, "..", ".."))


def runtime_js():
    src = os.path.join(SKILL, "runtime", "src")
    parts = sorted(f for f in os.listdir(src) if f.endswith(".js"))
    texts = []
    for name in parts:
        with open(os.path.join(src,name),encoding="utf-8") as f:
            texts.append(f.read())
    return "\n".join(texts)


def _encode(im, fmt, quality):
    """Encode one layer. "auto" keeps a layer lossless when that costs little
    (flat colour, line art, small cut-outs) and otherwise uses lossy WebP."""
    buf = _io.BytesIO()
    if fmt == "png":
        im.save(buf, "PNG", optimize=True)
        return buf.getvalue(), "image/png"
    if fmt == "jpg":
        im.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue(), "image/jpeg"
    im.save(buf, "WEBP", quality=quality, method=5)
    lossy = buf.getvalue()
    if fmt == "auto":
        b2 = _io.BytesIO()
        im.save(b2, "WEBP", lossless=True, quality=70, method=4)
        exact = b2.getvalue()
        if len(exact) <= 1.6 * len(lossy) or len(exact) < 48 * 1024:
            return exact, "image/webp"
    return lossy, "image/webp"


def _same_pixels(im, encoded):
    """Encoded pixels, not MIME or quality settings, establish losslessness."""
    with Image.open(_io.BytesIO(encoded)) as decoded:
        if decoded.size != im.size:
            return False
        extrema = ImageChops.difference(im.convert("RGBA"), decoded.convert("RGBA")).getextrema()
        return all(hi == 0 for lo, hi in extrema)


def _fixed_contract(layer, im, original, encoded):
    """The one-pass fixed overlay supports opaque originals and hard masks.

    A soft mask/transparent original needs destination replacement, not
    source-over. Refuse those cases instead of claiming precise protection.
    """
    if original.convert("RGBA").getchannel("A").getextrema() != (255,255):
        raise ValueError("fixed layers require an opaque working original; transparent fixed replacement is unsupported")
    rect=layer.get("rect")
    if (not isinstance(rect,(list,tuple)) or len(rect)!=4 or
            any(not isinstance(v,int) or isinstance(v,bool) for v in rect)):
        raise ValueError("fixed layer %s requires an integer source rect" % layer["id"])
    x,y,w,h=rect
    if x<0 or y<0 or w<=0 or h<=0 or x+w>original.width or y+h>original.height or im.size!=(w,h):
        raise ValueError("fixed layer %s rect must match its unscaled original pixels" % layer["id"])
    rgba=im.convert("RGBA");mask=rgba.getchannel("A")
    if any(mask.histogram()[1:255]):
        raise ValueError("fixed layer %s requires binary alpha (0 or 255); soft fixed masks are unsupported" % layer["id"])
    difference=ImageChops.difference(rgba.convert("RGB"),original.crop((x,y,x+w,y+h)).convert("RGB"))
    masked=ImageChops.multiply(difference,Image.merge("RGB",(mask,mask,mask)))
    if any(hi for lo,hi in masked.getextrema()):
        raise ValueError("fixed layer %s must use the exact corresponding original pixels" % layer["id"])
    if not _same_pixels(im,encoded):
        raise ValueError("fixed layer %s cannot use lossy output encoding" % layer["id"])
    return {"id":layer["id"],"mode":"opaque-source-hard-mask","rect":list(rect),
            "source_pixel_equal":True,"binary_alpha":True,"lossless":True}


def flow_map(spec, size, preview_bg=None):
    """Build a flow map from a few arrows.

    spec = {"arrows": [[x1, y1, x2, y2], ...], "areas": [[[x, y], ...], ...], "feather": 24}
    Each arrow says "paint here drifts this way". Directions are blended
    smoothly between arrows; `areas` (optional outlines) limit where anything
    flows at all. Returns (map image, preview image or None).
    """
    import numpy as np
    from PIL import ImageDraw, ImageFilter
    W, H = size
    k = max(1, int(round(max(W, H) / 320.0)))
    w, h = max(2, W // k), max(2, H // k)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    px, py = (xx + 0.5) * k, (yy + 0.5) * k
    sx = np.zeros((h, w), np.float32)
    sy = np.zeros((h, w), np.float32)
    sw = np.zeros((h, w), np.float32)
    for x1, y1, x2, y2 in spec.get("arrows", []):
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** 0.5 or 1.0
        for t in (0.0, 0.5, 1.0):                       # an arrow speaks for its whole length
            cx, cy = x1 + dx * t, y1 + dy * t
            wgt = 1.0 / (((px - cx) ** 2 + (py - cy) ** 2) + 30.0 ** 2) ** 1.5
            sx += wgt * dx / n
            sy += wgt * dy / n
            sw += wgt
    if not spec.get("arrows"):
        sx[:] = 1.0
        sw[:] = 1.0
    nrm = np.sqrt(sx * sx + sy * sy) + 1e-9
    ux, uy = sx / nrm, sy / nrm
    mask = Image.new("L", (w, h), 0 if spec.get("areas") else 255)
    d = ImageDraw.Draw(mask)
    for poly in spec.get("areas", []):
        d.polygon([(x / k, y / k) for x, y in poly], fill=255)
    fe = spec.get("feather", 24) / float(k)
    if spec.get("areas") and fe > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(fe * 0.5))
    m = np.asarray(mask, dtype=np.float32) / 255.0
    rgb = np.stack([ux * 0.5 + 0.5, uy * 0.5 + 0.5, m], axis=-1)
    img = Image.fromarray(np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8))
    prev = None
    if preview_bg is not None:
        prev = preview_bg.convert("RGB").resize((W, H))
        prev = Image.blend(prev, Image.new("RGB", (W, H), (255, 255, 255)), 0.45)
        dp = ImageDraw.Draw(prev)
        step = max(24, max(W, H) // 28)
        for y in range(step // 2, H, step):
            for x in range(step // 2, W, step):
                j, i = min(h - 1, y // k), min(w - 1, x // k)
                if m[j, i] < 0.15:
                    continue
                L = step * 0.42 * (0.4 + 0.6 * m[j, i])
                ex, ey = x + ux[j, i] * L, y + uy[j, i] * L
                dp.line([(x - ux[j, i] * L, y - uy[j, i] * L), (ex, ey)], fill=(20, 60, 200), width=2)
                dp.ellipse([ex - 3, ey - 3, ex + 3, ey + 3], fill=(200, 30, 30))
    return img, prev


def light_map(spec, size, picture=None):
    """Light on the moving things, as a smooth map over the picture.

    spec = {"base": 1, "zones": [{"poly"|"rect"|"ellipse": ..., "light": 0.6 or [r, g, b]}], "feather": 40,
            "auto": {"strength": 1, "blur": px}}
    It is the light where a moving thing passes, not the brightness of the
    ground behind it: a petal crossing in front of a dark window is still in
    the sun. Zones are read from the picture by the author; `auto` estimates
    light from large-scale luminance and is only valid where the ground has one
    local colour (snow, a plain wall). Values are relative; the map is
    normalised so its brightest point is 1. Returns (map image, preview).
    """
    import numpy as np
    from PIL import ImageDraw, ImageFilter

    def colour(v, what):
        if isinstance(v, bool):
            raise ValueError("lightMap %s must be a number or [r, g, b]" % what)
        if isinstance(v, (int, float)):
            v = [v, v, v]
        if (not isinstance(v, (list, tuple)) or len(v) != 3 or
                any(isinstance(c, bool) or not isinstance(c, (int, float)) or c < 0 for c in v)):
            raise ValueError("lightMap %s must be a nonnegative number or [r, g, b]" % what)
        return np.asarray(v, np.float32)

    W, H = size
    k = max(1, int(round(max(W, H) / 320.0)))
    w, h = max(2, W // k), max(2, H // k)
    L = np.ones((h, w, 3), np.float32) * colour(spec.get("base", 1.0), "base")
    outlines = []
    for z in spec.get("zones", []):
        mask = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(mask)
        if "poly" in z:
            pts = [(x / k, y / k) for x, y in z["poly"]]
            d.polygon(pts, fill=255)
            outlines.append([(x, y) for x, y in z["poly"]] + [tuple(z["poly"][0])])
        elif "rect" in z:
            x0, y0, x1, y1 = z["rect"]
            d.rectangle([x0 / k, y0 / k, x1 / k, y1 / k], fill=255)
            outlines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
        elif "ellipse" in z:
            (cx, cy), r = z["ellipse"]["c"], z["ellipse"]["r"]
            rx, ry = (r, r) if isinstance(r, (int, float)) else r
            d.ellipse([(cx - rx) / k, (cy - ry) / k, (cx + rx) / k, (cy + ry) / k], fill=255)
            outlines.append([(cx + rx * np.cos(t), cy + ry * np.sin(t)) for t in np.linspace(0, 2 * np.pi, 33)])
        else:
            raise ValueError("lightMap zones need poly, rect or ellipse")
        fe = z.get("feather", spec.get("feather", 40)) / float(k)
        if fe > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(fe * 0.5))
        m = (np.asarray(mask, np.float32) / 255.0)[..., None]
        L = L * (1 - m) + colour(z.get("light", 1.0), "zone light") * m
    auto = spec.get("auto")
    if auto:
        if picture is None:
            raise ValueError("lightMap.auto needs the picture")
        strength = auto if isinstance(auto, (int, float)) and not isinstance(auto, bool) else auto.get("strength", 1.0)
        blur = (auto.get("blur") if isinstance(auto, dict) else None) or 0.03 * max(W, H)
        g = picture.convert("L").resize((w, h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(blur / float(k)))
        lum = np.asarray(g, np.float32) / 255.0 + 1e-3
        L = L * ((lum / np.median(lum)) ** float(strength))[..., None]
    if L.max() <= 0:
        raise ValueError("lightMap is dark everywhere")
    L = L / L.max()
    img = Image.fromarray(np.clip(L * 255 + 0.5, 0, 255).astype(np.uint8), "RGB")
    prev = None
    if picture is not None:
        pic = np.asarray(picture.convert("RGB").resize((W, H)), np.float32) / 255.0
        up = np.asarray(img.resize((W, H), Image.BILINEAR), np.float32) / 255.0
        prev = Image.fromarray(np.clip(pic * (0.25 + 0.75 * up) * 255, 0, 255).astype(np.uint8))
        dp = ImageDraw.Draw(prev)
        for line in outlines:
            dp.line(line, fill=(255, 220, 0), width=max(1, W // 400))
    return img, prev


def _uri(data, mime):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>
  html, body { margin: 0; height: 100%%; background: %(bg)s; }
  body { display: flex; align-items: center; justify-content: center; }
  .living-picture { position: relative; width: min(94vw, calc(94vh * %(w)d / %(h)d)); aspect-ratio: %(w)d / %(h)d; overflow: hidden; }
  .living-picture > img { position: absolute; inset: 0; width: 100%%; height: 100%%; display: block; }
</style></head>
<body>
<div class="living-picture" id="picture"><img src="%(fallback)s" alt="%(alt)s"></div>
%(scripts)s
</body></html>
"""


def bundle(scene_path, out_dir, title=None, fmt="png", quality=92, bg="#f3f1ec", alt="", log=print):
    scene_dir = os.path.dirname(os.path.abspath(scene_path))
    with open(scene_path, encoding="utf-8") as f:
        scene = json.load(f)
    work = scene.pop("work", "work")
    if not os.path.isabs(work):
        work = os.path.join(scene_dir, work)
    with open(os.path.join(work, "layers.json")) as f:
        man = json.load(f)
    by_id = dict((l["id"], l) for l in man["layers"])
    scene.setdefault("size", man["size"])
    pic = image_io.normalized_image(os.path.join(work, "picture.png"))
    if list(pic.size) != list(scene["size"]):
        raise ValueError("scene.size must match the normalized working original; refusing a stretched fallback")
    original_opaque = pic.convert("RGBA").getchannel("A").getextrema() == (255,255)

    site = os.path.join(out_dir, "site")
    os.makedirs(os.path.join(site, "layers"), exist_ok=True)
    assets = {}
    encoding = []
    fixed_contracts = []
    total = 0
    for L in scene["layers"]:
        ref = L.pop("from", L["id"])
        if ref not in by_id:
            raise SystemExit("scene layer %r: no cut layer named %r in %s (have: %s)"
                             % (L["id"], ref, os.path.join(work, "layers.json"), ", ".join(sorted(by_id))))
        m = by_id[ref]
        L.setdefault("rect", m["rect"])
        # The blend belongs to the asset: a pigment layer stores transmittance, which only means
        # anything under multiply. A scene cannot silently draw it the other way.
        asset_blend = m.get("blend", "normal")
        if L.get("blend", asset_blend) != asset_blend:
            raise ValueError("scene layer %s: blend %r contradicts cut layer %s (%r)"
                             % (L["id"], L.get("blend"), ref, asset_blend))
        if asset_blend != "normal":
            L["blend"] = asset_blend
        im = Image.open(os.path.join(work, m["src"]))
        im.load()
        if L.get("fixed"):
            im.close()
            im = image_io.normalized_image(os.path.join(work,m["src"]))
        lf = L.pop("format", fmt)
        data, mime = _encode(im, lf, quality)
        if L.get("fixed"):
            fixed_contracts.append(_fixed_contract(L,im,pic,data))
        encoding.append({"id": L["id"], "requested_format": lf,
                         "decoded_pixel_equal": _same_pixels(im, data)})
        name = "layers/%s.%s" % (ref, {"image/png": "png", "image/jpeg": "jpg"}.get(mime, "webp"))
        with open(os.path.join(site, name), "wb") as f:
            f.write(data)
        L["src"] = name
        assets[name] = _uri(data, mime)
        total += len(data)
    if isinstance(scene.get("flowMap"), dict):
        pic0 = Image.open(os.path.join(work, "picture.png"))
        fm, prev = flow_map(scene["flowMap"], scene["size"], pic0)
        os.makedirs(out_dir, exist_ok=True)
        fm.save(os.path.join(work, "flow.png"))
        if prev is not None:
            prev.save(os.path.join(out_dir, "flow-preview.jpg"), "JPEG", quality=85)
            log("flow map built from %d arrows; look at %s (red dot = where the paint goes)"
                % (len(scene["flowMap"].get("arrows", [])), os.path.join(out_dir, "flow-preview.jpg")))
        scene["flowMap"] = "flow.png"
    if scene.get("flowMap"):
        im = Image.open(os.path.join(work, scene["flowMap"]))
        data, mime = _encode(im, "png", quality)
        name = "layers/flow.png"
        with open(os.path.join(site, name), "wb") as f:
            f.write(data)
        scene["flowMap"] = name
        assets[name] = _uri(data, mime)
        total += len(data)
    if isinstance(scene.get("lightMap"), dict):
        lm, lprev = light_map(scene["lightMap"], scene["size"], pic)
        os.makedirs(out_dir, exist_ok=True)
        lm.save(os.path.join(work, "light.png"))
        if lprev is not None:
            lprev.save(os.path.join(out_dir, "light-preview.jpg"), "JPEG", quality=85)
            log("light map built from %d zones; look at %s (darker = less light on moving things there)"
                % (len(scene["lightMap"].get("zones", [])), os.path.join(out_dir, "light-preview.jpg")))
        scene["lightMap"] = os.path.join(work, "light.png")
    elif isinstance(scene.get("lightMap"), str) and not os.path.isabs(scene["lightMap"]):
        scene["lightMap"] = os.path.join(scene_dir, scene["lightMap"])      # an author-supplied map, beside the scene
    if scene.get("lightMap"):
        im = Image.open(scene["lightMap"]).convert("RGB")
        data, mime = _encode(im, "png", quality)
        name = "layers/light.png"
        with open(os.path.join(site, name), "wb") as f:
            f.write(data)
        scene["lightMap"] = name
        assets[name] = _uri(data, mime)
        total += len(data)
    for L in scene["layers"]:
        if L.get("relight") and not scene.get("lightMap"):
            raise ValueError("scene layer %s relights but the scene has no lightMap" % L["id"])

    hooks_src = None
    hooks = scene.pop("hooks", None)
    if hooks:
        import hashlib
        with open(os.path.join(scene_dir, hooks), encoding="utf-8") as f:
            hooks_src = f.read().strip().rstrip(";")
        scene["hooksId"] = "h" + hashlib.sha256(hooks_src.encode("utf-8")).hexdigest()[:10]
    # the closing bracket goes on its own line, so a trailing // comment in the file cannot swallow it
    reg = ('(window.LivingPictureHooks = window.LivingPictureHooks || {})["%s"] = (\n%s\n);\n'
           % (scene.get("hooksId", ""), hooks_src)) if hooks_src else ""

    fb, fb_mime = _encode(pic, "png", quality)
    with open(os.path.join(site, "picture.png"), "wb") as f:
        f.write(fb)
    # A reused output directory must not retain the old lossy fallback.
    old_fallback = os.path.join(site, "picture.jpg")
    if os.path.exists(old_fallback):
        os.remove(old_fallback)
    scene["original"] = "picture.png"
    total += len(fb)
    scene["assetContract"] = {
        "originalOpaque": original_opaque,
        "fixedLayers": fixed_contracts,
        "working_space": "8-bit sRGB with source alpha preserved",
        "fallback": {"src": "picture.png", "lossless": True,
                     "decoded_pixel_equal": _same_pixels(pic, fb)},
        "layers": encoding,
        "all_layer_encodings_lossless": all(item["decoded_pixel_equal"] for item in encoding),
        "cut_rest_error": man.get("rest_error"),
        "encoded_cut_rest_error": man.get("encoded_rest_error"),
        "meaning": "Fallback equals the normalized working original. Lossless layer encoding does not prove rest compositing or moving edges correct."
    }
    if not scene["assetContract"]["all_layer_encodings_lossless"]:
        log("WARNING: explicit layer encoding changed pixels; original fallback remains lossless, active-layer equality is not claimed")
    js = runtime_js()
    with open(os.path.join(site, "living-picture.js"), "w", encoding="utf-8") as f:
        f.write(js)
    with open(os.path.join(site, "scene.json"), "w", encoding="utf-8") as f:
        json.dump(scene, f, ensure_ascii=False, indent=1)
    if hooks_src:
        with open(os.path.join(site, "hooks.js"), "w", encoding="utf-8") as f:
            f.write(reg)

    title = title or "Living picture"
    W, H = scene["size"]
    served = ('%s<script src="living-picture.js"></script>\n<script>\n'
              'fetch("scene.json").then(function(r){return r.json()}).then(function(scene){'
              'window.__lp=LivingPicture.mount("#picture",scene,{});});\n</script>'
              % ('<script src="hooks.js"></script>\n' if hooks_src else ""))
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE % dict(title=title, bg=bg, w=W, h=H, fallback="picture.png", alt=alt, scripts=served))

    inline = ("<script>\n%s\n</script>\n<script>\n%svar scene=%s;\nvar assets=%s;\n"
              "assets['picture.png']=document.querySelector('#picture > img').src;\n"
              "window.__lp=LivingPicture.mount('#picture',scene,{assets:assets,motion:(/[?&]force/.test(location.search)?'force':undefined)});\n</script>"
              % (js, reg, json.dumps(scene, ensure_ascii=False), json.dumps(assets)))
    single = os.path.join(out_dir, "index.html")
    with open(single, "w", encoding="utf-8") as f:
        f.write(PAGE % dict(title=title, bg=bg, w=W, h=H, fallback=_uri(fb, fb_mime), alt=alt, scripts=inline))
    log("bundled %d layers, %.0f KB of images -> %s" % (len(scene["layers"]), total / 1024.0, single))
    return {"page": single, "site": site, "layers": len(scene["layers"]), "image_kb": round(total / 1024.0),
            "asset_contract": scene["assetContract"]}
