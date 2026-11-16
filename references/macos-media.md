# macOS media: picture inputs and video output

Everything here runs on the Mac itself with what macOS ships; nothing is installed or uploaded.
The two Swift helpers are built once, on first use, with the Xcode command line tools
(`xcode-select --install`) into `~/Library/Caches/living-picture/<name>-<source hash>`; a changed
source builds a new one. Without those tools each path below says what it falls back to.

## Pictures

`prepare`, `look`, `diff`, `measure` and `palette` read their picture through
`lp/imgops.normalized_image`, so what follows holds for them. `prepare` is the one that matters: every
later step reads its `work/picture.png`. `cut` reads the plan's `"image"` through the same function,
so it may be anything `prepare` reads. `mask` part images and a scene's `lightMap` are opened with
Pillow directly: they must be PNG, JPEG or another format Pillow reads (a HEIC there fails with
"cannot identify image file").

| Input | How it is read |
|---|---|
| JPEG, PNG, TIFF, WebP, GIF, BMP, PSD composite | Pillow, as before (byte-identical `prepare` output on 19 test files, including P3 JPEG with EXIF orientation, 16-bit RGB(A) PNG/TIFF, CMYK JPEG, palette PNG with transparency). |
| HEIC/HEIF (iPhone, Photos), AVIF, JPEG XL, camera RAW (DNG, NEF, CR2, CR3, ARW, RAF, ORF, RW2, ...) | macOS: `scripts/macos/lpconvert.swift` (Apple ImageIO, ColorSync through vImage) writes an sRGB PNG to a temporary folder, which then takes the Pillow path. Other systems: an error asking for PNG, JPEG or TIFF (camera RAW exported from a RAW developer). The Pillow here recognises AVIF but has no decoder for it, so AVIF also takes this route. |
| 16-bit greyscale (scans) | Pillow, now rounded to 8 bits. Before, it came out white (no profile) or failed (grey ICC profile, or grey plus alpha). |
| PNG with a transparent colour (tRNS) at 2, 4 or 16 bits | The colour is matched at the file's own bit depth. Pillow matches it against 8-bit pixels, so the wrong pixels turned transparent (16-bit grey: the key's low byte; 16-bit RGB: decoded to high bytes) or none did (2- and 4-bit grey, scaled to 0..255 while the key is not). Alpha now equals ImageIO's on all of these, interlaced and EXIF-turned files included. 8-bit files are unchanged. |
| Uncompressed greyscale TIFF with orientation 5–8 | Opened from a file object, so Pillow decodes it instead of memory-mapping it with the turned size, which scrambled the pixels. |
| Grey ICC profile on a file Pillow opens in a colour, palette or bilevel mode | Pillow opens 16-bit grey plus alpha PNG as RGBA. When every pixel is grey, the profile is applied to that one channel (before, this failed). Colour pixels under a grey profile are still an error, as before. |

