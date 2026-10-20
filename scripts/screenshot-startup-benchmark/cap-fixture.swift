import AppKit
import CryptoKit
import Darwin
import ScreenCaptureKit

let comparisonBorder = 16

func rawRGB(_ bitmap: NSBitmapImageRep, x: Int, y: Int) -> [Double] {
    guard bitmap.bitsPerSample == 8, bitmap.colorSpace.colorSpaceModel == .rgb,
          bitmap.samplesPerPixel == (bitmap.hasAlpha ? 4 : 3),
          !bitmap.bitmapFormat.contains(.alphaFirst) else { return [] }
    var pixel = [Int](repeating: 0, count: 4)
    pixel.withUnsafeMutableBufferPointer { bitmap.getPixel($0.baseAddress!, atX: x, y: y) }
    guard !bitmap.hasAlpha || pixel[3] == 255 else { return [] }
    return pixel.prefix(3).map { Double($0) / 255 }
}

func imageEvidence(_ bitmap: NSBitmapImageRep) -> [String: Any] {
    var counts = [0, 0, 0]
    var total = 0
    for y in stride(from: 0, to: bitmap.pixelsHigh, by: max(1, bitmap.pixelsHigh / 64)) {
        for x in stride(from: 0, to: bitmap.pixelsWide, by: max(1, bitmap.pixelsWide / 64)) {
            let rgb = rawRGB(bitmap, x: x, y: y)
            total += 1
            guard rgb.count == 3 else { continue }
            let r = rgb[0]
            let g = rgb[1]
            let b = rgb[2]
            if r > 0.6 && g < 0.3 && b < 0.4 { counts[0] += 1 }
            if g > 0.5 && r < 0.3 && b < 0.4 { counts[1] += 1 }
            if b > 0.6 && r < 0.3 && g < 0.4 { counts[2] += 1 }
        }
    }
    let ratios = counts.map { Double($0) / Double(max(1, total)) }
    let centers = [bitmap.pixelsWide / 6, bitmap.pixelsWide / 2, bitmap.pixelsWide * 5 / 6].map { x -> [Double] in
        rawRGB(bitmap, x: x, y: bitmap.pixelsHigh / 2)
    }
    let fixtureMatches = centers.allSatisfy { $0.count == 3 } &&
        centers[0][0] > 0.6 && centers[0][1] < 0.3 && centers[0][2] < 0.4 &&
        centers[1][2] > 0.6 && centers[1][0] < 0.3 && centers[1][1] < 0.4 &&
        centers[2][1] > 0.5 && centers[2][0] < 0.3 && centers[2][2] < 0.4
    let converted = [bitmap.pixelsWide / 6, bitmap.pixelsWide / 2, bitmap.pixelsWide * 5 / 6].map { x -> [Double] in
        guard let color = bitmap.colorAt(x: x, y: bitmap.pixelsHigh / 2)?.usingColorSpace(.deviceRGB) else { return [] }
        return [Double(color.redComponent), Double(color.greenComponent), Double(color.blueComponent)]
    }
    return ["width": bitmap.pixelsWide, "height": bitmap.pixelsHigh, "sampleRGB": centers,
            "rawRGBInspectable": centers.allSatisfy { $0.count == 3 },
            "validationSpace": "stored opaque 8-bit RGB samples", "convertedDeviceRGB": converted,
            "colorRatios": ratios, "fixtureMatches": fixtureMatches,
            "containsFixtureColors": ratios.allSatisfy { $0 > 0.05 }]
}

func pngEvidence(_ png: Data) throws -> [String: Any] {
    guard let bitmap = NSBitmapImageRep(data: png) else { throw NSError(domain: "png-decode", code: 1) }
    return imageEvidence(bitmap)
}

