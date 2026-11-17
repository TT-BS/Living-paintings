#!/usr/bin/env python3
"""Picture-side tools for the living-picture skill.

  prepare  re-encode the source picture into a working copy (strips metadata,
           caps the size); every later coordinate refers to this copy
  look     the picture (or a zoomed crop) with a labelled pixel grid, so that
           coordinates can be read off by eye
  cut      run a layer plan: lift layers front to back, fill what they hid,
           write review sheets
  bundle   assemble a scene (scene.json + cut layers) into a playable page
  sheet    lay frames out as one contact sheet, optionally with a motion map or a rhythm chart
  diff     compare two images, e.g. the frame at 0 s against the picture
  measure  rows, blobs (eye centres and ready blink masks) or an outline polygon of what passes a tone/colour rule
  palette  colours at points, or the main colours of a region, ready for like/unlike lists
  inspect  a cut layer over several grounds next to what it uncovers, or each of its pieces alone
  vision   macOS only: Apple Vision starting points (subject/person masks, eyes, heads, joints, text, saliency)
  dewatermark  find and remove a share-platform watermark (e.g. Xiaohongshu) before preparing; writes a review sheet
  rectify  a casual photo of a painting: crop to it, undo perspective and lens, even out light, remove a colour cast

Run any command with --help. Needs Python 3.9+, numpy, scipy, Pillow.
"""
import argparse
import json
import os
import sys

sys.dont_write_bytecode = True     # the skill folder may be shared or read-only: no __pycache__ inside it
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _j(o):
    return json.dumps(o, ensure_ascii=False)


def _need():
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        import PIL  # noqa: F401
    except Exception as e:  # pragma: no cover
        sys.stderr.write("Missing a dependency (%s). Install with: python3 -m pip install numpy scipy pillow\n" % e)
        sys.exit(2)


def cmd_prepare(a):
    from lp import imgops as io
    try:
        rgb, s = io.load_image(a.source, a.max)
    except (OSError, ValueError) as e:  # unreadable, damaged or unsupported: one clear line, no traceback
        print(_j({"error": str(e)}))
        sys.exit(2)
    if a.crop:
        x0, y0, x1, y1 = [int(round(v * s)) for v in a.crop]
        rgb = rgb[y0:y1, x0:x1]
    os.makedirs(a.out, exist_ok=True)
    dst = os.path.join(a.out, "picture.png")
    io.save_image(rgb, dst)
    print(_j({"picture": dst, "size": [rgb.shape[1], rgb.shape[0]], "scale_from_source": s}))


