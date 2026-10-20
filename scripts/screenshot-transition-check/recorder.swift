import AppKit
import CoreMedia
import CoreVideo
import Darwin
import QuartzCore
import ScreenCaptureKit

func writeJSON(_ value: [String: Any], to path: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: path, options: .atomic)
}

struct StoredFrame {
    let bytes: Data
    let width: Int
    let height: Int
    let stride: Int
    var metadata: [String: Any]

    func rgb(_ x: Int, _ y: Int) -> [Double] {
        let offset = y * stride + x * 4
        guard offset >= 0, offset + 3 < bytes.count else { return [] }
        return [Double(bytes[offset + 2]) / 255, Double(bytes[offset + 1]) / 255, Double(bytes[offset]) / 255]
    }

    func patternVisible() -> Bool {
        let points = [width / 6, width / 2, width * 5 / 6].map { rgb($0, height / 4) }
        guard points.allSatisfy({ $0.count == 3 }) else { return false }
        return points[0][0] > 0.6 && points[0][1] < 0.3 && points[0][2] < 0.4 &&
            points[1][2] > 0.6 && points[1][0] < 0.3 && points[1][1] < 0.4 &&
            points[2][1] > 0.5 && points[2][0] < 0.3 && points[2][2] < 0.4
    }

    func png() throws -> Data {
        guard let provider = CGDataProvider(data: bytes as CFData),
              let space = CGColorSpace(name: CGColorSpace.sRGB),
              let image = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
                                  bytesPerRow: stride, space: space,
                                  bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                                  provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent),
              let data = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
            throw NSError(domain: "frame-png", code: 1)
        }
        return data
    }
}

struct SpooledFrame {
    let path: URL
    let width: Int
    let height: Int
    let stride: Int
    var metadata: [String: Any]
}

final class FrameStore: NSObject, SCStreamOutput, SCStreamDelegate {
    let queue = DispatchQueue(label: "cap-transition-frames")
    private let lock = NSLock()
    private var frames: [SpooledFrame] = []
    private var lastFrame: StoredFrame?
    private var lastSpool: SpooledFrame?
    private let directory: URL
    private var statuses: [String: Int] = [:]
    private var errors: [String] = []
    private var unusableEvents: [[String: Any]] = []
    private var writtenBytes: Int64 = 0
    private var sequence = 0
    private let frameByteLimit = 64 * 1024 * 1024
    private let diskByteLimit: Int64 = 4 * 1024 * 1024 * 1024

