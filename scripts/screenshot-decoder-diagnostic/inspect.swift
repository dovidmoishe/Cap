import AppKit
import Darwin

func predicateResults(_ samples: [[Double]]) -> [[String: Any]] {
    guard samples.count == 3 && samples.allSatisfy({ $0.count == 3 }) else { return [] }
    let rules: [(Int, Int, String, Double)] = [
        (0, 0, ">", 0.6), (0, 1, "<", 0.3), (0, 2, "<", 0.4),
        (1, 2, ">", 0.6), (1, 0, "<", 0.3), (1, 1, "<", 0.4),
        (2, 1, ">", 0.5), (2, 0, "<", 0.3), (2, 2, "<", 0.4),
    ]
    return rules.map { sample, channel, operation, threshold in
        let actual = samples[sample][channel]
        return ["sample": ["left", "center", "right"][sample],
                "channel": ["red", "green", "blue"][channel],
                "operation": operation, "threshold": threshold, "actual": actual,
                "passed": operation == ">" ? actual > threshold : actual < threshold]
    }
}

func rgb(_ color: NSColor?) -> [Double] {
    guard let color = color, color.colorSpace.colorSpaceModel == .rgb else { return [] }
    return [Double(color.redComponent), Double(color.greenComponent), Double(color.blueComponent)]
}

@main
struct Main {
    @MainActor
    static func main() {
        guard CommandLine.arguments.count == 2 else { exit(2) }
        do {
            let contents = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
            guard let bitmap = NSBitmapImageRep(data: contents), bitmap.pixelsWide > 0,
                  bitmap.pixelsHigh > 0, bitmap.samplesPerPixel <= 8 else {
                fputs("Cannot decode an inspectable bitmap\n", stderr)
                exit(2)
            }
            let rawRGBEligible = bitmap.bitsPerSample == 8 && bitmap.samplesPerPixel >= 3 &&
                bitmap.colorSpace.colorSpaceModel == .rgb && !bitmap.bitmapFormat.contains(.alphaFirst)
            let locations = [bitmap.pixelsWide / 6, bitmap.pixelsWide / 2, bitmap.pixelsWide * 5 / 6]
            var points: [[String: Any]] = []
            var rawSamples: [[Double]] = []
            var deviceSamples: [[Double]] = []
            var srgbSamples: [[Double]] = []
            for x in locations {
                let y = bitmap.pixelsHigh / 2
                var pixel = [Int](repeating: 0, count: 8)
                pixel.withUnsafeMutableBufferPointer { buffer in
                    bitmap.getPixel(buffer.baseAddress!, atX: x, y: y)
                }
                let source = bitmap.colorAt(x: x, y: y)
                let raw = rawRGBEligible ? pixel.prefix(3).map { Double($0) / 255 } : []
                let device = rgb(source?.usingColorSpace(.deviceRGB))
                let srgb = rgb(source?.usingColorSpace(.sRGB))
                rawSamples.append(raw)
                deviceSamples.append(device)
                srgbSamples.append(srgb)
                points.append(["x": x, "y": y,
                               "rawSamples": Array(pixel.prefix(bitmap.samplesPerPixel)),
                               "sourceRGB": rgb(source), "deviceRGB": device, "sRGB": srgb,
                               "sourceColorSpace": source?.colorSpace.localizedName ?? "unknown"])
            }
            let report: [String: Any] = [
                "width": bitmap.pixelsWide, "height": bitmap.pixelsHigh,
                "bitsPerSample": bitmap.bitsPerSample, "samplesPerPixel": bitmap.samplesPerPixel,
                "bitmapFormat": bitmap.bitmapFormat.rawValue, "isPlanar": bitmap.isPlanar,
                "hasAlpha": bitmap.hasAlpha, "rawRGBEligible": rawRGBEligible,
                "bitmapColorSpace": bitmap.colorSpace.localizedName ?? "unknown",
                "iccProfileBytes": bitmap.colorSpace.iccProfileData?.count ?? 0,
                "points": points,
                "rawChecks": predicateResults(rawSamples),
                "deviceRGBChecks": predicateResults(deviceSamples),
                "sRGBChecks": predicateResults(srgbSamples),
            ]
            let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
            guard let text = String(data: data, encoding: .utf8) else { exit(2) }
            print(text)
        } catch {
            fputs("Image inspection failed: \(error)\n", stderr)
            exit(2)
        }
    }
}
