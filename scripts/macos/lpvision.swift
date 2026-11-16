// lpvision: on-device Apple Vision perception for a living-picture job.
//
// Reads one picture, runs the requested analyses and prints ONE JSON object on
// stdout. Every coordinate is in the picture's own upright pixels, origin top
// left (Vision itself answers in normalised units, origin bottom left). Masks
// and heat maps are written into --out as 8-bit grayscale PNGs at the
// picture's pixel size, soft edges kept, so a mask can be a cut part as is.
// Nothing leaves the machine: Vision runs locally and downloads nothing.
//
// Compiled on demand by scripts/lp/apple.py (xcrun swiftc -O). Uses the
// classic VN* requests (macOS 14+ for instance masks and animal pose), which
// keeps macOS 14 working; the Swift-native Vision API (macOS 15+) adds
// nothing needed here.
import CoreGraphics
import CoreVideo
import Foundation
import ImageIO
import UniformTypeIdentifiers
import Vision

let usage = """
usage: lpvision <what> <image> --out <dir> [--box x0 y0 x1 y1] [--zoom k] [--face x0 y0 x1 y1]...
                [--langs zh-Hans,en-US] [--save-input file.png]
       lpvision check
  what: comma list of subjects, persons, faces, pose, saliency, text, or all
  --box   analyse only this region (picture pixels); it is enlarged (--zoom, auto by default) so that
          small faces and figures reach the size the models expect; results map back to the picture
  --face  a face box you know is there: face landmarks are fitted inside it even if detection missed it
  --save-input  also write the image exactly as Vision analysed it (cropped, enlarged, flattened)
"""

struct Fail: Error { let msg: String }

func emit(_ obj: [String: Any]) {
    let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{\"error\":\"unserialisable result\"}".utf8)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func r2(_ v: Double) -> Double { v.isFinite ? (v * 100).rounded() / 100 : 0 }
func ms(_ t0: Date) -> Int { Int(Date().timeIntervalSince(t0) * 1000) }

// ------------------------------------------------------------------ picture

/// The picture upright (EXIF orientation applied, as Pillow's exif_transpose does), in sRGB,
/// any transparency flattened on white like the skill's own analysis view (imgops.load_rgb).
func loadPicture(_ path: String) throws -> CGImage {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          CGImageSourceGetCount(src) > 0 else { throw Fail(msg: "cannot read image: \(path)") }
    let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any] ?? [:]
    let w = props[kCGImagePropertyPixelWidth] as? Int ?? 0, h = props[kCGImagePropertyPixelHeight] as? Int ?? 0
    guard w > 0, h > 0 else { throw Fail(msg: "image has no pixels: \(path)") }
    // The thumbnail path is the one that applies the orientation; at full size it does not resample.
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                 kCGImageSourceCreateThumbnailWithTransform: true,
                                 kCGImageSourceThumbnailMaxPixelSize: max(w, h),
                                 kCGImageSourceShouldCacheImmediately: true]
    guard let img = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else {
        throw Fail(msg: "cannot decode image: \(path)")
    }
    return try draw(img, CGRect(x: 0, y: 0, width: img.width, height: img.height), img.width, img.height)
}

