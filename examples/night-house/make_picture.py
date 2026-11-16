#!/usr/bin/env python3
"""Draws the example picture (a flat-colour night scene) so the example needs
no third-party artwork. Usage: python3 make_picture.py [out.png]"""
import math
import random
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 800
rnd = random.Random(4)
im = Image.new("RGB", (W, H), "#1b2a4a")
d = ImageDraw.Draw(im)

# sky in flat bands
for i, c in enumerate(["#1b2a4a", "#20325a", "#263c69", "#2d4677", "#355084"]):
    d.rectangle([0, 90 * i + 60, W, H], fill=c)

# stars
for _ in range(70):
    x, y, r = rnd.randrange(W), rnd.randrange(0, 380), rnd.choice([1, 1, 2, 2, 3])
    d.polygon([(x, y - r * 2), (x + r * 0.6, y - r * 0.6), (x + r * 2, y), (x + r * 0.6, y + r * 0.6),
               (x, y + r * 2), (x - r * 0.6, y + r * 0.6), (x - r * 2, y), (x - r * 0.6, y - r * 0.6)], fill="#f4f1dc")

# moon with a halo ring
d.ellipse([890, 70, 1070, 250], fill="#3e5a92")
d.ellipse([920, 100, 1040, 220], fill="#f7e9a6")
d.ellipse([952, 128, 978, 154], fill="#ecd98c")
d.ellipse([990, 170, 1006, 186], fill="#ecd98c")

# hills
def hill(base, amp, wl, ph, col):
    pts = [(0, H)]
    for x in range(0, W + 20, 20):
        pts.append((x, base - amp * math.sin(x / wl + ph) - amp * 0.4 * math.sin(x / (wl * 0.37) + ph * 2)))
    pts.append((W, H))
    d.polygon(pts, fill=col)

hill(470, 46, 210, 0.6, "#23365e")
hill(560, 34, 150, 2.1, "#1a2b47")

# ground
d.rectangle([0, 650, W, H], fill="#1f4a3a")
d.polygon([(0, 650), (W, 650), (W, 664), (0, 672)], fill="#2a5e49")

# house
d.rectangle([420, 440, 700, 652], fill="#c96b4a")
d.polygon([(390, 446), (560, 318), (730, 446)], fill="#5a2e2a")
d.rectangle([640, 330, 672, 400], fill="#7a3f36")          # chimney
d.rectangle([634, 322, 678, 336], fill="#5a2e2a")
d.rectangle([590, 540, 650, 652], fill="#3b2320")          # door
d.ellipse([636, 592, 644, 600], fill="#e8c56a")
d.rectangle([462, 486, 546, 566], fill="#5a2e2a")          # lit window
d.rectangle([470, 494, 538, 558], fill="#ffd98a")
d.line([(504, 494), (504, 558)], fill="#5a2e2a", width=5)
d.line([(470, 526), (538, 526)], fill="#5a2e2a", width=5)
d.rectangle([588, 470, 652, 520], fill="#5a2e2a")          # dark window
d.rectangle([594, 476, 646, 514], fill="#2a3b5c")

# smoke: separate puffs, rising and leaning right
for (x, y, r) in [(662, 296, 16), (680, 262, 20), (706, 222, 25), (742, 180, 30), (790, 140, 34)]:
    d.ellipse([x - r, y - r * 0.8, x + r, y + r * 0.8], fill="#aab4c8")

# cat on the ridge, tail hanging down the roof
d.ellipse([528, 286, 574, 336], fill="#11151f")            # body
d.ellipse([536, 256, 568, 288], fill="#11151f")            # head
d.polygon([(538, 264), (541, 244), (552, 260)], fill="#11151f")
d.polygon([(556, 260), (565, 244), (567, 266)], fill="#11151f")
d.line([(570, 326), (590, 344), (598, 370), (590, 392)], fill="#11151f", width=9, joint="curve")
d.ellipse([585, 387, 595, 397], fill="#11151f")
d.ellipse([544, 268, 549, 274], fill="#e8d36a")            # eyes
d.ellipse([556, 268, 561, 274], fill="#e8d36a")

# tree with a lantern
d.rectangle([842, 470, 866, 656], fill="#3a2a20")
d.line([(854, 520), (800, 478)], fill="#3a2a20", width=10)
for (x, y, r, c) in [(854, 400, 78, "#1f5a45"), (800, 440, 52, "#1f5a45"), (912, 446, 56, "#1f5a45"),
                     (840, 372, 50, "#2a6e54"), (884, 410, 44, "#2a6e54")]:
    d.ellipse([x - r, y - r, x + r, y + r], fill=c)
d.line([(790, 486), (790, 530)], fill="#11151f", width=2)   # lantern string
d.rectangle([778, 530, 802, 562], fill="#ffcf70")
d.rectangle([775, 526, 805, 532], fill="#5a2e2a")
d.rectangle([775, 560, 805, 566], fill="#5a2e2a")

# reeds in the foreground
for x in range(60, 360, 22):
    hgt = rnd.randrange(60, 130)
    lean = rnd.randrange(-10, 14)
    d.line([(x, 690), (x + lean, 690 - hgt)], fill="#3f7a5c", width=4)
    d.ellipse([x + lean - 5, 690 - hgt - 20, x + lean + 5, 690 - hgt + 4], fill="#8a5a3a")

# fence
for x in range(930, 1190, 44):
    d.rectangle([x, 596, x + 14, 668], fill="#d9c9a8")
    d.polygon([(x, 596), (x + 7, 584), (x + 14, 596)], fill="#d9c9a8")
d.rectangle([920, 618, 1196, 628], fill="#d9c9a8")

# lettering
def font(size):
    for name in ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()

d.text((64, 716), "GOOD NIGHT", fill="#f4f1dc", font=font(46))

im.save(sys.argv[1] if len(sys.argv) > 1 else "night-house.png")