func compareImages(_ first: NSBitmapImageRep, _ second: NSBitmapImageRep) throws -> [String: Any] {
    guard first.pixelsWide == second.pixelsWide, first.pixelsHigh == second.pixelsHigh,
          first.pixelsWide > comparisonBorder * 2, first.pixelsHigh > comparisonBorder * 2 else {
        throw NSError(domain: "comparison-dimensions", code: 1)
    }
    var interiorDifferences = 0
    var exterior: [[String: Any]] = []
    var firstInterior = Data()
    var secondInterior = Data()
    for y in 0..<first.pixelsHigh {
        for x in 0..<first.pixelsWide {
            let a = rawRGB(first, x: x, y: y)
            let b = rawRGB(second, x: x, y: y)
            guard a.count == 3, b.count == 3 else { throw NSError(domain: "comparison-bitmap-format", code: 2) }
            let interior = x >= comparisonBorder && x < first.pixelsWide - comparisonBorder &&
                y >= comparisonBorder && y < first.pixelsHigh - comparisonBorder
            if interior {
                firstInterior.append(contentsOf: a.map { UInt8(($0 * 255).rounded()) })
                secondInterior.append(contentsOf: b.map { UInt8(($0 * 255).rounded()) })
                if a != b { interiorDifferences += 1 }
            } else if a != b {
                exterior.append(["x": x, "y": y, "baselineRGB": a, "revisedRGB": b])
            }
        }
    }
    func digest(_ data: Data) -> String { SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() }
    return ["width": first.pixelsWide, "height": first.pixelsHigh, "excludedBorderPixels": comparisonBorder,
            "interiorDifferentPixels": interiorDifferences, "interiorMatches": interiorDifferences == 0,
            "baselineInteriorSHA256": digest(firstInterior), "revisedInteriorSHA256": digest(secondInterior),
            "exteriorDifferentPixels": exterior.count, "exteriorDifferences": exterior]
}

func writeJSON(_ value: [String: Any], to path: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: path, options: .atomic)
}

@MainActor
final class Fixture: NSObject, NSApplicationDelegate {
    enum Mode: Equatable { case colored, desktop, testWindow }
    let directory: URL
    let mode: Mode
    private var window: NSWindow?
    private var timer: Timer?
    private var hidden = false
    private var recording = false
    private var hiddenSince: TimeInterval?
    private var mainWindowObserved = false
    private var resumed = false
    private var preparationStartedAt: TimeInterval?
    private var hideReturned: Bool?
    private var activationPolicyAtRequest: Int?

    init(directory: URL, mode: Mode = .colored) {
        self.directory = directory
        self.mode = mode
    }

