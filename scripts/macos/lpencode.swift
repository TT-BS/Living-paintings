// lpencode: encode numbered frames into an MP4 with AVFoundation (macOS only, no ffmpeg needed).
//
//   lpencode <frames-dir> <out.mp4> [--fps 30] [--hevc] [--quality 0.87 | --bitrate BITS_PER_SECOND]
//
// capture.mjs calls it when ffmpeg is missing or with --encoder avfoundation. Every .png/.jpg in
// <frames-dir> is a frame, in name order. H.264 High profile by default, HEVC Main (tagged hvc1)
// with --hevc: 8-bit 4:2:0 from the hardware encoder, which Safari, QuickTime and Chrome play.
// The H.264 encoder takes at most 4096 px a side; larger frames are encoded as HEVC and the JSON
// says so in "note". An odd width or height loses its last column or row.
// Chroma: frames are converted to 4:2:0 here (see toYCbCr), filtered so that QuickTime, Safari,
// Chrome and ffmpeg all show coloured edges well; given BGRA, the encoder would keep only the
// top-left pixel of each 2x2 block, unfiltered.
// Rate control: on Apple silicon a constant quality (default 0.87: on two rendered clips, RGB PSNR
// within 0.4 dB of ffmpeg's libx264 -crf 19 or above it, at 2.5 to 3.6 times its size). Average
// bitrate mode starts each clip blurred and takes about a second to recover, so it is used only
// with --bitrate or on Intel Macs. Quality 1.0 turns H.264 into High 4:4:4 Predictive, a profile
// web players generally lack, hence the 0.95 cap.
// Colour: frames are sRGB, so the stream says BT.709 primaries and matrix with the sRGB transfer
// curve. QuickTime and Safari honour that tag; untagged or BT.709-transfer video shows about
// 7 levels brighter there (Chrome ignores the transfer either way). The index is written first,
// so a page can start playing before the file has loaded. Prints one JSON line.
import AVFoundation
import CoreGraphics
import CoreVideo
import Foundation
import ImageIO
import VideoToolbox

// Set once writing has started, so that no failure leaves a broken movie, or the multi-megabyte
// "<name>.sb-…" working copy, behind.
var abandon: (() -> Void)? = nil

func die(_ message: String, _ code: Int32 = 1) -> Never {
  abandon?()
  FileHandle.standardError.write(("lpencode: " + message + "\n").data(using: .utf8)!)
  exit(code)
}

let usage = "usage: lpencode <frames-dir> <out.mp4> [--fps 30] [--hevc] [--quality 0.87 | --bitrate BITS_PER_SECOND]"
var paths: [String] = []
var fps: Int32 = 30
var hevc = false
var bitrate: Int? = nil
var quality = 0.87
var argv = Array(CommandLine.arguments.dropFirst())
while !argv.isEmpty {
  let a = argv.removeFirst()
  switch a {
  case "--fps": fps = Int32(argv.isEmpty ? "" : argv.removeFirst()) ?? 0
  case "--hevc": hevc = true
  case "--bitrate": bitrate = Int(argv.isEmpty ? "" : argv.removeFirst()) ?? 0
  case "--quality": quality = Double(argv.isEmpty ? "" : argv.removeFirst()) ?? 0
  case "-h", "--help": print(usage); exit(0)
  default: paths.append(a)
  }
}
if paths.count != 2 || fps < 1 || (bitrate ?? 1) < 1 || !(quality > 0 && quality <= 0.95) { die(usage, 2) }

let dir = URL(fileURLWithPath: paths[0], isDirectory: true)
let frames = ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [])
  .filter { ["png", "jpg", "jpeg"].contains($0.pathExtension.lowercased()) }
  .sorted { $0.lastPathComponent < $1.lastPathComponent }
if frames.isEmpty { die("no .png or .jpg frames in \(paths[0])") }

func load(_ url: URL) -> CGImage {
  guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
        let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { die("cannot read frame \(url.path)") }
  return img
}

let first = load(frames[0])
let width = first.width & ~1, height = first.height & ~1
if width < 2 || height < 2 { die("frames are too small: \(first.width)x\(first.height)") }
#if arch(arm64)
let constantQuality = bitrate == nil
#else
let constantQuality = false          // not verified with Intel encoders: use a generous average bitrate there
#endif
func averageRate(_ codec: String) -> Int {
  bitrate ?? max(1_000_000, Int(Double(width * height) * Double(fps) * (codec == "hevc" ? 0.2 : 0.3)))
}

let out = URL(fileURLWithPath: paths[1])
try? FileManager.default.removeItem(at: out)
// Moving the index to the front leaves a "<name>.sb-…" copy beside the movie on macOS 27; remove
// the ones this run creates.
let leftovers = { () -> Set<String> in
  let names = (try? FileManager.default.contentsOfDirectory(atPath: out.deletingLastPathComponent().path)) ?? []
  return Set(names.filter { $0.hasPrefix(out.lastPathComponent + ".sb-") })
}
let leftBefore = leftovers()
guard let writer = try? AVAssetWriter(outputURL: out, fileType: .mp4) else { die("cannot create \(paths[1])") }
writer.shouldOptimizeForNetworkUse = true