/// Draw `crop` of `img` (pixels, top left) into a W x H opaque sRGB canvas on white.
func draw(_ img: CGImage, _ crop: CGRect, _ W: Int, _ H: Int) throws -> CGImage {
    guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw Fail(msg: "cannot allocate a \(W)x\(H) canvas")
    }
    ctx.setFillColor(CGColor(srgbRed: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    ctx.interpolationQuality = (crop.width == CGFloat(W) && crop.height == CGFloat(H)) ? .none : .high
    let sx = CGFloat(W) / crop.width, sy = CGFloat(H) / crop.height
    // CG's origin is bottom left: place the whole image so that `crop` lands on the canvas.
    ctx.draw(img, in: CGRect(x: -crop.minX * sx, y: -(CGFloat(img.height) - crop.maxY) * sy,
                             width: CGFloat(img.width) * sx, height: CGFloat(img.height) * sy))
    guard let out = ctx.makeImage() else { throw Fail(msg: "cannot render the picture") }
    return out
}

func savePNG(_ img: CGImage, _ path: String) -> Bool {
    guard let dst = CGImageDestinationCreateWithURL(URL(fileURLWithPath: path) as CFURL, UTType.png.identifier as CFString, 1, nil) else { return false }
    CGImageDestinationAddImage(dst, img, nil)
    return CGImageDestinationFinalize(dst)
}

/// One float plane from a Vision pixel buffer (32-bit float, 16-bit half or 8-bit).
func plane(_ pb: CVPixelBuffer) throws -> (w: Int, h: Int, v: [Float]) {
    CVPixelBufferLockBaseAddress(pb, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(pb, .readOnly) }
    let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb), bpr = CVPixelBufferGetBytesPerRow(pb)
    let fmt = CVPixelBufferGetPixelFormatType(pb)
    guard let base = CVPixelBufferGetBaseAddress(pb) else { throw Fail(msg: "empty pixel buffer") }
    var out = [Float](repeating: 0, count: w * h)
    for y in 0..<h {
        let row = base.advanced(by: y * bpr)
        switch fmt {
        case kCVPixelFormatType_OneComponent32Float:
            let p = row.assumingMemoryBound(to: Float.self); for x in 0..<w { out[y * w + x] = p[x] }
        #if arch(arm64)
        case kCVPixelFormatType_OneComponent16Half:
            let p = row.assumingMemoryBound(to: Float16.self); for x in 0..<w { out[y * w + x] = Float(p[x]) }
        #endif
        case kCVPixelFormatType_OneComponent8:
            let p = row.assumingMemoryBound(to: UInt8.self); for x in 0..<w { out[y * w + x] = Float(p[x]) / 255 }
        default:
            throw Fail(msg: "unexpected pixel format \(fmt)")
        }
    }
    return (w, h, out)
}

// ------------------------------------------------------------------ geometry

/// Where the analysed image sits in the picture: a crop [x0, y0, x0+cw, y0+ch] enlarged to aw x ah.
struct Frame {
    let W: Int, H: Int, x0: Int, y0: Int, cw: Int, ch: Int, aw: Int, ah: Int
    /// Vision's normalised point (origin bottom left) to picture pixels (origin top left).
    func px(_ p: CGPoint) -> [Double] { [r2(Double(x0) + Double(p.x) * Double(cw)), r2(Double(y0) + (1 - Double(p.y)) * Double(ch))] }
    func box(_ r: CGRect) -> [Double] {
        [r2(Double(x0) + Double(r.minX) * Double(cw)), r2(Double(y0) + (1 - Double(r.maxY)) * Double(ch)),
         r2(Double(x0) + Double(r.maxX) * Double(cw)), r2(Double(y0) + (1 - Double(r.minY)) * Double(ch))]
    }
    /// A picture-pixel box to Vision's normalised rect in the analysed image.
    func normalised(_ b: [Double]) -> CGRect {
        let nx0 = (b[0] - Double(x0)) / Double(cw), nx1 = (b[2] - Double(x0)) / Double(cw)
        let ny0 = 1 - (b[3] - Double(y0)) / Double(ch), ny1 = 1 - (b[1] - Double(y0)) / Double(ch)
        return CGRect(x: nx0, y: ny0, width: nx1 - nx0, height: ny1 - ny0)
    }