    init(directory: URL) {
        self.directory = directory
        super.init()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock()
        errors.append(error.localizedDescription)
        lock.unlock()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .screen else { return }
        lock.lock()
        defer { lock.unlock() }
        guard errors.isEmpty else { return }
        guard frames.count < 2400 else {
            errors.append("Recording exceeded its event budget; no frames were discarded")
            return
        }
        guard sampleBuffer.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let first = attachments.first, let raw = first[.status] as? Int,
              let status = SCFrameStatus(rawValue: raw) else {
            errors.append("Missing valid sample metadata")
            return
        }
        statuses[String(raw), default: 0] += 1
        if status != .complete && status != .idle {
            unusableEvents.append(["utc": Date().timeIntervalSince1970, "status": raw])
            return
        }
        let hostBefore = CMClockGetTime(CMClockGetHostTimeClock()).seconds
        let arrivalUTC = Date().timeIntervalSince1970
        let hostAfter = CMClockGetTime(CMClockGetHostTimeClock()).seconds
        guard let displayTime = first[.displayTime] as? UInt64, displayTime > 0 else {
            errors.append("Missing ScreenCaptureKit display timestamp for status \(raw)")
            return
        }
        let presentation = CMClockMakeHostTimeFromSystemUnits(displayTime).seconds
        guard presentation.isFinite, hostBefore.isFinite, hostAfter.isFinite else {
            errors.append("Nonfinite sample or host clock")
            return
        }
        let age = (hostBefore + hostAfter) / 2 - presentation
        guard age >= -0.05, age < 2, hostAfter - hostBefore < 0.01 else {
            errors.append("Display timestamp could not be aligned to the host clock: status=\(raw), age=\(age), anchorSpan=\(hostAfter - hostBefore)")
            return
        }
        var frame: SpooledFrame
        if status == .complete {
            guard let buffer = sampleBuffer.imageBuffer,
                  CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_32BGRA,
                  CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
                errors.append("Missing BGRA buffer or buffer lock failed")
                return
            }
            let width = CVPixelBufferGetWidth(buffer)
            let height = CVPixelBufferGetHeight(buffer)
            let stride = CVPixelBufferGetBytesPerRow(buffer)
            guard width > 0, height > 0, stride >= width * 4, height <= frameByteLimit / stride,
                  let base = CVPixelBufferGetBaseAddress(buffer) else {
                _ = CVPixelBufferUnlockBaseAddress(buffer, .readOnly)
                errors.append("Frame exceeds the diagnostic memory budget or lacks pixel data")
                return
            }
            let pixels = StoredFrame(bytes: Data(bytes: base, count: stride * height), width: width, height: height, stride: stride, metadata: [:])
            guard CVPixelBufferUnlockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
                errors.append("Cannot unlock copied capture buffer")
                return
            }
            guard writtenBytes + Int64(pixels.bytes.count) <= diskByteLimit else {
                errors.append("Recording exceeded its 4 GiB raw-frame disk budget; no frames were discarded")
                return
            }
            let path = directory.appendingPathComponent(String(format: "%04d.bgra", sequence))
            do { try pixels.bytes.write(to: path, options: .withoutOverwriting) } catch {
                errors.append("Cannot spool captured pixels: \(error.localizedDescription)")
                return
            }
            writtenBytes += Int64(pixels.bytes.count)
            lastFrame = pixels
            frame = SpooledFrame(path: path, width: width, height: height, stride: stride, metadata: [:])
            lastSpool = frame
        } else if status == .idle, let previous = lastSpool {
            frame = previous
        } else {
            unusableEvents.append(["utc": arrivalUTC, "status": raw])
            return
        }
        frame.metadata = ["sequence": sequence, "utc": arrivalUTC - age, "arrivalUTC": arrivalUTC,
                          "presentationHostSeconds": presentation, "clockAgeMs": age * 1000,
                          "displayTimeSystemUnits": displayTime,
                          "samplePTSSeconds": sampleBuffer.presentationTimeStamp.seconds.isFinite ? sampleBuffer.presentationTimeStamp.seconds as Any : NSNull(),
                          "anchorSpanMs": (hostAfter - hostBefore) * 1000,
                          "freshPixels": status == .complete, "status": raw]
        sequence += 1
        frames.append(frame)
    }

    func latest() -> StoredFrame? {
        lock.lock()
        defer { lock.unlock() }
        return lastFrame
    }

    func snapshot() -> ([SpooledFrame], [String: Any]) {
        lock.lock()
        defer { lock.unlock() }
        return (frames, ["statuses": statuses, "errors": errors, "unusableEvents": unusableEvents, "evictedEvents": 0,
                         "retainedEvents": frames.count, "requestedFPS": 60,
                         "maximumEvents": 2400, "maximumRawDiskBytes": diskByteLimit, "writtenRawBytes": writtenBytes,
                         "maximumFrameBytes": frameByteLimit, "storage": "disk spool without eviction",
                         "timeSource": "SCStreamFrameInfo.displayTime converted from system units to host time and aligned to UTC"])
    }
}

@MainActor
final class RecorderApp: NSObject, NSApplicationDelegate {
    let output: URL
    let capture: URL?
    let selfTest: Bool
    private let store: FrameStore
    private var stream: SCStream?
    private var window: NSPanel?
    private var heartbeat: NSPanel?
    private var heartbeatTimer: Timer?
    private var heartbeatSequence = 0
    private var observedHeartbeatValues: Set<Int> = []
    private var middle: NSView?
    private var timer: Timer?
    private var readyAt: Double?
    private var gpuSeenAt: Double?
    private var finishing = false
    private var phase = 0
    private var phaseEvents: [[String: Any]] = []
    private var geometry: [String: Any] = [:]
    private var editorHistory: [[String: Any]] = []