func settings(_ codec: String, _ transfer: String) -> [String: Any] {
  var compression: [String: Any] = [
    AVVideoExpectedSourceFrameRateKey: fps,
    AVVideoMaxKeyFrameIntervalKey: Int(fps) * 2,
    AVVideoAllowFrameReorderingKey: true,
    AVVideoProfileLevelKey: codec == "hevc" ? (kVTProfileLevel_HEVC_Main_AutoLevel as String) : AVVideoProfileLevelH264HighAutoLevel,
  ]
  if constantQuality { compression[AVVideoQualityKey] = quality } else { compression[AVVideoAverageBitRateKey] = averageRate(codec) }
  if codec == "h264" { compression[AVVideoH264EntropyModeKey] = AVVideoH264EntropyModeCABAC }
  return [
    AVVideoCodecKey: codec == "hevc" ? AVVideoCodecType.hevc : AVVideoCodecType.h264,
    AVVideoWidthKey: width, AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: compression,
    AVVideoColorPropertiesKey: [
      AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
      AVVideoTransferFunctionKey: transfer,
      AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2,
    ],
  ]
}
// AVFoundation names the sRGB curve only from macOS 15; older systems get the BT.709 tag. Each codec
// is tried with both, so that a size H.264 refuses is not taken for a refused tag.
var chosen: (codec: String, transfer: String)? = nil
search: for codec in hevc ? ["hevc"] : ["h264", "hevc"] {
  for transfer in [kCVImageBufferTransferFunction_sRGB as String, AVVideoTransferFunction_ITU_R_709_2]
  where writer.canApply(outputSettings: settings(codec, transfer), forMediaType: .video) {
    chosen = (codec, transfer)
    break search
  }
}
guard let pick = chosen else {
  die("this Mac cannot encode \(hevc ? "HEVC" : "H.264 or HEVC") at \(width)x\(height); use smaller frames (capture.mjs --width)")
}
let (codec, transfer) = pick
let note: String? = codec == "hevc" && !hevc
  ? "this Mac cannot encode H.264 at \(width)x\(height)\(max(width, height) > 4096 ? " (it takes at most 4096 px a side)" : ""), so the video is HEVC"
  : nil

let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings(codec, transfer))
input.expectsMediaDataInRealTime = false
let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
  kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
  kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
  kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
])
writer.add(input)
abandon = {
  if writer.status == .writing { writer.cancelWriting() }
  try? FileManager.default.removeItem(at: out)
  for name in leftovers().subtracting(leftBefore) { try? FileManager.default.removeItem(at: out.deletingLastPathComponent().appendingPathComponent(name)) }
}
if !writer.startWriting() { die("cannot start writing: \(writer.error?.localizedDescription ?? "unknown error")") }
writer.startSession(atSourceTime: .zero)

// Each frame is drawn into one sRGB ARGB bitmap, then converted into the encoder's buffer.
let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
let rowBytes = width * 4
let bitmap = UnsafeMutableRawPointer.allocate(byteCount: rowBytes * height, alignment: 64)
guard let ctx = CGContext(data: bitmap, width: width, height: height, bitsPerComponent: 8, bytesPerRow: rowBytes, space: srgb,
                          bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Big.rawValue)
else { die("cannot make a drawing buffer") }
ctx.interpolationQuality = .none
// The buffers carry the stream's own colour tags, so the encoder takes them as they are.
let tags: [CFString: String] = [kCVImageBufferColorPrimariesKey: kCVImageBufferColorPrimaries_ITU_R_709_2 as String,
                                kCVImageBufferYCbCrMatrixKey: kCVImageBufferYCbCrMatrix_ITU_R_709_2 as String,
                                kCVImageBufferTransferFunctionKey: transfer]