    /// A plane covering the analysed image, resampled (bilinear) to the picture: zero outside the crop.
    func toPicture(_ src: [Float], _ w: Int, _ h: Int) -> [Float] {
        var out = [Float](repeating: 0, count: W * H)
        let sx = Float(w) / Float(cw), sy = Float(h) / Float(ch)
        for Y in 0..<ch {
            let fy = max(0, min(Float(h - 1), (Float(Y) + 0.5) * sy - 0.5))
            let ya = Int(fy), yb = min(h - 1, ya + 1), ty = fy - Float(ya)
            let row = (Y + y0) * W + x0
            for X in 0..<cw {
                let fx = max(0, min(Float(w - 1), (Float(X) + 0.5) * sx - 0.5))
                let xa = Int(fx), xb = min(w - 1, xa + 1), tx = fx - Float(xa)
                let top = src[ya * w + xa] * (1 - tx) + src[ya * w + xb] * tx
                let bottom = src[yb * w + xa] * (1 - tx) + src[yb * w + xb] * tx
                out[row + X] = top * (1 - ty) + bottom * ty
            }
        }
        return out
    }

    func writeGray(_ v: [Float], _ path: String) throws {
        var bytes = [UInt8](repeating: 0, count: W * H)
        for i in 0..<(W * H) { bytes[i] = UInt8(max(0, min(255, (v[i] * 255).rounded()))) }
        guard let provider = CGDataProvider(data: Data(bytes) as CFData),
              let img = CGImage(width: W, height: H, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: W,
                                space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                                provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent),
              savePNG(img, path) else { throw Fail(msg: "cannot write \(path)") }
    }

    /// Box (>= 0.5), coverage-weighted area and the count of soft-edge pixels of a picture-size mask.
    func stats(_ m: [Float]) -> [String: Any] {
        var x0 = W, y0 = H, x1 = -1, y1 = -1, area = 0.0, soft = 0
        for y in 0..<H {
            for x in 0..<W {
                let v = m[y * W + x]
                if v >= 0.5 { x0 = min(x0, x); y0 = min(y0, y); x1 = max(x1, x); y1 = max(y1, y) }
                area += Double(v)
                if v > 0.02 && v < 0.98 { soft += 1 }
            }
        }
        return ["bbox": x1 < 0 ? [] : [x0, y0, x1 + 1, y1 + 1], "area": Int(area),
                "share": (area / Double(W * H) * 10000).rounded() / 10000, "soft_edge_px": soft]
    }
}

// ------------------------------------------------------------------ analyses

@available(macOS 14.0, *)
func instanceMasks(_ obs: VNInstanceMaskObservation, _ handler: VNImageRequestHandler, _ f: Frame, _ out: String, _ stem: String) throws -> [[String: Any]] {
    var list: [[String: Any]] = []
    for i in obs.allInstances {
        let pb = try obs.generateScaledMaskForImage(forInstances: IndexSet(integer: i), from: handler)
        let (w, h, v) = try plane(pb)
        let m = f.toPicture(v, w, h)
        let name = "\(stem)-\(list.count + 1).png"
        try f.writeGray(m, out + "/" + name)
        var e = f.stats(m)
        e["mask"] = name
        list.append(e)
    }
    return list
}

func subjects(_ handler: VNImageRequestHandler, _ f: Frame, _ out: String) throws -> [String: Any] {
    guard #available(macOS 14.0, *) else { throw Fail(msg: "subject masks need macOS 14 or later") }
    let req = VNGenerateForegroundInstanceMaskRequest()
    try handler.perform([req])
    guard let obs = req.results?.first else { return ["instances": [] as [Any]] }     // nothing stands out
    return ["instances": try instanceMasks(obs, handler, f, out, "subject"), "confidence": r2(Double(obs.confidence))]
}