Camera RAW is often TIFF inside (DNG, NEF, CR2, ARW, PEF, SRW and most others), and Pillow opens the
small preview it carries as if it were the picture. So RAW is recognised before Pillow decodes
anything: by extension, or by content (a DNGVersion tag; CFA or LinearRaw sensor pixels in any
directory, SubIFDs included; Canon's CR2 header). ImageIO and `sips` choose their RAW decoder by
file name, so a DNG under another name is converted from a copy named `.dng`, and other RAW under a
non-RAW name is refused with a message asking for its camera's extension. A camera this macOS does
not know fails with ImageIO's error and the advice to export from a RAW developer. Off macOS, camera
RAW is that same error, never the preview.

What `lpconvert` does, with measurements against an independent conversion (numpy with exact
primaries, per-channel clipping and the sRGB curve, starting from ImageIO's native decode):

- **Image:** the primary one (the one Photos shows in a multi-image HEIC); `--index N` picks another.
- **Orientation:** EXIF/HEIF orientations 1–8 are applied; all eight are identical to Pillow's
  `exif_transpose`.
- **Colour:** converted to sRGB, relative colorimetric, out-of-gamut colours clipped (what the Pillow
  path does with LittleCMS). On Display P3 charts with 56% of pixels outside sRGB, as 10-bit HEIC,
  AVIF and 16-bit PNG, every pixel is within 1 level, mean 0.003–0.017. An 8-bit P3 photo
  (`/System/Library/Desktop Pictures/Mac Blue.heic`) is within 1 level, mean 0.14. sRGB HEIC is exact.
  On the same 8-bit P3 pixels, LittleCMS (Pillow path) and ColorSync agree within 1 level.
- **Alpha:** stays straight. Colours under partial transparency are converted directly and stay
  within 1 level even at alpha 1–15. `sips` and CGContext drawing premultiply at 8 bits: up to 8
  levels off at alpha ≥ 16 and 62–127 levels at alpha below 16, on the same files.
- **Bit depth:** 10- and 16-bit sources are converted at full precision and rounded once to 8 bits,
  because the working picture is 8-bit. A 16-bit PNG would be no better: Pillow truncates 16-bit
  RGB to 8 bits (measured mean 0.2–0.7 levels, max 2). `--depth 16` writes one anyway, for other uses.
- **HDR:** ImageIO's default decode, the SDR rendition; gain maps are ignored. Not checked against
  an iPhone HDR photo.
- **Damaged files:** ImageIO decodes a truncated HEIC to black and a truncated JPEG to grey, and
  reports success both times. So the box sizes of HEIF/AVIF/JPEG XL files are checked first, and an
  all-zero decode is refused (so is a picture that really is all black). A box that holds the
  picture (`meta`, `moov`, `mdat`, `jxlc`, `jxlp`) running past the end of the file is refused, and
  so is any box that does so before the picture is complete (`meta` or `moov` plus `mdat`; for
  JPEG XL, the whole codestream). Bytes after a complete picture (some phone and camera apps append
  a trailer) are ignored, as ImageIO ignores them. Files Pillow opens but finds damaged keep
  Pillow's error; the fallback covers only formats Pillow cannot decode at all, and camera RAW.
- **Without the Xcode tools:** opaque pictures go through `sips --matchTo "sRGB Profile.icc"`, with
  identical results on opaque pixels (orientation via the tag sips keeps). Pictures with
  transparency are refused and the message says `xcode-select --install`.
- **Speed:** a 6016×6016 HEIC converts in about 2 s; `prepare` then spends most of its time writing
  the optimised PNG, as it does for any input.

Known limits: Pillow's own handling of 16-bit RGB PNG/TIFF reduces to 8 bits *before* the ICC
transform (up to 5 levels off on saturated P3, 2 in gamut). It is left as it was, so that existing
jobs reproduce. Untagged CMYK: ImageIO assumes Generic CMYK while Pillow converts naively, so
`lpconvert` and Pillow disagree there. Pillow reads CMYK JPEGs itself, so only direct `lpconvert`
use sees the difference. JPEG XL is in ImageIO's list of readable types but untested here; no
samples were available (the box check was tried on hand-made box layouts only). Camera RAW was
tested on synthetic DNGs only, not on files from real cameras: 16-bit LinearRaw and Bayer CFA data
in a SubIFD behind an 8-bit preview, both byte orders, orientations 1, 3, 6 and 8, also named
`.tif`, `.nef` and with no extension. ImageIO developed the full picture each time, the `sips`
fallback gave the same pixels on all of them, and sensor data without DNG tags was refused under a
`.tif` name and failed with ImageIO's error under `.nef`.

Direct use (prints one JSON line):
```
python3 -c 'import sys; sys.path.insert(0, "scripts"); from lp.imgops import swift_helper; print(swift_helper("lpconvert")[0])'
<printed path> photo.heic photo-srgb.png [--depth 16] [--index N]
```

## Video

`node scripts/capture.mjs <page> --out <dir> --video SECONDS [--encoder auto|ffmpeg|avfoundation] [--hevc]`

- `--encoder auto` (default): ffmpeg (unchanged: libx264 `-crf 19`, MP4 byte-identical to before).
  On macOS, AVFoundation when ffmpeg is missing, does not run (`ffmpeg -version` exits non-zero or
  crashes, as when a partial `brew upgrade` leaves a library missing) or fails on the frames; the
  JSON then says why under `"ffmpeg_error"`. The JSON says `"encoder"` and `"codec"`; avfoundation
  adds `"rate_control"`, and `"note"` when it changed the codec. A failure gives `"video_error"` and
  keeps the frames in `<out>/<name>_frames/`; when an explicit `--encoder ffmpeg` fails on macOS, it
  adds that `--encoder avfoundation` needs no ffmpeg. Leftover frames from an earlier, longer run
  are cleared first.
- `--encoder avfoundation`: `scripts/macos/lpencode.swift` (AVAssetWriter, hardware encoder).
  - H.264 High, or HEVC Main tagged `hvc1` with `--hevc`; 8-bit 4:2:0, limited range. An odd
    width or height loses its last column or row; nothing is rescaled. Each frame lasts 1/fps, so
    the duration is frames ÷ fps. The index sits at the front (fast start).
  - Size: H.264 on this Mac (M2 Max) takes at most 4096 px a side: 4096×4096 and 4096×720 encode,
    4098×720, 720×4400 and 5000×3000 do not. lpencode then writes HEVC and says so in `"note"`, and
    capture.mjs reports `"codec": "hevc"`. At the default `--width 720` this happens to pictures
    taller than about 1:5.7, such as hanging scrolls. Sizes HEVC gives up on too (16400×200) fail
    with a message suggesting a smaller `--width`.
  - Chroma: lpencode converts the frames to 4:2:0 itself. Given BGRA, the encoder keeps the top-left
    pixel of each 2×2 block, unfiltered. Players disagree on where that sample sits, as fitting their
    output to the stream's own decoded planes shows: AVFoundation (the stack under QuickTime and
    Safari) puts it on the block's top-left pixel, Chrome and ffmpeg's scaler at the block's centre, and a
    chroma location tag in the stream changes none of them. lpencode samples a quarter pixel from
    both, with a [1 4 3]/8 filter on each axis, so no player sees coloured edges moved by half a pixel.
  - Rate control: constant quality 0.87 on Apple silicon. RGB PSNR (dB) against the frames, each
    stream decoded by ffmpeg (with its own matrix), by headless Chrome (a `<video>` drawn into a
    canvas) and by AVFoundation (`AVAssetImageGenerator`):

    | Clip | Encoder | Size | ffmpeg | Chrome | AVFoundation |
    |---|---|---|---|---|---|
    | example (night-house), 720×480, 90 frames | x264 `-crf 19` | 95.5 kB | 40.34 | 36.58 | 29.39 |
    | | lpencode H.264, chroma as the encoder took it from BGRA | 252 kB | 38.32 | 38.35 | 39.59 |
    | | lpencode H.264 | 236 kB | 39.99 | 39.19 | 39.09 |
    | | lpencode HEVC | 183 kB | 40.00 | 39.21 | 39.12 |
    | watercolour, 720×994, 180 frames | x264 `-crf 19` | 629 kB | 42.00 | 36.99 | 29.13 |
    | | lpencode H.264, chroma from BGRA | 2.32 MB | 42.63 | 42.80 | 44.01 |
    | | lpencode H.264 | 2.27 MB | 43.79 | 43.68 | 43.27 |
    | | lpencode HEVC | 1.45 MB | 43.56 | 43.51 | 43.09 |

    x264 here is the capture default from `--png` frames: untagged BT.601, which Chrome and
    AVFoundation decode with the wrong matrix or transfer (see below), so its fair figure is the
    ffmpeg one. Against that, lpencode H.264 is 0.35 dB lower on the example's hard coloured edges
    and 1.8 dB higher on the watercolour, at 2.5 and 3.6 times the size (HEVC: 1.9 and 2.3 times).
    The quarter-pixel chroma costs 0.5–0.7 dB in AVFoundation against the encoder's own top-left
    sample and gains 0.8–0.9 dB in Chrome and 1.2–1.7 dB in ffmpeg; a plain 2×2 mean would gain
    1.4–1.7 dB in Chrome but lose 1.4–1.5 dB in AVFoundation. Average-bitrate mode blurs the first
    second (first frame 35 dB, median 40), so lpencode uses it only with `--bitrate` or on Intel
    Macs (0.3 bit/pixel, untested). Quality 1.0 would switch H.264 to High 4:4:4, which browsers do
    not play, so lpencode caps it at 0.95.
  - Colour: BT.709 primaries and matrix with the sRGB transfer curve, also attached to every frame
    buffer so that the encoder converts nothing. Grey steps come back within 1 level of the frames
    in AVFoundation and exactly in Chrome and ffmpeg; saturated colour patches within 2 levels in
    AVFoundation and 1 in the others. The same video tagged with the BT.709 transfer, or untagged,
    shows about 7 levels brighter in AVFoundation. If AVFoundation refuses the sRGB tag (it names it
    only from macOS 15), lpencode writes the BT.709 tag instead; this was not tested on an older system.
  - Failures: once writing has started, every failure cancels the writer and removes the partial
    movie and the `<name>.sb-…` working copy that fast start makes (tried with a frame of another
    size, an unreadable frame and an encoder that gave up; a full disk takes the same path). Before,
    a 0-byte MP4 and a 5 MB copy stayed next to the kept frames. A writer that fails while lpencode
    waits to hand it a frame ends the run instead of leaving it waiting.
  - Checked: `ffprobe -count_frames` (90 frames, 3.000 s for `--video 3`; tags bt709,
    iec61966-2-1, tv), and frames decoded by AVFoundation (`isPlayable`), Chrome and ffmpeg, for
    H.264 and HEVC. Safari and QuickTime Player themselves were not launched, so their on-screen
    chroma is inferred from AVFoundation's.
- `--hevc` with ffmpeg uses libx265 `-crf 19 -tag:v hvc1`. On the same frames (decoded by ffmpeg)
  it gave 40.37 and 42.57 dB at 92% and 93% of x264's size.
- What the unchanged ffmpeg path produces (measured, for choosing): from the default JPEG
  frames, a full-range BT.601 H.264. QuickTime shows it right, but headless Chrome stretches it as
  if limited range (grey 64 shows as 56, so shadows crush and highlights clip). From `--png`
  frames it is untagged, which AVFoundation shows about 7 levels brighter. The avfoundation
  output displays correctly in both.
- No ffmpeg and no Xcode tools: `video_error` names `xcode-select --install`, and the frames are kept.

## Viewing results on a Mac

Checked without opening any window. LaunchServices maps `.mp4` to QuickTime Player, `.html` to
the default browser (Safari on the test Mac) and `.png`/`.heic` to Preview. `qlmanage -t` renders
thumbnails of the H.264 and HEVC MP4s and of the HEIC inputs, so Quick Look can show them.

```
open <job>/out/index.html          # the page in the default browser; open -a Safari ... for Safari
open <job>/video/f.mp4             # QuickTime Player
qlmanage -p <job>/check/sheet.jpg  # Quick Look preview window
sips -g pixelWidth -g pixelHeight -g profile -g hasAlpha photo.heic   # what a picture is, before prepare
```

## Shortcuts and Finder Quick Actions (not created)

Running `prepare` from Finder needs a Quick Action that the user makes in Automator: "Run Shell
Script", input "image files" in Finder, "pass input as arguments", calling python3 by its absolute
path. Automator's PATH is only `/usr/bin:/bin:/usr/sbin:/sbin`. Saved, it lives in
`~/Library/Services/`. A Shortcut with the same shell step can be run from a terminal with
`shortcuts run "<name>" --input-path <file>` (`shortcuts list` shows what exists). Both need the
user's own GUI steps or writes to `~/Library`, so none were made here.