def cmd_look(a):
    from PIL import Image, ImageDraw
    from lp import imgops as io
    rgb, _ = io.load_rgb(a.image)
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = (0, 0, W, H) if not a.box else [int(v) for v in a.box]
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
    crop = rgb[y0:y1, x0:x1]
    cw, ch = x1 - x0, y1 - y0
    k = a.scale or min(4.0, a.max / float(max(cw, ch)))
    im = Image.fromarray(io.to_u8(crop)).resize((max(1, int(cw * k)), max(1, int(ch * k))),
                                                Image.LANCZOS if k < 1 else Image.BICUBIC)
    step = a.grid
    if not step:
        target = max(cw, ch) / 12.0
        step = min((5, 10, 20, 25, 50, 100, 200, 250, 500), key=lambda c: abs(c - target))
    if not a.plain:
        ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        fs = max(11, int(min(im.size) / 60))
        gx = (x0 // step + 1) * step if x0 % step else x0
        for x in range(gx, x1, step):
            X = (x - x0) * k
            d.line([(X, 0), (X, im.size[1])], fill=(255, 255, 255, 110), width=1)
            d.line([(X + 1, 0), (X + 1, im.size[1])], fill=(0, 0, 0, 90), width=1)
        gy = (y0 // step + 1) * step if y0 % step else y0
        for y in range(gy, y1, step):
            Y = (y - y0) * k
            d.line([(0, Y), (im.size[0], Y)], fill=(255, 255, 255, 110), width=1)
            d.line([(0, Y + 1), (im.size[0], Y + 1)], fill=(0, 0, 0, 90), width=1)
        im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
        d = ImageDraw.Draw(im)
        for x in range(gx, x1, step):
            X = (x - x0) * k
            io.label(d, (X + 3, 2), str(x), size=fs)
            io.label(d, (X + 3, im.size[1] - fs - 6), str(x), size=fs)
        for y in range(gy, y1, step):
            Y = (y - y0) * k
            io.label(d, (2, Y + 2), str(y), size=fs)
            io.label(d, (im.size[0] - fs * 3 - 2, Y + 2), str(y), size=fs)
    if a.points:
        d = ImageDraw.Draw(im)
        pts = json.loads(a.points)
        for i, p in enumerate(pts):
            X, Y = (p[0] - x0) * k, (p[1] - y0) * k
            d.ellipse([X - 5, Y - 5, X + 5, Y + 5], outline=(255, 0, 255), width=2)
            d.ellipse([X - 1, Y - 1, X + 1, Y + 1], fill=(255, 255, 0))
            io.label(d, (X + 7, Y - 7), str(i), size=12, bg=(255, 0, 255))
    if a.poly:
        d = ImageDraw.Draw(im)
        for poly in json.loads(a.poly):
            q = [((p[0] - x0) * k, (p[1] - y0) * k) for p in poly]
            d.line(q + q[:1], fill=(255, 0, 255), width=2)
    out = a.out or (os.path.splitext(a.image)[0] + "-look.jpg")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    im.save(out, "JPEG", quality=88)
    print(_j({"out": out, "picture_size": [W, H], "shown_box": [x0, y0, x1, y1],
                      "grid_every": step, "zoom": round(k, 3)}))


def cmd_cut(a):
    from lp import layers
    with open(a.plan) as f:
        plan = json.load(f)
    if a.out:
        plan["out"] = a.out
    man = layers.build(plan, os.path.dirname(os.path.abspath(a.plan)), log=lambda m: sys.stderr.write(m + "\n"))
    print(_j({"layers": [l["id"] for l in man["layers"]], "rest_error": man["rest_error"]}))


def cmd_bundle(a):
    from lp import bundle
    print(_j(bundle.bundle(a.scene, a.out, title=a.title, fmt=a.format, quality=a.quality, bg=a.bg, alt=a.alt or "",
                           log=lambda m: sys.stderr.write(m + "\n"))))


def cmd_diff(a):
    import numpy as np
    from PIL import Image
    from lp import imgops as io
    A, _ = io.load_image(a.a)
    B, _ = io.load_image(a.b)
    resampled = A.shape[:2] != B.shape[:2]
    source_b_size = [B.shape[1],B.shape[0]]
    if resampled:
        if not a.resize:
            raise SystemExit("diff: normalized image sizes differ; use --resize only for an explicitly resampled comparison")
        B = np.asarray(io.to_pil(B).resize((A.shape[1],A.shape[0]),Image.LANCZOS),dtype=np.float32)/255
    def premult(pixels):
        alpha = pixels[...,3:4] if pixels.shape[-1]==4 else np.ones(pixels.shape[:2]+(1,),np.float32)
        return np.concatenate([pixels[...,:3]*alpha,alpha],axis=-1)
    d = np.abs(premult(A) - premult(B)).max(-1)
    res = {"max": round(float(d.max()), 4), "mean": round(float(d.mean()), 5), "p99": round(float(np.percentile(d, 99)), 4),
           "share_above_0.04": round(float((d > 0.04).mean()), 4),
           "space":"normalized premultiplied sRGB plus alpha", "resampled":resampled,
           "a_size":[A.shape[1],A.shape[0]],"b_source_size":source_b_size}
    if a.out:
        t = np.clip(d / 0.25, 0, 1)[..., None]
        heat = np.concatenate([t * 2, 1.6 - np.abs(t - 0.55) * 3, 1.2 - t * 3], axis=-1).clip(0, 1) * (t > 0.03)
        k = np.clip(d / 0.04, 0, 1)[..., None] * 0.9
        io.save_image((io.luma(A[...,:3])[..., None] * 0.45 + 0.2) * (1 - k) + heat * k, a.out)
        res["out"] = a.out
    print(_j(res))


def cmd_sheet(a):
    from lp import sheets
    if a.rhythm:
        print(_j(sheets.rhythm(a.frames, a.out, box=a.box)))
        return
    print(_j(sheets.contact(a.frames, a.out, cols=a.cols, max_side=a.max, labels=not a.no_labels,
                                    motion=a.motion, box=a.box, order=a.order, zoom=a.zoom)))


def cmd_measure(a):
    from lp import tools
    near = [float(v) for v in a.near.split(",")] if a.near else None
    print(_j(tools.measure(a.image, box=a.box, tone=a.tone, level=a.level, near=near, tol=a.tol, rows=a.rows,
                           blobs=a.blobs, outline=a.outline, margin=a.margin, min_area=a.min_area, simplify=a.simplify)))


def cmd_palette(a):
    from lp import tools
    print(_j(tools.palette(a.image, points=json.loads(a.points) if a.points else None, box=a.box,
                           poly=json.loads(a.poly) if a.poly else None, k=a.k)))


def cmd_inspect(a):
    from lp import tools
    out = a.out or os.path.join(a.work, "review", "inspect-%s%s.jpg" % (a.layer, "-pieces" if a.pieces else ""))
    print(_j(tools.inspect(a.work, a.layer, out, grounds=a.grounds.split(","), pieces=a.pieces, box=a.box, zoom=a.zoom)))


def cmd_vision(a):
    from lp import apple
    try:
        print(_j(apple.vision(a.image, a.out, what=[w.strip() for w in a.what.split(",")], box=a.box, zoom=a.zoom,
                              faces=a.face, eyes=json.loads(a.eyes) if a.eyes else None,
                              langs=a.langs.split(",") if a.langs else None, plan_dir=a.plan_dir, force=a.force,
                              log=lambda m: sys.stderr.write(m + "\n"))))
    except apple.Unavailable as e:      # not a Mac, no Swift compiler, no writable cache: say so, the rest still works
        print(_j({"error": str(e), "available": False}))
        sys.exit(3)
    except (RuntimeError, ValueError, OSError) as e:    # bad options, a damaged picture, an --out in use
        print(_j({"error": str(e)}))
        sys.exit(2)


def cmd_dewatermark(a):
    from lp import watermark
    out = a.out
    if os.path.isdir(out) or not os.path.splitext(out)[1]:
        out = os.path.join(out, "clean.png")
    elif os.path.splitext(out)[1].lower() != ".png":
        out = os.path.splitext(out)[0] + ".png"          # the restored picture stays lossless
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    review = a.review or os.path.splitext(out)[0] + "-review.jpg"
    try:
        rep = watermark.run(a.image, out, review=review, pill=a.pill, line=a.line, mode=a.mode)
    except (OSError, ValueError) as e:
        print(_j({"error": str(e)}))
        sys.exit(2)
    print(_j(rep))
    if not rep.get("found"):
        sys.exit(4)                    # nothing found, nothing written: callers fall back to the picture as received


def cmd_rectify(a):
    from lp import photo
    out = a.out
    if os.path.isdir(out) or not os.path.splitext(out)[1]:
        out = os.path.join(out, "rectified.png")
    elif os.path.splitext(out)[1].lower() != ".png":
        out = os.path.splitext(out)[0] + ".png"          # the corrected picture stays lossless
    review = a.review or os.path.splitext(out)[0] + "-review.jpg"
    try:
        code, rep = photo.rectify(a.photo, out, review=review, corners=a.corners, frame=a.frame, lens=a.lens,
                                  light=a.light, color=a.color, max_side=max(0, a.max))
    except Exception as e:             # unreadable photo, bad --corners, or a failure: one JSON line, exit 2
        print(_j({"error": "%s%s" % ("" if isinstance(e, (OSError, ValueError)) else type(e).__name__ + ": ", e)}))
        sys.exit(2)
    print(_j(rep))
    if code:
        sys.exit(code)                 # 4: nothing to correct or no boundary found; nothing written


def main():
    _need()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("prepare", help="make the working copy of the picture")
    p.add_argument("source")
    p.add_argument("--out", required=True, help="work folder")
    p.add_argument("--max", type=int, default=1600, help="cap for the longer side (px)")
    p.add_argument("--crop", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), help="crop in source pixels")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("look", help="view with a labelled pixel grid")
    p.add_argument("image")
    p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), help="zoom into this region")
    p.add_argument("--grid", type=int, help="grid spacing in picture px (auto if omitted)")
    p.add_argument("--scale", type=float, help="zoom factor (auto if omitted)")
    p.add_argument("--max", type=int, default=1500, help="longer side of the output")
    p.add_argument("--plain", action="store_true", help="no grid")
    p.add_argument("--points", help="JSON [[x,y],...] to mark, to verify coordinates")
    p.add_argument("--poly", help="JSON [[[x,y],...],...] outlines to draw, to verify a selection")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_look)

    p = sub.add_parser("cut", help="build layers from a plan")
    p.add_argument("plan")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_cut)

    p = sub.add_parser("bundle", help="assemble a scene into a playable page")
    p.add_argument("scene", help="scene.json")
    p.add_argument("--out", required=True, help="output folder (gets index.html and site/)")
    p.add_argument("--title")
    p.add_argument("--alt", help="alt text for the fallback image")
    p.add_argument("--format", default="png", choices=["auto", "webp", "png"],
                   help="layer images: png (default, lossless); auto or webp explicitly allow lossy layers, reported separately from exact original fallback")
    p.add_argument("--quality", type=int, default=92, help="quality of lossy layers")
    p.add_argument("--bg", default="#f3f1ec", help="page background of the demo page")
    p.set_defaults(fn=cmd_bundle)

    p = sub.add_parser("diff", help="compare two images (e.g. the frame at 0 s against the picture)")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--resize", action="store_true", help="explicitly resize b to a; reports resampling and is not a native-size equality test")
    p.add_argument("--out", help="write a picture of where they differ")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("sheet", help="contact sheet from frames")
    p.add_argument("frames", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--cols", type=int, default=0)
    p.add_argument("--max", type=int, default=1800)
    p.add_argument("--no-labels", action="store_true")
    p.add_argument("--motion", action="store_true", help="append a map of where and how much things moved")
    p.add_argument("--box", type=float, nargs=4, help="crop every frame to this region first")
    p.add_argument("--zoom", type=float, default=1.0, help="enlarge each (cropped) frame by this factor, for small regions")
    p.add_argument("--order", action="store_true",
                   help="show, for each step between frames, where the picture changed (to read the order things are reached in)")
    p.add_argument("--rhythm", action="store_true",
                   help="instead of a sheet, chart how much changes from frame to frame over time (rests and events)")
    p.set_defaults(fn=cmd_sheet)

    p = sub.add_parser("measure", help="rows / blobs / outline of the pixels passing a tone or colour rule")
    p.add_argument("image", help="usually work/picture.png")
    p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    p.add_argument("--tone", choices=["light", "dark"], default="light", help="pass pixels lighter (or darker) than --level")
    p.add_argument("--level", type=float, default=150, help="luma threshold 0..255")
    p.add_argument("--near", help="pass pixels near this colour instead: r,g,b (0..255)")
    p.add_argument("--tol", type=float, default=0.08, help="colour distance for --near (Lab, 0..1 scale)")
    p.add_argument("--rows", type=int, default=0, help="list passing segments every N rows")
    p.add_argument("--blobs", action="store_true", help="separate marks: centre, size, axis, and a ready blink entry")
    p.add_argument("--outline", action="store_true", help="outline polygon of the largest passing area, grown by --margin")
    p.add_argument("--margin", type=float, default=3.0)
    p.add_argument("--simplify", type=float, default=1.0, help="outline tolerance in px")
    p.add_argument("--min-area", type=int, default=6)
    p.set_defaults(fn=cmd_measure)

    p = sub.add_parser("palette", help="colour samples for like / unlike lists")
    p.add_argument("image")
    p.add_argument("--points", help='JSON [[x,y],...]: 3x3 means at these points')
    p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    p.add_argument("--poly", help="JSON [[x,y],...] limiting the region")
    p.add_argument("--k", type=int, default=6, help="how many main colours")
    p.set_defaults(fn=cmd_palette)

    p = sub.add_parser("inspect", help="a cut layer over several grounds, or each of its pieces alone")
    p.add_argument("work", help="the cut's output folder (with layers.json)")
    p.add_argument("--layer", required=True)
    p.add_argument("--grounds", default="#3fa0a0,#f0ede8,#2b2b2f", help="comma-separated hex colours")
    p.add_argument("--pieces", action="store_true", help="each connected piece alone on each ground (for released marks)")
    p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"))
    p.add_argument("--zoom", type=float)
    p.add_argument("--out")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("vision", help="macOS only: Apple Vision starting points to check by eye")
    p.add_argument("image", help="usually work/picture.png")
    p.add_argument("--out", required=True,
                   help="folder for this run's vision.json, masks and review.jpg (e.g. work/vision; one folder per --box)")
    p.add_argument("--what", default=",".join(("subjects", "persons", "faces", "pose", "saliency", "text")),
                   help="comma list of subjects, persons, faces, pose, saliency, text")
    p.add_argument("--box", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                   help="look only here, enlarged (small faces and figures); results stay in picture pixels")
    p.add_argument("--zoom", type=float, help="enlargement (default with --box: up to 4x, longer side about 1280 px)")
    p.add_argument("--face", type=float, nargs=4, action="append", metavar=("X0", "Y0", "X1", "Y1"),
                   help="a face you can see that detection missed: fit face landmarks inside this box (repeatable)")
    p.add_argument("--eyes", help="JSON [[x,y] or [x,y,r],...]: eyes you can see; blink entries from the painted marks there")
    p.add_argument("--langs", help="text languages in order of preference, e.g. zh-Hans,en-US (default: detect)")
    p.add_argument("--plan-dir", help="folder of plan.json, for mask paths in the suggestions (default: the job folder above work/)")
    p.add_argument("--force", action="store_true",
                   help="replace an earlier run in --out even if its masks change (plan.json may point at them)")
    p.set_defaults(fn=cmd_vision)

    p = sub.add_parser("dewatermark", help="find and remove a share-platform watermark before preparing")
    p.add_argument("image", help="the picture as received (left unchanged)")
    p.add_argument("--out", required=True, help="restored picture, PNG (e.g. <job>/clean.png); prepare from it afterwards")
    p.add_argument("--review", help="before | after | repaired sheet (default: next to --out, *-review.jpg)")
    p.add_argument("--pill", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                   help="rough box round the logo pill, when detection misses it")
    p.add_argument("--line", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                   help="rough box round the ID text line, when detection misses it")
    p.add_argument("--mode", default="auto", choices=["auto", "untint", "fill", "hybrid"],
                   help="under the pill: untint recovers the lightened paint, fill repaints from round it; auto picks by the ground's texture")
    p.set_defaults(fn=cmd_dewatermark)

    p = sub.add_parser("rectify", help="a casual photo of a painting: crop, perspective, lens, uneven light, colour cast")
    p.add_argument("photo", help="the photo as received (left unchanged)")
    p.add_argument("--out", required=True, help="corrected picture, PNG (e.g. <job>/rectified.png); prepare from it afterwards")
    p.add_argument("--review", help="photo with the boundary | result | light change (default: next to --out, *-review.jpg)")
    p.add_argument("--corners", help='the artwork\'s corners in photo pixels, "x,y;x,y;x,y;x,y" (any order); skips detection')
    p.add_argument("--frame", default="auto", choices=["auto", "keep", "remove"],
                   help="auto: keep the painted surface inside a plain frame and its mat (a sheet's own paper margin "
                        "stays); keep: keep them; remove: also cut away an ornate or uneven frame")
    p.add_argument("--lens", default="auto", choices=["auto", "off"], help="radial lens distortion, from the sides' straightness")
    p.add_argument("--light", default="auto", choices=["auto", "off"], help="even out lamp falloff, a hotspot, vignetting")
    p.add_argument("--color", default="auto", choices=["auto", "off"], help="remove a warm or cool light's colour cast")
    p.add_argument("--max", type=int, default=4096,
                   help="cap for the output's longer side (px; 0 = none); never enlarged beyond the work's own detail")
    p.set_defaults(fn=cmd_rectify)

    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        sys.exit(1)
    a.fn(a)


if __name__ == "__main__":
    main()