func persons(_ handler: VNImageRequestHandler, _ f: Frame, _ out: String) throws -> [String: Any] {
    guard #available(macOS 14.0, *) else { throw Fail(msg: "person masks need macOS 14 or later") }
    let req = VNGeneratePersonInstanceMaskRequest()
    try handler.perform([req])
    guard let obs = req.results?.first, !obs.allInstances.isEmpty else { return ["instances": [] as [Any]] }
    var res: [String: Any] = ["instances": try instanceMasks(obs, handler, f, out, "person"), "confidence": r2(Double(obs.confidence))]
    // The segmentation matte is finer at hair and fingers than the instance masks, but merges everyone.
    let seg = VNGeneratePersonSegmentationRequest()
    seg.qualityLevel = .accurate
    seg.outputPixelFormat = kCVPixelFormatType_OneComponent8
    try handler.perform([seg])
    if let o = seg.results?.first {
        let (w, h, v) = try plane(o.pixelBuffer)
        let m = f.toPicture(v, w, h)
        try f.writeGray(m, out + "/persons-matte.png")
        var e = f.stats(m)
        e["mask"] = "persons-matte.png"
        res["matte"] = e
    }
    return res
}

let faceRegions: [(String, (VNFaceLandmarks2D) -> VNFaceLandmarkRegion2D?)] = [
    ("left_eye", { $0.leftEye }), ("right_eye", { $0.rightEye }), ("left_pupil", { $0.leftPupil }), ("right_pupil", { $0.rightPupil }),
    ("left_eyebrow", { $0.leftEyebrow }), ("right_eyebrow", { $0.rightEyebrow }), ("nose", { $0.nose }), ("nose_crest", { $0.noseCrest }),
    ("median_line", { $0.medianLine }), ("outer_lips", { $0.outerLips }), ("inner_lips", { $0.innerLips }), ("face_contour", { $0.faceContour })]

func faces(_ handler: VNImageRequestHandler, _ f: Frame, given: [[Double]]) throws -> [[String: Any]] {
    let rects = VNDetectFaceRectanglesRequest()
    try handler.perform([rects])
    let found = rects.results ?? []
    let mine = given.map { VNFaceObservation(boundingBox: f.normalised($0)) }
    let all = found + mine
    if all.isEmpty { return [] }
    let marks = VNDetectFaceLandmarksRequest()
    marks.inputFaceObservations = all
    try handler.perform([marks])
    let size = CGSize(width: f.aw, height: f.ah)
    let kx = Double(f.cw) / Double(f.aw), ky = Double(f.ch) / Double(f.ah)
    func near(_ a: CGRect, _ b: CGRect) -> Double { Double(abs(a.midX - b.midX) + abs(a.midY - b.midY) + abs(a.width - b.width)) }
    var list: [[String: Any]] = []
    for o in marks.results ?? [] {
        // Match each answer back to the face it was asked about (detected or given).
        let i = all.indices.min { near(all[$0].boundingBox, o.boundingBox) < near(all[$1].boundingBox, o.boundingBox) } ?? 0
        var e: [String: Any] = ["box": f.box(o.boundingBox), "given": i >= found.count]
        if i < found.count {
            e["confidence"] = r2(Double(found[i].confidence))
            // Vision's angles (radians in its own bottom-left frame), as degrees.
            if let v = found[i].roll { e["roll_deg"] = r2(v.doubleValue * 180 / .pi) }
            if let v = found[i].yaw { e["yaw_deg"] = r2(v.doubleValue * 180 / .pi) }
            if let v = found[i].pitch { e["pitch_deg"] = r2(v.doubleValue * 180 / .pi) }
        }
        if let L = o.landmarks {
            e["landmarks_confidence"] = r2(Double(L.confidence))
            var pts: [String: Any] = [:]
            for (name, get) in faceRegions {
                guard let reg = get(L) else { continue }
                pts[name] = reg.pointsInImage(imageSize: size).map { [r2(Double(f.x0) + Double($0.x) * kx), r2(Double(f.y0) + (Double(f.ah) - Double($0.y)) * ky)] }
            }
            e["landmarks"] = pts
        }
        list.append(e)
    }
    return list
}