// BT.709 video range, 8 bits. Y from each pixel; Cb and Cr from the colour around the point a quarter
// pixel right of and below each 2x2 block's top-left pixel (a [1 4 3]/8 filter on each axis). Players
// disagree on where that sample sits: AVFoundation (QuickTime, Safari) puts it on the top-left pixel,
// Chrome and ffmpeg's scaler at the block's centre. A quarter pixel from both, no player sees coloured
// edges shifted by half a pixel, as one of them does with a plain 2x2 mean or with the unfiltered
// top-left sample the encoder takes itself from BGRA.
let half = width / 2
var filtered = [Float](repeating: 0, count: height * half * 3)        // R, G, B after the horizontal pass
func toYCbCr(_ pb: CVPixelBuffer) {
  let src = bitmap.assumingMemoryBound(to: UInt8.self)
  let luma = CVPixelBufferGetBaseAddressOfPlane(pb, 0)!.assumingMemoryBound(to: UInt8.self)
  let chroma = CVPixelBufferGetBaseAddressOfPlane(pb, 1)!.assumingMemoryBound(to: UInt8.self)
  let lumaRow = CVPixelBufferGetBytesPerRowOfPlane(pb, 0), chromaRow = CVPixelBufferGetBytesPerRowOfPlane(pb, 1)
  let k: Float = 219.0 / 255.0, c: Float = 224.0 / 255.0
  filtered.withUnsafeMutableBufferPointer { h in
    for y in 0..<height {
      let row = src + y * rowBytes                  // A, R, G, B; opaque, so premultiplied equals straight
      for x in 0..<width {
        let p = row + x * 4
        luma[y * lumaRow + x] = UInt8(16.5 + k * (0.2126 * Float(p[1]) + 0.7152 * Float(p[2]) + 0.0722 * Float(p[3])))
      }
      for j in 0..<half {
        let l = row + max(2 * j - 1, 0) * 4, m = row + 2 * j * 4, r = m + 4
        for ch in 1...3 { h[(y * half + j) * 3 + ch - 1] = 0.125 * Float(l[ch]) + 0.5 * Float(m[ch]) + 0.375 * Float(r[ch]) }
      }
    }
    for i in 0..<height / 2 {
      let up = max(2 * i - 1, 0) * half * 3, mid = 2 * i * half * 3, down = mid + half * 3
      for j in 0..<half {
        let o = j * 3
        let R = 0.125 * h[up + o] + 0.5 * h[mid + o] + 0.375 * h[down + o]
        let G = 0.125 * h[up + o + 1] + 0.5 * h[mid + o + 1] + 0.375 * h[down + o + 1]
        let B = 0.125 * h[up + o + 2] + 0.5 * h[mid + o + 2] + 0.375 * h[down + o + 2]
        chroma[i * chromaRow + 2 * j] = UInt8(128.5 + c * (-0.114572 * R - 0.385428 * G + 0.5 * B))
        chroma[i * chromaRow + 2 * j + 1] = UInt8(128.5 + c * (0.5 * R - 0.454153 * G - 0.045847 * B))
      }
    }
  }
}

// canApply passes some sizes (16400x200 HEVC, say) that the encoder then gives up on.
func encoderFailed(at i: Int) -> Never {
  die("the \(codec == "hevc" ? "HEVC" : "H.264") encoder failed at frame \(i) of \(width)x\(height): "
      + "\(writer.error?.localizedDescription ?? "unknown error")\(max(width, height) > 4096 ? "; smaller frames may work (capture.mjs --width)" : "")")
}

for (i, url) in frames.enumerated() {
  let image = i == 0 ? first : load(url)
  if image.width & ~1 != width || image.height & ~1 != height { die("frame \(url.lastPathComponent) is \(image.width)x\(image.height), not \(first.width)x\(first.height)") }
  ctx.setFillColor(red: 0, green: 0, blue: 0, alpha: 1)
  ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
  // anchored at the top left: an odd last row or column falls off the bottom or right edge
  ctx.draw(image, in: CGRect(x: 0, y: height - image.height, width: image.width, height: image.height))
  guard let pool = adaptor.pixelBufferPool else { encoderFailed(at: i) }
  var buffer: CVPixelBuffer? = nil
  if CVPixelBufferPoolCreatePixelBuffer(nil, pool, &buffer) != kCVReturnSuccess || buffer == nil { die("cannot allocate a frame buffer") }
  let pb = buffer!
  CVPixelBufferLockBaseAddress(pb, [])
  toYCbCr(pb)
  CVPixelBufferUnlockBaseAddress(pb, [])
  for (key, value) in tags { CVBufferSetAttachment(pb, key, value as CFString, .shouldPropagate) }
  while !input.isReadyForMoreMediaData {          // a writer that has failed is never ready again
    if writer.status != .writing { encoderFailed(at: i) }
    usleep(2000)
  }
  if !adaptor.append(pb, withPresentationTime: CMTime(value: CMTimeValue(i), timescale: fps)) { encoderFailed(at: i) }
}
input.markAsFinished()
writer.endSession(atSourceTime: CMTime(value: CMTimeValue(frames.count), timescale: fps))   // the last frame lasts 1/fps too
let done = DispatchSemaphore(value: 0)
writer.finishWriting { done.signal() }
done.wait()
if writer.status != .completed { die("writing \(paths[1]) failed: \(writer.error?.localizedDescription ?? "unknown error")") }
abandon = nil
for name in leftovers().subtracting(leftBefore) { try? FileManager.default.removeItem(at: out.deletingLastPathComponent().appendingPathComponent(name)) }

let bytes = ((try? FileManager.default.attributesOfItem(atPath: out.path))?[.size] as? NSNumber)?.intValue ?? 0
var result: [String: Any] = [
  "video": out.path, "codec": codec, "frames": frames.count, "fps": Int(fps),
  "duration": Double(frames.count) / Double(fps), "size": [width, height], "bytes": bytes,
  "rate_control": constantQuality ? "quality \(quality)" : "average \(averageRate(codec)) bit/s",
  "transfer": transfer == AVVideoTransferFunction_ITU_R_709_2 ? "bt709" : "srgb",
]
if let note { result["note"] = note }
let json = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .withoutEscapingSlashes])
print(String(data: json, encoding: .utf8)!)