    private func ownedWindows(_ pid: Int32) throws -> [[String: Any]] {
        guard let windows = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]] else {
            throw NSError(domain: "window-list", code: 1)
        }
        return windows.filter { ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid }
    }

    private func utc() -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date())
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let screen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == CGMainDisplayID()
        }) else { exit(2) }
        if mode == .desktop {
            do {
                try writeJSON(["mode": "desktop", "pixelWidth": CGDisplayPixelsWide(CGMainDisplayID()),
                               "pixelHeight": CGDisplayPixelsHigh(CGMainDisplayID()),
                               "activeDisplay": CGDisplayIsActive(CGMainDisplayID()) != 0 && CGDisplayIsAsleep(CGMainDisplayID()) == 0,
                               "helperWindowCount": ownedWindows(getpid()).count],
                              to: directory.appendingPathComponent("fixture-ready.json"))
            } catch { fail(error) }
            timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [self] _ in
                MainActor.assumeIsolated { prepareDesktop() }
            }
            return
        }
        let window = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        self.window = window
        let fixtureLevelAboveCapMainPanel = NSWindow.Level(rawValue: 101)
        window.level = mode == .testWindow ? NSWindow.Level(rawValue: 100) : fixtureLevelAboveCapMainPanel
        window.title = "Cap fixed screenshot fixture"
        window.isReleasedWhenClosed = false
        let root = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        window.contentView = root
        let colors = [CGColor(red: 0.9, green: 0.05, blue: 0.12, alpha: 1),
                      CGColor(red: 0.05, green: 0.18, blue: 0.9, alpha: 1),
                      CGColor(red: 0.05, green: 0.8, blue: 0.2, alpha: 1)]
        for (index, color) in colors.enumerated() {
            let width = root.bounds.width / 3
            let view = NSView(frame: NSRect(x: width * CGFloat(index), y: 0, width: width, height: root.bounds.height))
            view.wantsLayer = true
            view.layer?.backgroundColor = color
            root.addSubview(view)
        }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in
            do {
                try writeJSON(["pixelWidth": CGDisplayPixelsWide(CGMainDisplayID()),
                               "pixelHeight": CGDisplayPixelsHigh(CGMainDisplayID()),
                               "displayScale": screen.backingScaleFactor,
                               "visible": window.isVisible], to: directory.appendingPathComponent("fixture-ready.json"))
            } catch { fail(error) }
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.01, repeats: true) { [self] _ in
            MainActor.assumeIsolated { poll() }
        }
    }

    private func fail(_ error: Error) -> Never {
        fputs("Fixture failed: \(error)\n", stderr)
        exit(2)
    }

    private func prepareDesktop() {
        let request = directory.appendingPathComponent("cap-pid")
        guard !hidden, FileManager.default.fileExists(atPath: request.path) else { return }
        do {
            guard let pid = Int32(try String(contentsOf: request, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)),
                  pid != getpid() else { throw NSError(domain: "cap-pid", code: 1) }
            guard let application = NSRunningApplication(processIdentifier: pid) else { return }
            let now = ProcessInfo.processInfo.systemUptime
            if preparationStartedAt == nil { preparationStartedAt = now }
            let windows = try ownedWindows(pid)
            let mainWindows = windows.filter { ($0[kCGWindowLayer as String] as? NSNumber)?.intValue == 100 }
            if !mainWindows.isEmpty { mainWindowObserved = true }
            try writeJSON(["ownerPID": pid, "hidden": application.isHidden,
                           "activationPolicy": application.activationPolicy.rawValue,
                           "mainWindowObserved": mainWindowObserved, "visibleMainWindows": mainWindows.count,
                           "visibleWindowLayers": windows.compactMap { ($0[kCGWindowLayer as String] as? NSNumber)?.intValue },
                           "hideRequested": hideReturned != nil, "hideReturned": hideReturned.map { $0 as Any } ?? NSNull()],
                          to: directory.appendingPathComponent("desktop-state.json"))
            if let started = preparationStartedAt, now - started > 3 {
                throw NSError(domain: "desktop-preparation-timeout", code: 3)
            }
            guard mainWindowObserved else { return }
            if hideReturned == nil {
                activationPolicyAtRequest = application.activationPolicy.rawValue
                hideReturned = application.hide()
                return
            }
            guard application.isHidden && mainWindows.isEmpty else { hiddenSince = nil; return }
            guard let since = hiddenSince else { hiddenSince = now; return }
            guard now - since >= 0.25 else { return }
            try writeJSON(["ownerPID": pid, "hidden": true, "hiddenUTC": utc(),
                           "visibleMainWindows": mainWindows.count, "mainWindowLevel": 100,
                           "hideRequestCount": 1, "hideReturned": hideReturned ?? false,
                           "activationPolicyAtRequest": activationPolicyAtRequest ?? -1,
                           "activationPolicy": application.activationPolicy.rawValue,
                           "stableHiddenMs": (now - since) * 1000],
                          to: directory.appendingPathComponent("desktop-hidden.json"))
            hidden = true
            timer?.invalidate()
        } catch { fail(error) }
    }

    private func poll() {
        if mode == .testWindow && !resumed && FileManager.default.fileExists(atPath: directory.appendingPathComponent("unhide-request").path) {
            resumed = true
            window?.orderOut(nil)
            let editor = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 400, height: 300),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window = editor
            editor.title = "Cap desktop setup stand-in editor"
            editor.isReleasedWhenClosed = false
            editor.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [self] in
                do {
                    try writeJSON(["hidden": NSApplication.shared.isHidden, "visible": window?.isVisible == true,
                                   "visibleWindowCount": ownedWindows(getpid()).count],
                                  to: directory.appendingPathComponent("resumed.json"))
                } catch { fail(error) }
            }
        }
        if !hidden && FileManager.default.fileExists(atPath: directory.appendingPathComponent("capture-saved").path) {
            window?.orderOut(nil)
            hidden = true
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            do {
                try writeJSON(["hiddenUTC": formatter.string(from: Date())], to: directory.appendingPathComponent("fixture-hidden.json"))
            } catch { fail(error) }
        }
        let request = directory.appendingPathComponent("editor-pid")
        if !recording && FileManager.default.fileExists(atPath: request.path) {
            recording = true
            Task { @MainActor in
                do {
                    guard let pid = Int32(try String(contentsOf: request, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)) else {
                        throw NSError(domain: "fixture", code: 1)
                    }
                    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                    guard let editor = content.windows.first(where: {
                        $0.owningApplication?.processID == pid && $0.title == "Cap Screenshot Editor" && $0.isOnScreen
                    }) else { throw NSError(domain: "editor-window-not-found", code: 2) }
                    let filter = SCContentFilter(desktopIndependentWindow: editor)
                    let configuration = SCStreamConfiguration()
                    configuration.width = max(1, Int(filter.contentRect.width * CGFloat(filter.pointPixelScale)))
                    configuration.height = max(1, Int(filter.contentRect.height * CGFloat(filter.pointPixelScale)))
                    configuration.showsCursor = false
                    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
                    let bitmap = NSBitmapImageRep(cgImage: image)
                    guard let png = bitmap.representation(using: .png, properties: [:]) else {
                        throw NSError(domain: "editor-png", code: 3)
                    }
                    try png.write(to: directory.appendingPathComponent("editor.png"))
                    var evidence = try pngEvidence(png)
                    evidence["windowID"] = editor.windowID
                    evidence["ownerPID"] = pid
                    try writeJSON(evidence, to: directory.appendingPathComponent("editor-evidence.json"))
                } catch {
                    do {
                        try writeJSON(["error": error.localizedDescription], to: directory.appendingPathComponent("editor-evidence.json"))
                    } catch { fail(error) }
                }
            }
        }
    }
}