let humanJoints: [(VNHumanBodyPoseObservation.JointName, String)] = [
    (.nose, "nose"), (.leftEye, "left_eye"), (.rightEye, "right_eye"), (.leftEar, "left_ear"), (.rightEar, "right_ear"),
    (.neck, "neck"), (.leftShoulder, "left_shoulder"), (.rightShoulder, "right_shoulder"), (.leftElbow, "left_elbow"),
    (.rightElbow, "right_elbow"), (.leftWrist, "left_wrist"), (.rightWrist, "right_wrist"), (.root, "root"),
    (.leftHip, "left_hip"), (.rightHip, "right_hip"), (.leftKnee, "left_knee"), (.rightKnee, "right_knee"),
    (.leftAnkle, "left_ankle"), (.rightAnkle, "right_ankle")]

func pose(_ handler: VNImageRequestHandler, _ f: Frame) throws -> [String: Any] {
    var res: [String: Any] = [:]
    let body = VNDetectHumanBodyPoseRequest()
    try handler.perform([body])
    res["humans"] = (body.results ?? []).map { o -> [String: Any] in
        var j: [String: Any] = [:]
        for (key, name) in humanJoints {
            if let p = try? o.recognizedPoint(key), p.confidence > 0 { j[name] = f.px(p.location) + [r2(Double(p.confidence))] }
        }
        return ["confidence": r2(Double(o.confidence)), "joints": j]
    }
    if #available(macOS 14.0, *) {
        let animal = VNDetectAnimalBodyPoseRequest()
        try handler.perform([animal])
        let names: [(VNAnimalBodyPoseObservation.JointName, String)] = [
            (.leftEarTop, "left_ear_top"), (.rightEarTop, "right_ear_top"), (.leftEarMiddle, "left_ear_middle"),
            (.rightEarMiddle, "right_ear_middle"), (.leftEarBottom, "left_ear_bottom"), (.rightEarBottom, "right_ear_bottom"),
            (.leftEye, "left_eye"), (.rightEye, "right_eye"), (.nose, "nose"), (.neck, "neck"),
            (.leftFrontElbow, "left_front_elbow"), (.rightFrontElbow, "right_front_elbow"), (.leftFrontKnee, "left_front_knee"),
            (.rightFrontKnee, "right_front_knee"), (.leftFrontPaw, "left_front_paw"), (.rightFrontPaw, "right_front_paw"),
            (.leftBackElbow, "left_back_elbow"), (.rightBackElbow, "right_back_elbow"), (.leftBackKnee, "left_back_knee"),
            (.rightBackKnee, "right_back_knee"), (.leftBackPaw, "left_back_paw"), (.rightBackPaw, "right_back_paw"),
            (.tailTop, "tail_top"), (.tailMiddle, "tail_middle"), (.tailBottom, "tail_bottom")]
        res["animals"] = (animal.results ?? []).map { o -> [String: Any] in
            var j: [String: Any] = [:]
            for (key, name) in names {
                if let p = try? o.recognizedPoint(key), p.confidence > 0 { j[name] = f.px(p.location) + [r2(Double(p.confidence))] }
            }
            return ["confidence": r2(Double(o.confidence)), "joints": j]
        }
    } else {
        res["animals_error"] = "animal pose needs macOS 14 or later"
    }
    // Only cats and dogs are known to this classifier; boxes help to read the animal pose.
    let kinds = VNRecognizeAnimalsRequest()
    try handler.perform([kinds])
    res["animal_labels"] = (kinds.results ?? []).map {
        ["box": f.box($0.boundingBox), "label": $0.labels.first?.identifier ?? "", "confidence": r2(Double($0.labels.first?.confidence ?? 0))] as [String: Any]
    }
    return res
}

