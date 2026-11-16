"""Frames laid out for looking at: contact sheets and motion maps."""
import os
import re

import numpy as np
from PIL import Image, ImageDraw

from . import imgops as io


def _label_of(path):
    name = os.path.splitext(os.path.basename(path))[0]
    m = re.search(r"t(\d+(?:\.\d+)?)", name)
    return ("%gs" % float(m.group(1))) if m else name


def motion_map(frames):
    """Where, and how much, the picture changed across the frames.

    Returns an RGB image: the first frame dimmed, overlaid with heat where
    pixels changed (blue = slightly, yellow = clearly, red = strongly), plus
    simple numbers describing the spread of motion.
    """
    first = np.asarray(frames[0].convert("RGB"), dtype=np.float32) / 255.0
    lo, hi = first.copy(), first.copy()
    for f in frames[1:]:
        a = np.asarray(f.convert("RGB").resize(frames[0].size), dtype=np.float32) / 255.0
        lo, hi = np.minimum(lo, a), np.maximum(hi, a)
    rng = (hi - lo).max(-1)
    grey = io.luma(first)[..., None] * 0.45 + 0.2
    t = np.clip(rng / 0.5, 0, 1)[..., None]
    heat = np.stack([np.clip(t[..., 0] * 2.2, 0, 1), np.clip(1.6 - np.abs(t[..., 0] - 0.55) * 3.0, 0, 1) * (t[..., 0] > 0.04),
                     np.clip(1.2 - t[..., 0] * 3.0, 0, 1) * (t[..., 0] > 0.04)], axis=-1)
    k = np.clip(rng / 0.08, 0, 1)[..., None] * 0.85
    out = grey * (1 - k) + heat * k
    moved = rng > 0.06
    stats = {"moving_area": round(float(moved.mean()), 4),
             "strongly_moving_area": round(float((rng > 0.25).mean()), 4)}
    return Image.fromarray(io.to_u8(out)), stats


def rhythm(paths, out, box=None):
    """How much the picture changes from each frame to the next, as a bar chart
    over time. Rests show as low bars, events as peaks: a flat even band means
    constant motion with no events, an exactly repeating pattern means a loop."""
    frames = [Image.open(p).convert("L") for p in paths]
    if box:
        b = [int(v) for v in box]
        frames = [f.crop((b[0], b[1], b[2], b[3])) for f in frames]
    small = [np.asarray(f.resize((max(8, f.width // 2), max(8, f.height // 2))), dtype=np.float32) / 255.0 for f in frames]
    act = [float(np.abs(small[i + 1] - small[i]).mean()) * 1000.0 for i in range(len(small) - 1)]
    if not act:
        return {"error": "need at least two frames"}
    W, H, pad = max(600, len(act) * 6), 220, 26
    im = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(im)
    top = max(max(act), 1e-6)
    bw = (W - 2 * pad) / float(len(act))
    for i, a in enumerate(act):
        x0 = pad + i * bw
        d.rectangle([x0, H - pad - (H - 2 * pad) * a / top, x0 + max(1, bw - 1), H - pad], fill=(60, 110, 180))
    d.line([(pad, H - pad), (W - pad, H - pad)], fill=(0, 0, 0))
    names = [_label_of(p) for p in paths]
    for i in range(0, len(names), max(1, len(names) // 10)):
        io.label(d, (pad + i * bw, H - pad + 4), names[i], size=11, fg=(0, 0, 0), bg=(255, 255, 255))
    io.label(d, (pad, 4), "change between consecutive frames (peak %.1f, median %.1f)" % (top, float(np.median(act))), size=12,
             fg=(0, 0, 0), bg=(255, 255, 255))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    im.save(out, "PNG")
    med = float(np.median(act))
    return {"out": out, "frames": len(frames), "peak": round(top, 2), "median": round(med, 2),
            "quiet_share": round(float(np.mean([a < 0.45 * top for a in act])), 2),
            "peak_to_median": round(top / max(med, 1e-6), 1)}


def contact(paths, out, cols=0, max_side=1800, labels=True, motion=False, box=None, order=False, zoom=1.0):
    frames = [Image.open(p).convert("RGB") for p in paths]
    if box:
        b = [int(v) for v in box]
        frames = [f.crop((b[0], b[1], b[2], b[3])) for f in frames]
    if zoom and zoom != 1.0:
        frames = [f.resize((max(1, int(f.width * zoom)), max(1, int(f.height * zoom))), Image.LANCZOS) for f in frames]
    stats = None
    tiles = list(frames)
    names = [_label_of(p) for p in paths]
    if order and len(frames) > 1:
        # one map per step: what changed between each frame and the next, so the order in which
        # things are reached (a gust crossing, a shadow arriving) can be read left to right
        tiles, new = [], []
        for i in range(len(frames) - 1):
            mm, _ = motion_map([frames[i], frames[i + 1]])
            tiles.append(mm)
            new.append("%s to %s" % (names[i], names[i + 1]))
        names, motion = new, False
    if motion and len(frames) > 1:
        mm, stats = motion_map(frames)
        tiles.append(mm)
        names.append("where it moved")
    n = len(tiles)
    if not cols:
        w, h = tiles[0].size
        best = None
        for c in range(1, n + 1):
            r = -(-n // c)
            score = abs((c * w) / float(r * h) - 1.5)
            if best is None or score < best[0]:
                best = (score, c)
        cols = best[1]
    rows = -(-n // cols)
    w, h = tiles[0].size
    k = min(1.0, max_side / float(max(cols * w, rows * h)))
    tw, th = max(1, int(w * k)), max(1, int(h * k))
    sheet = Image.new("RGB", (cols * tw + (cols - 1) * 4, rows * th + (rows - 1) * 4), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    for i, t in enumerate(tiles):
        x, y = (i % cols) * (tw + 4), (i // cols) * (th + 4)
        sheet.paste(t.resize((tw, th), Image.LANCZOS), (x, y))
        if labels:
            io.label(d, (x + 5, y + 4), names[i], size=max(12, tw // 28))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    sheet.save(out, "JPEG", quality=88)
    res = {"out": out, "frames": len(frames), "tile": [tw, th]}
    if stats:
        res["motion"] = stats
    return res