@main
struct Main {
    @MainActor
    static func main() {
        if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--compare-images" {
            do {
                let images = try CommandLine.arguments.suffix(2).map { path -> NSBitmapImageRep in
                    let data = try Data(contentsOf: URL(fileURLWithPath: path))
                    guard let bitmap = NSBitmapImageRep(data: data) else { throw NSError(domain: "decode", code: 1) }
                    return bitmap
                }
                let data = try JSONSerialization.data(withJSONObject: compareImages(images[0], images[1]), options: [.sortedKeys])
                print(String(decoding: data, as: UTF8.self))
            } catch {
                fputs("Image comparison failed: \(error)\n", stderr)
                exit(2)
            }
            return
        }
        guard CommandLine.arguments.count == 3 else { exit(2) }
        if CommandLine.arguments[1] == "--inspect-image" {
            guard let contents = try? Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])),
                  let evidence = try? pngEvidence(contents),
                  let data = try? JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys]),
                  let text = String(data: data, encoding: .utf8) else { exit(2) }
            print(text)
            return
        }
        let modes: [String: Fixture.Mode] = ["--run": .colored, "--run-desktop": .desktop, "--test-window": .testWindow]
        guard let mode = modes[CommandLine.arguments[1]] else { exit(2) }
        let application = NSApplication.shared
        application.setActivationPolicy(mode == .desktop ? .prohibited : .regular)
        let delegate = Fixture(directory: URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true), mode: mode)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
