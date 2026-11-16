// lpconvert: decode a picture with Apple ImageIO and write it as an sRGB PNG (macOS only).
//
//   lpconvert <input> <output.png> [--depth 8|16] [--index N]
//
// For formats Pillow cannot open here (HEIC/HEIF, AVIF, JPEG XL, camera RAW, ...); lp/imgops.py calls
// it and then reads the PNG through its usual path. The primary image (or --index N) is decoded,
// its EXIF/HEIF orientation applied, and its colours converted to sRGB by ColorSync (relative
// colorimetric: out-of-gamut colours clip, as the Pillow path does with LittleCMS). Alpha stays
// straight: colours under partial transparency are converted directly, not through an 8-bit
// premultiplied bitmap (sips and CGContext drawing shift them by up to 64 levels at low alpha).
// 10- and 16-bit sources are converted at full precision and rounded once. --depth 16 keeps 16 bits
// in the PNG; the living-picture tools ask for 8 because Pillow truncates 16-bit RGB PNGs to 8 bits.
// Prints one JSON line describing the result.
import Accelerate
import CoreGraphics
import Foundation
import ImageIO

func die(_ message: String, _ code: Int32 = 1) -> Never {
  FileHandle.standardError.write(("lpconvert: " + message + "\n").data(using: .utf8)!)
  exit(code)
}

let usage = "usage: lpconvert <input> <output.png> [--depth 8|16] [--index N]"
var files: [String] = []
var depth = 8
var wantIndex: Int? = nil
var argv = Array(CommandLine.arguments.dropFirst())
while !argv.isEmpty {
  let a = argv.removeFirst()
  switch a {
  case "--depth": depth = Int(argv.isEmpty ? "" : argv.removeFirst()) ?? 0
  case "--index": wantIndex = Int(argv.isEmpty ? "" : argv.removeFirst())
  case "-h", "--help": print(usage); exit(0)
  default: files.append(a)
  }
}
if files.count != 2 || (depth != 8 && depth != 16) { die(usage, 2) }

guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: files[0]) as CFURL, nil),
      CGImageSourceGetCount(source) > 0 else { die("not a format ImageIO can read: \(files[0])") }
let count = CGImageSourceGetCount(source)
let index = wantIndex ?? CGImageSourceGetPrimaryImageIndex(source)
if index < 0 || index >= count { die("no image \(index): the file has \(count)") }
guard let image = CGImageSourceCreateImageAtIndex(source, index, nil) else { die("ImageIO could not decode image \(index) of \(files[0])") }
guard let space = image.colorSpace else { die("\(files[0]) holds a mask, not a picture") }
let props = (CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any]) ?? [:]
let orientation = (props[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
let alpha: Bool
switch image.alphaInfo {
case .none, .noneSkipFirst, .noneSkipLast: alpha = false
default: alpha = true
}

// One ColorSync conversion straight into the output format: straight alpha, sRGB, relative colorimetric.
let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
let channels = alpha ? 4 : 3
let bytesPerPixel = channels * depth / 8
let bitmapInfo = CGBitmapInfo(rawValue: (alpha ? CGImageAlphaInfo.last : CGImageAlphaInfo.none).rawValue
                              | (depth == 16 ? CGBitmapInfo.byteOrder16Little.rawValue : 0))
var format = vImage_CGImageFormat(bitsPerComponent: UInt32(depth), bitsPerPixel: UInt32(depth * channels),
                                  colorSpace: Unmanaged.passUnretained(srgb), bitmapInfo: bitmapInfo, version: 0,
                                  decode: nil, renderingIntent: .relativeColorimetric)
var decoded = vImage_Buffer()
let status = vImageBuffer_InitWithCGImage(&decoded, &format, nil, image, vImage_Flags(kvImageNoFlags))
if status != kvImageNoError { die("colour conversion of \(files[0]) failed (vImage error \(status))") }

// EXIF orientation 1-8 -> upright pixels; 5-8 swap width and height.
let sw = Int(decoded.width), sh = Int(decoded.height)
let swap = orientation >= 5 && orientation <= 8
let w = swap ? sh : sw, h = swap ? sw : sh
let rowBytes = w * bytesPerPixel
var pixels = Data(count: rowBytes * h)
pixels.withUnsafeMutableBytes { (out: UnsafeMutableRawBufferPointer) in
  let dst = out.baseAddress!, src = decoded.data!, srcRow = decoded.rowBytes
  if orientation < 2 || orientation > 8 {
    for y in 0..<h { (dst + y * rowBytes).copyMemory(from: src + y * srcRow, byteCount: rowBytes) }
    return
  }
  for y in 0..<h {
    for x in 0..<w {
      var sx = x, sy = y
      switch orientation {
      case 2: sx = sw - 1 - x
      case 3: sx = sw - 1 - x; sy = sh - 1 - y
      case 4: sy = sh - 1 - y
      case 5: sx = y; sy = x
      case 6: sx = y; sy = sh - 1 - x
      case 7: sx = sw - 1 - y; sy = sh - 1 - x
      case 8: sx = sw - 1 - y; sy = x
      default: break
      }
      (dst + y * rowBytes + x * bytesPerPixel).copyMemory(from: src + sy * srcRow + sx * bytesPerPixel, byteCount: bytesPerPixel)
    }
  }
}
free(decoded.data)

// ImageIO decodes some truncated files (HEIC among them) to all-zero pixels without an error.
if !pixels.contains(where: { $0 != 0 }) { die("\(files[0]) decoded to an empty image: truncated, or a codec this Mac lacks") }

guard let provider = CGDataProvider(data: pixels as CFData),
      let upright = CGImage(width: w, height: h, bitsPerComponent: depth, bitsPerPixel: depth * channels, bytesPerRow: rowBytes,
                            space: srgb, bitmapInfo: bitmapInfo, provider: provider, decode: nil, shouldInterpolate: false,
                            intent: .defaultIntent),
      let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: files[1]) as CFURL, "public.png" as CFString, 1, nil)
else { die("cannot create \(files[1])") }
CGImageDestinationAddImage(dest, upright, nil)
if !CGImageDestinationFinalize(dest) { die("cannot write \(files[1])") }

let profile = (props[kCGImagePropertyProfileName] as? String) ?? (space.name as String?) ?? "untagged"
let result: [String: Any] = [
  "out": files[1], "size": [w, h], "depth": depth, "alpha": alpha,
  "source_size": [image.width, image.height], "source_bits": image.bitsPerComponent, "source_profile": profile,
  "source_type": (CGImageSourceGetType(source) as String?) ?? "unknown", "orientation": orientation,
  "images": count, "index": index,
]
let json = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .withoutEscapingSlashes])
print(String(data: json, encoding: .utf8)!)
