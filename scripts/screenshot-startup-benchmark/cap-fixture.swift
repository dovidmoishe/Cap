import AppKit
import Darwin
import ScreenCaptureKit

func imageEvidence(_ bitmap: NSBitmapImageRep) -> [String: Any] {
    var counts = [0, 0, 0]
    var total = 0
    for y in stride(from: 0, to: bitmap.pixelsHigh, by: max(1, bitmap.pixelsHigh / 64)) {
        for x in stride(from: 0, to: bitmap.pixelsWide, by: max(1, bitmap.pixelsWide / 64)) {
            guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
            let r = color.redComponent
            let g = color.greenComponent
            let b = color.blueComponent
            if r > 0.6 && g < 0.3 && b < 0.4 { counts[0] += 1 }
            if g > 0.5 && r < 0.3 && b < 0.4 { counts[1] += 1 }
            if b > 0.6 && r < 0.3 && g < 0.4 { counts[2] += 1 }
            total += 1
        }
    }
    let ratios = counts.map { Double($0) / Double(max(1, total)) }
    let centers = [bitmap.pixelsWide / 6, bitmap.pixelsWide / 2, bitmap.pixelsWide * 5 / 6].map { x -> [Double] in
        guard let color = bitmap.colorAt(x: x, y: bitmap.pixelsHigh / 2)?.usingColorSpace(.deviceRGB) else { return [] }
        return [Double(color.redComponent), Double(color.greenComponent), Double(color.blueComponent)]
    }
    let fixtureMatches = centers.allSatisfy { $0.count == 3 } &&
        centers[0][0] > 0.6 && centers[0][1] < 0.3 && centers[0][2] < 0.4 &&
        centers[1][2] > 0.6 && centers[1][0] < 0.3 && centers[1][1] < 0.4 &&
        centers[2][1] > 0.5 && centers[2][0] < 0.3 && centers[2][2] < 0.4
    return ["width": bitmap.pixelsWide, "height": bitmap.pixelsHigh, "sampleRGB": centers,
            "colorRatios": ratios, "fixtureMatches": fixtureMatches,
            "containsFixtureColors": ratios.allSatisfy { $0 > 0.05 }]
}

func writeJSON(_ value: [String: Any], to path: URL) throws {
    let data = try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
    try data.write(to: path, options: .atomic)
}

@MainActor
final class Fixture: NSObject, NSApplicationDelegate {
    let directory: URL
    private var window: NSWindow?
    private var timer: Timer?
    private var hidden = false
    private var recording = false

    init(directory: URL) {
        self.directory = directory
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let screen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == CGMainDisplayID()
        }) else { exit(2) }
        let window = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        self.window = window
        let fixtureLevelAboveCapMainPanel = NSWindow.Level(rawValue: 101)
        window.level = fixtureLevelAboveCapMainPanel
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

    private func poll() {
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
                    var evidence = imageEvidence(bitmap)
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
        guard CommandLine.arguments.count == 3 else { exit(2) }
        if CommandLine.arguments[1] == "--inspect-image" {
            guard let bitmap = NSBitmapImageRep(contentsOf: URL(fileURLWithPath: CommandLine.arguments[2])),
                  let data = try? JSONSerialization.data(withJSONObject: imageEvidence(bitmap), options: [.sortedKeys]),
                  let text = String(data: data, encoding: .utf8) else { exit(2) }
            print(text)
            return
        }
        guard CommandLine.arguments[1] == "--run" else { exit(2) }
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        let delegate = Fixture(directory: URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true))
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
