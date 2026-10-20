import AppKit
import Darwin

@MainActor
final class WindowProbe: NSObject, NSApplicationDelegate {
    let directory: URL
    let role: String
    let targetPID: pid_t?
    private var window: NSWindow?
    private var timer: Timer?
    private var lastCommand = ""
    private var evidence: [String: Any] = [:]

    init(directory: URL, role: String, targetPID: pid_t?) {
        self.directory = directory
        self.role = role
        self.targetPID = targetPID
    }

    private func state(_ application: NSRunningApplication) -> [String: Any] {
        let windowList = CGWindowListCopyWindowInfo(.optionOnScreenOnly, kCGNullWindowID) as? [[String: Any]]
        let windows = (windowList ?? [])
            .filter { ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == application.processIdentifier }
        return ["pid": application.processIdentifier, "hidden": application.isHidden,
                "active": application.isActive, "finishedLaunching": application.isFinishedLaunching,
                "activationPolicy": application.activationPolicy.rawValue,
                "bundleIdentifier": application.bundleIdentifier ?? "",
                "bundleURL": application.bundleURL?.path ?? "",
                "windowListAvailable": windowList != nil, "visibleWindowCount": windows.count,
                "visibleWindowLayers": windows.compactMap { ($0[kCGWindowLayer as String] as? NSNumber)?.intValue }]
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if role == "target" {
            let panel = NSPanel(contentRect: NSRect(x: 100, y: 100, width: 600, height: 400),
                                styleMask: .borderless, backing: .buffered, defer: false)
            panel.level = NSWindow.Level(rawValue: 100)
            panel.hidesOnDeactivate = false
            panel.isReleasedWhenClosed = false
            panel.title = "Cap window diagnostic stand-in"
            window = panel
            panel.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [self] _ in
            MainActor.assumeIsolated { tick() }
        }
        tick()
    }

    private func tick() {
        do {
            let commandURL = directory.appendingPathComponent("command")
            if FileManager.default.fileExists(atPath: commandURL.path) {
                let command = try String(contentsOf: commandURL, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
                if command != lastCommand {
                    lastCommand = command
                    if role == "helper", command == "remote-hide", let targetPID,
                       let target = NSRunningApplication(processIdentifier: targetPID) {
                        evidence["beforeHide"] = state(target)
                        evidence["hideReturned"] = target.hide()
                    } else if role == "target", command == "self-hide" {
                        NSApp.hide(nil)
                    } else if role == "target", command == "open-editor" {
                        window?.orderOut(nil)
                        let editor = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 400, height: 300),
                                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
                        editor.title = "Cap diagnostic stand-in editor"
                        editor.isReleasedWhenClosed = false
                        window = editor
                        editor.makeKeyAndOrderFront(nil)
                        NSApp.activate(ignoringOtherApps: true)
                    } else if role == "target", command == "explicit-unhide" {
                        NSApp.unhide(nil)
                        window?.makeKeyAndOrderFront(nil)
                        NSApp.activate(ignoringOtherApps: true)
                    }
                }
            }
            evidence["role"] = role
            evidence["lastCommand"] = lastCommand
            evidence["self"] = state(NSRunningApplication.current)
            if let targetPID, let target = NSRunningApplication(processIdentifier: targetPID) {
                evidence["target"] = state(target)
            }
            let data = try JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: directory.appendingPathComponent("state.json"), options: .atomic)
        } catch {
            fputs("Window diagnostic failed: \(error)\n", stderr)
            exit(2)
        }
    }
}

@main
struct Main {
    @MainActor static func main() {
        let args = CommandLine.arguments
        guard args.count == 4 || args.count == 5,
              ["helper", "target"].contains(args[1]),
              ["regular", "accessory", "prohibited"].contains(args[3]) else { exit(2) }
        let application = NSApplication.shared
        let policy: NSApplication.ActivationPolicy = args[3] == "prohibited" ? .prohibited : args[3] == "accessory" ? .accessory : .regular
        let accepted = application.setActivationPolicy(policy)
        print("Requested policy \(args[3]); accepted=\(accepted)")
        let delegate = WindowProbe(directory: URL(fileURLWithPath: args[2], isDirectory: true), role: args[1],
                                   targetPID: args.count == 5 ? Int32(args[4]) : nil)
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