func saliency(_ handler: VNImageRequestHandler, _ f: Frame, _ out: String) throws -> [String: Any] {
    var res: [String: Any] = [:]
    for (name, req) in [("attention", VNGenerateAttentionBasedSaliencyImageRequest() as VNImageBasedRequest),
                        ("objectness", VNGenerateObjectnessBasedSaliencyImageRequest())] {
        try handler.perform([req])
        guard let o = (req.results as? [VNSaliencyImageObservation])?.first else { res[name] = ["objects": [] as [Any]]; continue }
        let (w, h, v) = try plane(o.pixelBuffer)
        try f.writeGray(f.toPicture(v, w, h), out + "/\(name).png")
        res[name] = ["heatmap": "\(name).png", "grid": [w, h], "max": r2(Double(v.max() ?? 0)),
                     "objects": (o.salientObjects ?? []).map { ["box": f.box($0.boundingBox), "confidence": r2(Double($0.confidence))] as [String: Any] }]
    }
    return res
}

func text(_ handler: VNImageRequestHandler, _ f: Frame, langs: [String]) throws -> [String: Any] {
    let read = VNRecognizeTextRequest()
    read.recognitionLevel = .accurate
    read.usesLanguageCorrection = true
    if !langs.isEmpty { read.recognitionLanguages = langs }
    else if #available(macOS 13.0, *) { read.automaticallyDetectsLanguage = true }
    // Text-like marks even where nothing can be read: a scrawled signature, a stamp, half-hidden lettering.
    let marks = VNDetectTextRectanglesRequest()
    marks.reportCharacterBoxes = true
    try handler.perform([read, marks])
    let lines = (read.results ?? []).compactMap { o -> [String: Any]? in
        guard let c = o.topCandidates(1).first else { return nil }
        var e: [String: Any] = ["string": c.string, "confidence": r2(Double(c.confidence)), "box": f.box(o.boundingBox)]
        if let q = try? c.boundingBox(for: c.string.startIndex..<c.string.endIndex) {
            e["quad"] = [f.px(q.topLeft), f.px(q.topRight), f.px(q.bottomRight), f.px(q.bottomLeft)]
        }
        return e
    }
    let regions = (marks.results ?? []).map {
        ["box": f.box($0.boundingBox), "confidence": r2(Double($0.confidence)), "chars": $0.characterBoxes?.count ?? 0] as [String: Any]
    }
    return ["lines": lines, "marks": regions]
}

// ------------------------------------------------------------------ main

func capabilities() -> [String: Any] {
    var req: [String: Bool] = ["faces": true, "pose": true, "saliency": true, "text": true]
    if #available(macOS 14.0, *) { req["subjects"] = true; req["persons"] = true; req["animal_pose"] = true }
    else { req["subjects"] = false; req["persons"] = false; req["animal_pose"] = false }
    return ["tool": "lpvision", "version": 1, "macos": ProcessInfo.processInfo.operatingSystemVersionString, "requests": req]
}

