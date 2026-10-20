# Fork macOS runner capability probe

This is preparation for a screenshot editor startup benchmark. It does not build Cap, measure startup, or satisfy the PR's macOS validation requirement.

The workflow runs only in `dovidmoishe/Cap`, on pushes to `perf/screenshot-startup-benchmark` that change these tools or their workflow. It uses standard GitHub-hosted `macos-15` (Apple Silicon) and `macos-15-intel` runners. It does not require Cap's Blacksmith account. This push trigger allows the initial run without adding the workflow to the fork's default branch. Existing runs can be rerun in GitHub Actions.

Keep this tooling on the benchmark branch. The production PR remains on `perf/screenshot-editor-startup`; do not merge this branch into it. After collecting evidence, commit and push only the production fixes from that PR branch and summarize the results in the PR description or a comment. Benchmark tooling does not need to accompany those fixes.

Each job opens an owned, borderless test window on the main display. The left third is red, the right third green, and the center starts magenta. It checks for a Metal device, clears and presents a window drawable in blue, and captures the full display through ScreenCaptureKit. The captured image must contain the red and green regions, and the center must contain the Metal-rendered blue region. Merely listing a GPU or returning a screenshot is insufficient.

The probe records partial results before potentially blocking operations. It fails if a display, visible window, completed Metal command, captured fixture, or captured GPU output cannot be verified. It has a 60-second in-app deadline, a 90-second process deadline, and a ten-minute job deadline. It does not grant permissions or modify the runner's permission database. A denied capture or missing graphics device is evidence that more runner setup or another runner is needed, not evidence of a Cap regression.

Uploaded diagnostics include the capability JSON, compile/runtime logs, OS and runner image versions, hardware information, and a display PNG. The display image is from the disposable CI runner, not the user's desktop. Artifacts expire after seven days. The workflow has only read access to repository contents, does not use repository secrets, and does not post PR comments or modify branches.

Resources live only for the probe process. The test window, Metal layer, and command queue are retained while capture runs and released when the process exits. No renderer resources are added to the production application.

A pass establishes basic AppKit, Metal presentation, and ScreenCaptureKit capability on that runner image. It does not establish GPUI/wgpu compatibility, permission for a separately built Cap binary, the fallback SCStream path, representative consumer hardware, or a flash-free preview handoff. The next step is a separate Cap smoke run, then matched baseline/revised release-build measurements on one runner with fixed capture input, separate first-use/warm samples, and separate preview/GPU draw endpoints. Transition recordings must run separately from timed samples.

Preparation on Windows can validate the workflow structure and Python runner, but cannot compile this macOS Swift program. The first Actions run must compile and execute it; until then its macOS behavior is unverified.
