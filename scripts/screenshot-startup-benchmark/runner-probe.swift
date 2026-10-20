import AppKit
import Darwin
import Metal
import QuartzCore
import ScreenCaptureKit

struct ProbeFailure: LocalizedError {
    let errorDescription: String?

    init(_ message: String) {
        errorDescription = message
    }
}

@MainActor
final class RunnerProbe: NSObject, NSApplicationDelegate {
    private let output: URL
    private var report: [String: Any] = [
        "allChecksPassed": false,
        "purpose": "Runner capabilities only; not Cap startup timing",
        "os": ProcessInfo.processInfo.operatingSystemVersionString,
        "screenCapturePreflight": CGPreflightScreenCaptureAccess(),
    ]
    private var checks: [String: Bool] = [:]
    private var window: NSWindow?
    private var metalLayer: CAMetalLayer?
    private var queue: MTLCommandQueue?
    private var phase = "launch"

    init(output: URL) {
        self.output = output
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [self] in
            report["error"] = "Timed out during \(phase)"
            finish()
        }
        Task { @MainActor in
            do {
                try await run()
            } catch {
                report["error"] = error.localizedDescription
            }
            finish()
        }
    }

    private func save() {
        report["phase"] = phase
        report["checks"] = checks
        do {
            let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: output.appendingPathComponent("probe.json"), options: .atomic)
        } catch {
            fputs("Cannot write probe evidence: \(error)\n", stderr)
            exit(2)
        }
    }

    private func finish() {
        report["allChecksPassed"] = checks.count == 4 && checks.values.allSatisfy { $0 }
        save()
        window?.orderOut(nil)
        print("Capability probe complete: \(report)")
        exit(report["allChecksPassed"] as? Bool == true ? 0 : 1)
    }

    private func run() async throws {
        phase = "display"
        save()
        let displayID = CGMainDisplayID()
        guard let screen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == displayID
        }) else {
            throw ProbeFailure("No AppKit screen matches the main display")
        }
        guard CGDisplayIsActive(displayID) != 0 && CGDisplayIsAsleep(displayID) == 0 else {
            throw ProbeFailure("Main display is inactive or asleep")
        }
        report["display"] = [
            "id": displayID,
            "pixelWidth": CGDisplayPixelsWide(displayID),
            "pixelHeight": CGDisplayPixelsHigh(displayID),
            "scale": screen.backingScaleFactor,
        ]
        checks["activeDisplay"] = true

        phase = "window"
        let window = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        self.window = window
        window.level = .floating
        window.title = "Cap fork runner capability probe"
        window.isReleasedWhenClosed = false
        let root = NSView(frame: NSRect(origin: .zero, size: screen.frame.size))
        root.wantsLayer = true
        window.contentView = root
        let width = root.bounds.width / 3
        let red = NSView(frame: NSRect(x: 0, y: 0, width: width, height: root.bounds.height))
        red.wantsLayer = true
        red.layer?.backgroundColor = CGColor(red: 0.9, green: 0.05, blue: 0.12, alpha: 1)
        root.addSubview(red)
        let green = NSView(frame: NSRect(x: width * 2, y: 0, width: width, height: root.bounds.height))
        green.wantsLayer = true
        green.layer?.backgroundColor = CGColor(red: 0.05, green: 0.8, blue: 0.2, alpha: 1)
        root.addSubview(green)
        let center = NSView(frame: NSRect(x: width, y: 0, width: width, height: root.bounds.height))
        center.wantsLayer = true
        center.layer?.backgroundColor = CGColor(red: 1, green: 0, blue: 1, alpha: 1)
        root.addSubview(center)
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        try await Task.sleep(nanoseconds: 500_000_000)
        checks["visibleWindow"] = window.isVisible && window.occlusionState.contains(.visible)
        report["windowID"] = window.windowNumber
        save()

        phase = "metal"
        checks["metalPresentation"] = false
        report["metalDevices"] = MTLCopyAllDevices().map { $0.name }
        if let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() {
            self.queue = queue
            report["metalDevice"] = device.name
            let layer = CAMetalLayer()
            metalLayer = layer
            layer.device = device
            layer.pixelFormat = .bgra8Unorm
            layer.contentsScale = screen.backingScaleFactor
            layer.frame = center.bounds
            layer.drawableSize = CGSize(width: center.bounds.width * screen.backingScaleFactor,
                                        height: center.bounds.height * screen.backingScaleFactor)
            center.layer = layer
            CATransaction.flush()
            try await Task.sleep(nanoseconds: 250_000_000)
            if let drawable = layer.nextDrawable(), let command = queue.makeCommandBuffer() {
                let pass = MTLRenderPassDescriptor()
                pass.colorAttachments[0].texture = drawable.texture
                pass.colorAttachments[0].loadAction = .clear
                pass.colorAttachments[0].storeAction = .store
                pass.colorAttachments[0].clearColor = MTLClearColor(red: 0.05, green: 0.18, blue: 0.9, alpha: 1)
                if let encoder = command.makeRenderCommandEncoder(descriptor: pass) {
                    encoder.endEncoding()
                    command.present(drawable)
                    command.commit()
                    command.waitUntilCompleted()
                    checks["metalPresentation"] = command.status == .completed
                    if let error = command.error {
                        report["metalError"] = error.localizedDescription
                    }
                } else {
                    report["metalError"] = "Cannot create render encoder"
                }
            } else {
                report["metalError"] = "Cannot acquire window drawable or command buffer"
            }
        } else {
            report["metalError"] = "No default Metal device or command queue"
        }
        save()

        phase = "screenCaptureKit"
        checks["displayCapture"] = false
        save()
        try await Task.sleep(nanoseconds: 500_000_000)
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let ownWindowListed = content.windows.contains {
            $0.windowID == CGWindowID(window.windowNumber) &&
            $0.owningApplication?.processID == ProcessInfo.processInfo.processIdentifier && $0.isOnScreen
        }
        report["ownWindowListed"] = ownWindowListed
        checks["visibleWindow"] = checks["visibleWindow"] == true && ownWindowListed
        guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
            throw ProbeFailure("ScreenCaptureKit does not list the main display")
        }
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = CGDisplayPixelsWide(displayID)
        configuration.height = CGDisplayPixelsHigh(displayID)
        configuration.showsCursor = false
        let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
        let bitmap = NSBitmapImageRep(cgImage: image)
        guard let png = bitmap.representation(using: .png, properties: [:]) else {
            throw ProbeFailure("Cannot encode captured image")
        }
        try png.write(to: output.appendingPathComponent("captured-display.png"))
        let y = image.height / 2
        let samples = [image.width / 6, image.width / 2, image.width * 5 / 6].map {
            bitmap.colorAt(x: $0, y: y)?.usingColorSpace(.deviceRGB)
        }
        report["capturedImage"] = ["width": image.width, "height": image.height]
        report["sampleRGB"] = samples.map { color -> [Double] in
            guard let color = color else { return [] }
            return [Double(color.redComponent), Double(color.greenComponent), Double(color.blueComponent)]
        }
        if let left = samples[0], let right = samples[2] {
            checks["displayCapture"] = ownWindowListed && left.redComponent > 0.6 &&
                left.greenComponent < 0.3 && left.blueComponent < 0.4 &&
                right.greenComponent > 0.5 && right.redComponent < 0.3 && right.blueComponent < 0.4
        }
        if checks["metalPresentation"] == true, let middle = samples[1] {
            checks["metalPresentation"] = middle.blueComponent > 0.6 &&
                middle.redComponent < 0.3 && middle.greenComponent < 0.4
        } else {
            checks["metalPresentation"] = false
        }
        phase = "complete"
    }
}

@main
struct Main {
    @MainActor
    static func main() {
        guard CommandLine.arguments.count == 2 else {
            fputs("Usage: runner-probe OUTPUT_DIRECTORY\n", stderr)
            exit(2)
        }
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        let delegate = RunnerProbe(output: URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true))
        application.delegate = delegate
        withExtendedLifetime(delegate) {
            application.run()
        }
    }
}