func main() -> Int32 {
    var args = Array(CommandLine.arguments.dropFirst())
    if args.first == "check" { emit(capabilities()); return 0 }
    if args.isEmpty || args.contains("--help") || args.contains("-h") { FileHandle.standardError.write(Data((usage + "\n").utf8)); return args.isEmpty ? 2 : 0 }
    var outDir: String?, box: [Double]?, zoom: Double?, langs: [String] = [], saveInput: String?
    var given: [[Double]] = [], positional: [String] = []
    func nums(_ n: Int, _ flag: String) throws -> [Double] {
        guard args.count >= n else { throw Fail(msg: "\(flag) needs \(n) numbers") }
        let v = args.prefix(n).compactMap { Double($0) }
        guard v.count == n else { throw Fail(msg: "\(flag) needs \(n) numbers") }
        args.removeFirst(n)
        return v
    }
    do {
        while !args.isEmpty {
            let a = args.removeFirst()
            switch a {
            case "--out": guard !args.isEmpty else { throw Fail(msg: "--out needs a folder") }; outDir = args.removeFirst()
            case "--box": box = try nums(4, a)
            case "--face": given.append(try nums(4, a))
            case "--zoom": zoom = try nums(1, a)[0]
            case "--langs": guard !args.isEmpty else { throw Fail(msg: "--langs needs a list") }; langs = args.removeFirst().split(separator: ",").map(String.init)
            case "--save-input": guard !args.isEmpty else { throw Fail(msg: "--save-input needs a file") }; saveInput = args.removeFirst()
            default:
                if a.hasPrefix("--") { throw Fail(msg: "unknown option \(a)") }
                positional.append(a)
            }
        }
        guard positional.count == 2 else { throw Fail(msg: "need <what> and <image>") }
        guard let out = outDir else { throw Fail(msg: "need --out <dir>") }
        let known = ["subjects", "persons", "faces", "pose", "saliency", "text"]
        var what = positional[0].split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        if what == ["all"] { what = known }
        if let bad = what.first(where: { !known.contains($0) }) { throw Fail(msg: "unknown analysis '\(bad)' (have \(known.joined(separator: ", ")))") }
        try FileManager.default.createDirectory(atPath: out, withIntermediateDirectories: true)

        let t0 = Date()
        let picture = try loadPicture(positional[1])
        let W = picture.width, H = picture.height
        var x0 = 0, y0 = 0, x1 = W, y1 = H
        if let b = box {
            x0 = max(0, Int(b[0].rounded(.down))); y0 = max(0, Int(b[1].rounded(.down)))
            x1 = min(W, Int(b[2].rounded(.up))); y1 = min(H, Int(b[3].rounded(.up)))
            guard x1 - x0 >= 8, y1 - y0 >= 8 else { throw Fail(msg: "--box is empty or outside the picture (\(W)x\(H))") }
        }
        let cw = x1 - x0, ch = y1 - y0
        // A region is enlarged until its longer side is about 1280 px (at most 4x): the face and
        // pose models miss faces a few dozen pixels high. The whole picture is analysed as it is.
        let k = zoom ?? (box == nil ? 1 : min(4, max(1, 1280 / Double(max(cw, ch)))))
        guard k > 0, k <= 8 else { throw Fail(msg: "--zoom must be in (0, 8]") }
        let aw = max(1, Int((Double(cw) * k).rounded())), ah = max(1, Int((Double(ch) * k).rounded()))
        var analysed = picture
        if cw != W || ch != H || aw != W || ah != H { analysed = try draw(picture, CGRect(x: x0, y: y0, width: cw, height: ch), aw, ah) }
        if let s = saveInput, !savePNG(analysed, s) { throw Fail(msg: "cannot write \(s)") }
        let f = Frame(W: W, H: H, x0: x0, y0: y0, cw: cw, ch: ch, aw: aw, ah: ah)
        let handler = VNImageRequestHandler(cgImage: analysed, options: [:])
        var res = capabilities()
        res["image"] = positional[1]
        res["size"] = [W, H]
        res["analysed"] = ["box": [x0, y0, x1, y1], "zoom": r2(k), "pixels": [aw, ah], "flattened_on": "white"]
        var timings: [String: Int] = ["load": ms(t0)]
        for w in what {
            let t = Date()
            do {
                switch w {
                case "subjects": res[w] = try subjects(handler, f, out)
                case "persons": res[w] = try persons(handler, f, out)
                case "faces": res[w] = try faces(handler, f, given: given)
                case "pose": res[w] = try pose(handler, f)
                case "saliency": res[w] = try saliency(handler, f, out)
                default: res[w] = try text(handler, f, langs: langs)
                }
            } catch let e as Fail {
                res[w] = ["error": e.msg]
            } catch {
                res[w] = ["error": "\(error.localizedDescription)"]
            }
            timings[w] = ms(t)
        }
        res["timings_ms"] = timings
        emit(res)
        return 0
    } catch let e as Fail {
        emit(["error": e.msg, "usage": usage])
        return 2
    } catch {
        emit(["error": "\(error.localizedDescription)"])
        return 2
    }
}

exit(main())