    init(output: URL, capture: URL?, selfTest: Bool) {
        self.output = output
        self.capture = capture
        self.selfTest = selfTest
        self.store = FrameStore(directory: output.appendingPathComponent("raw"))
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            do { try await start() } catch { finish(error: error.localizedDescription) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { [self] in
            finish(error: "Recorder exceeded its 20-second capture deadline")
        }
    }

    private func start() async throws {
        try FileManager.default.createDirectory(at: output.appendingPathComponent("raw"), withIntermediateDirectories: true)
        let displayID = CGMainDisplayID()
        guard let screen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == displayID
        }), screen.backingScaleFactor == 1 else {
            throw NSError(domain: "transition-requires-scale-one-display", code: 1)
        }
        let panel = NSPanel(contentRect: screen.frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.level = NSWindow.Level(rawValue: NSWindow.Level.normal.rawValue - 1)
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = true
        panel.isReleasedWhenClosed = false
        panel.title = "Cap transition background pattern"
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary]
        let root = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        panel.contentView = root
        let colors = [CGColor(red: 0.9, green: 0.05, blue: 0.12, alpha: 1),
                      CGColor(red: 0.05, green: 0.18, blue: 0.9, alpha: 1),
                      CGColor(red: 0.05, green: 0.8, blue: 0.2, alpha: 1)]
        for (index, color) in colors.enumerated() {
            let width = root.bounds.width / 3
            let view = NSView(frame: NSRect(x: CGFloat(index) * width, y: 0, width: width, height: root.bounds.height))
            view.wantsLayer = true
            view.layer?.backgroundColor = color
            root.addSubview(view)
            if index == 1 { middle = view }
        }
        for index in 0..<8 {
            let view = NSView(frame: NSRect(x: root.bounds.width * CGFloat(index) / 8,
                                          y: root.bounds.height * 0.2, width: root.bounds.width / 8,
                                          height: root.bounds.height * 0.12))
            view.wantsLayer = true
            let value = CGFloat(index) / 7
            view.layer?.backgroundColor = CGColor(gray: value, alpha: 1)
            root.addSubview(view)
        }
        window = panel
        panel.orderFrontRegardless()
        let marker = NSPanel(contentRect: NSRect(x: screen.frame.minX, y: screen.frame.maxY - 8, width: 8, height: 8),
                             styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        marker.level = NSWindow.Level(rawValue: NSWindow.Level.statusBar.rawValue + 1)
        marker.hidesOnDeactivate = false
        marker.hasShadow = false
        marker.ignoresMouseEvents = true
        marker.isReleasedWhenClosed = false
        marker.collectionBehavior = [.canJoinAllSpaces, .stationary]
        marker.contentView?.wantsLayer = true
        marker.orderFrontRegardless()
        heartbeat = marker
        heartbeatTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [self] _ in
            MainActor.assumeIsolated {
                heartbeatSequence += 1
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                marker.contentView?.layer?.backgroundColor = CGColor(gray: CGFloat(heartbeatSequence % 251) / 250, alpha: 1)
                CATransaction.commit()
            }
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
            throw NSError(domain: "transition-display-missing", code: 1)
        }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = CGDisplayPixelsWide(displayID)
        configuration.height = CGDisplayPixelsHigh(displayID)
        configuration.pixelFormat = kCVPixelFormatType_32BGRA
        configuration.colorSpaceName = CGColorSpace.sRGB
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 60)
        configuration.queueDepth = 3
        configuration.showsCursor = false
        configuration.capturesAudio = false
        geometry = ["width": configuration.width, "height": configuration.height, "scale": screen.backingScaleFactor,
                    "displayOriginX": CGDisplayBounds(displayID).origin.x, "displayOriginY": CGDisplayBounds(displayID).origin.y,
                    "heartbeatRect": [0, 0, 8, 8], "heartbeatWindowNumber": marker.windowNumber,
                    "backgroundWindowNumber": panel.windowNumber, "backgroundLevel": panel.level.rawValue]
        let stream = SCStream(filter: filter, configuration: configuration, delegate: store)
        self.stream = stream
        try stream.addStreamOutput(store, type: .screen, sampleHandlerQueue: store.queue)
        try await stream.startCapture()
        timer = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { [self] _ in
            MainActor.assumeIsolated { tick() }
        }
    }

    private func tick() {
        guard !finishing else { return }
        let now = Date().timeIntervalSince1970
        do {
            if readyAt == nil, let frame = store.latest(), frame.patternVisible() {
                let sample = frame.rgb(4, 4)
                if sample.count == 3 && abs(sample[0] - sample[1]) < 0.02 && abs(sample[1] - sample[2]) < 0.02 {
                    observedHeartbeatValues.insert(Int((sample[0] * 255).rounded()))
                }
                if observedHeartbeatValues.count >= 3 {
                    geometry["observedHeartbeatValues"] = observedHeartbeatValues.count
                    readyAt = now
                    phaseEvents.append(["name": "blue", "utc": now])
                    try writeJSON(["utc": now, "pid": getpid(), "geometry": geometry, "patternVisible": true],
                                  to: output.appendingPathComponent("ready.json"))
                }
            }
            if selfTest, let readyAt {
                if phase == 0 && now - readyAt > 0.3 {
                    middle?.layer?.backgroundColor = CGColor(red: 0.05, green: 0.8, blue: 0.2, alpha: 1)
                    phase = 1
                    phaseEvents.append(["name": "green", "utc": now])
                } else if phase == 1 && now - readyAt > 4.0 {
                    middle?.layer?.backgroundColor = CGColor(red: 0.05, green: 0.18, blue: 0.9, alpha: 1)
                    phase = 2
                    phaseEvents.append(["name": "blue-again", "utc": now])
                } else if phase == 2 && now - readyAt > 4.6 {
                    finish(error: nil)
                }
            }
            if let capture {
                try observeEditor(capture, now: now)
                if FileManager.default.fileExists(atPath: capture.appendingPathComponent("gpu-drawn").path) {
                    if gpuSeenAt == nil { gpuSeenAt = now }
                    if let gpuSeenAt, now - gpuSeenAt > 0.75 { finish(error: nil) }
                }
            }
        } catch { finish(error: error.localizedDescription) }
    }

    private func observeEditor(_ capture: URL, now: Double) throws {
        let request = capture.appendingPathComponent("desktop-request.json")
        let log = capture.appendingPathComponent("cap.log")
        guard FileManager.default.fileExists(atPath: request.path), FileManager.default.fileExists(atPath: log.path),
              let identity = try JSONSerialization.jsonObject(with: Data(contentsOf: request)) as? [String: Any],
              let pid = (identity["ownerPID"] as? NSNumber)?.int32Value else { return }
        let text = String(decoding: try Data(contentsOf: log), as: UTF8.self)
        let expression = try NSRegularExpression(pattern: "screenshot editor window opened number=([0-9]+)")
        guard let match = expression.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
              let range = Range(match.range(at: 1), in: text), let number = UInt32(text[range]) else { return }
        guard let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]],
              let editor = windows.first(where: {
                  ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid &&
                  ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == number
              }), let bounds = editor[kCGWindowBounds as String] as? [String: Any] else { return }
        var row: [String: Any] = ["utc": now, "pid": pid, "windowNumber": number, "bounds": bounds,
                                  "alpha": editor[kCGWindowAlpha as String] ?? NSNull()]
        if let previous = editorHistory.last, NSDictionary(dictionary: previous["bounds"] as? [String: Any] ?? [:]).isEqual(to: bounds) {
            return
        }
        row["title"] = editor[kCGWindowName as String] ?? ""
        editorHistory.append(row)
    }

    private func finish(error: String?) {
        guard !finishing else { return }
        finishing = true
        timer?.invalidate()
        heartbeatTimer?.invalidate()
        Task { @MainActor in
            var failure = error
            if let stream {
                do { try await stream.stopCapture() } catch { failure = failure ?? error.localizedDescription }
            }
            store.queue.sync {}
            let (frames, details) = store.snapshot()
            do {
                let folder = output.appendingPathComponent("frames")
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                var rows: [[String: Any]] = []
                var encoded: [URL: String] = [:]
                for frame in frames {
                    let name: String
                    if let existing = encoded[frame.path] {
                        name = existing
                    } else {
                        name = "frames/\(frame.path.deletingPathExtension().lastPathComponent).png"
                        let pixels = StoredFrame(bytes: try Data(contentsOf: frame.path), width: frame.width,
                                                 height: frame.height, stride: frame.stride, metadata: [:])
                        try pixels.png().write(to: output.appendingPathComponent(name))
                        encoded[frame.path] = name
                        try FileManager.default.removeItem(at: frame.path)
                    }
                    var metadata = frame.metadata
                    metadata["file"] = name
                    metadata["width"] = frame.width
                    metadata["height"] = frame.height
                    rows.append(metadata)
                }
                var report: [String: Any] = ["completed": failure == nil, "selfTest": selfTest,
                    "purpose": "Untimed visual evidence; no performance claim or physical-display guarantee",
                    "geometry": geometry, "stream": details, "frames": rows,
                    "phaseEvents": phaseEvents, "editorHistory": editorHistory,
                    "readyUTC": readyAt.map { $0 as Any } ?? NSNull()]
                if let failure { report["error"] = failure }
                try writeJSON(report, to: output.appendingPathComponent("recording.json"))
            } catch {
                fputs("Cannot save transition evidence: \(error)\n", stderr)
                exit(2)
            }
            window?.orderOut(nil)
            heartbeat?.orderOut(nil)
            exit(failure == nil ? 0 : 1)
        }
    }
}

@main
struct Main {
    @MainActor static func main() {
        let args = CommandLine.arguments
        guard (args.count == 3 && args[1] == "--self-test") || (args.count == 4 && args[1] == "--record") else { exit(2) }
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let delegate = RecorderApp(output: URL(fileURLWithPath: args[2], isDirectory: true),
                                   capture: args.count == 4 ? URL(fileURLWithPath: args[3], isDirectory: true) : nil,
                                   selfTest: args[1] == "--self-test")
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
