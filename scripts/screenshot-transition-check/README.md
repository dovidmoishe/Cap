# Screenshot preview-to-GPU evidence

This is a fork-only, untimed visual diagnostic for the GPUI screenshot editor. It does not rebuild Cap, change production sources, or replace the completed timing run `39488846038`. It reuses the release package from `39426190700` and verifies its files, executable permissions, source snapshot, and instrumentation provenance before launch.

The separate `Fork screenshot transition evidence` workflow triggers only for this directory or its own workflow file on `perf/screenshot-startup-benchmark`, and only in `dovidmoishe/Cap`. These commits stay out of the production PR branch. It contains no Cargo build or repeated benchmark step.

## Recording flow

The native helper opens a static red/blue/green and grayscale background panel at level -1, below normal windows. It does not activate the app or hide itself during the handoff. Cap's main window is hidden using the existing owned PID/window-number protocol before display capture. The editor opens above the pattern. The source PNG must contain the pattern, so a blank desktop does not qualify as useful color evidence.

ScreenCaptureKit starts before Cap launches, requests 60 updates per second, and captures the full display without a pointer or audio. The helper confirms that its own pattern is actually visible in captured pixels before the runner can launch Cap. Acquisition is restricted to a scale-one display; other configurations are unverified and rejected.

ScreenCaptureKit's queue depth is three. Complete BGRA buffers are copied out and unlocked immediately; idle events reuse the previous pixel data because the API reports that the display did not change. The diagnostic retains at most 120 events, a two-second history, and 384 MiB of conservatively counted frame bytes. Framework buffers, temporary copies, and PNG encoding can require additional memory. This memory belongs only to the short-lived recorder process. It is not a production cache. PNG encoding starts after stream capture stops.

The recorder saves presentation timestamps, their UTC mapping through the host clock, arrival timestamps, complete/idle status, clock uncertainty, evictions, stream errors, and unusable events. It stops 750 ms after observing the saved build's `gpu-drawn` IPC file, with a hard 20-second capture deadline. The existing Cap helper still verifies normal editor close and terminates only owned processes.

Before downloading or launching Cap, a native self-test changes the center stripe blue → green → blue. Analysis requires the expected pixels in all three timestamped phases, allowing 80 ms after each color change. This checks the buffer format, stream updates, and clock alignment on the actual runner. It cannot be executed on Windows.

## Evidence and interpretation

Analysis requires a separately observed preview/GPU callback interval of at least 50 ms, at least two pattern-bearing samples around the preview callback, and at least two after the GPU callback. Frames are attributed using software callbacks and captured presentation times, not physical display scanout. Missing coverage, duplicate timestamps, an unstable or wrong owned window, a sampling gap above 50 ms, stream errors, or unusable stream events make the result inconclusive. No retry or sample omission turns an incomplete recording into a passing visual claim.

The inspected crop is the whole owned editor window. The static source pattern must remain present in every sampled frame between the observed preview and post-GPU samples; a loss is flagged as a possible visual discontinuity. Per-frame pixel differences against the final editor crop are saved as diagnostics. They include UI chrome, so a difference does not automatically identify an image flash, and zero sampled differences cannot rule out shorter unsampled events.

A green collection job means `evidence-ready-for-review`, not “flash-free.” Manual inspection of `recording/transition-filmstrip.png` and the raw frame sequence is still required. This samples a virtual macOS display and cannot prove behavior on a physical consumer Mac, every image or preset, or events between captured samples. Recording adds load, so this job's draw times are not performance measurements.

Artifacts retain all captured PNGs, recorder metadata, original Cap input PNG, Cap logs, package/source provenance, calibration evidence, per-frame diagnostic differences, and a filmstrip. Read `transition.json`, `recording/recording.json`, `recording/analysis.json`, and `report.md` alongside the images. The production reviewer fixes and the CI evidence commits remain on separate branches.

## Local checks

Install the pinned Python dependencies in an isolated environment, then run `python scripts/screenshot-transition-check/test-analysis.py`. The tests use synthetic PNGs and mocked processes; they do not launch Cap. Native Swift compilation, actual capture, and the blue/green calibration require the macOS job. The workflow has an eight-minute limit and retains partial evidence on failure where Actions permits upload.

API references: [Apple ScreenCaptureKit sample](https://developer.apple.com/documentation/screencapturekit/capturing-screen-content-in-macos), [frame status](https://developer.apple.com/documentation/screencapturekit/scframestatus), [frame interval](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/minimumframeinterval), and [queue depth](https://developer.apple.com/documentation/screencapturekit/scstreamconfiguration/queuedepth).
