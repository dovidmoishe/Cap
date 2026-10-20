# Screenshot image-decoder diagnostic

This short fork-only workflow investigates the existing failed smoke run `39415518245`. It downloads that run's `cap-screenshot-smoke-macos-15` artifact, compiles two small Swift helpers, and inspects its five PNGs. It does not compile Rust, launch Cap, capture another screenshot, or collect startup timings. The job has a ten-minute limit.

The original helper is compiled unchanged from `scripts/screenshot-startup-benchmark/cap-fixture.swift`. The diagnostic helper reads the same PNG through `NSBitmapImageRep(data:)`, then reports raw integer samples, unconverted source RGB, device RGB, sRGB, bitmap format, color-space names, and the individual color predicates. Raw normalized RGB is interpreted only for an eight-bit RGB bitmap without alpha-first ordering. The collector verifies that the diagnostic's device-RGB predicates reproduce the original helper's result; disagreement fails the diagnostic instead of being silently accepted.

A green diagnostic job means evidence was collected successfully. It does not turn the failed smoke run green, relax its thresholds, approve the eight-pixel input difference, or establish that a color-space conversion caused the rejection. Read the actual values before choosing a fix.

The files live in a separate directory with a separate workflow path. Neither the Cap smoke workflow nor the basic runner-probe workflow matches these changed paths, so pushing only these four files does not trigger either older workflow. The job has read-only access to repository contents and Actions artifacts, uses no repository secrets, and posts no comments. The source artifact must still exist and not have expired. New diagnostic artifacts expire after seven days.

Windows validation covers workflow structure and Python syntax. Swift compilation and decoding on macOS remain unverified until this job runs.

## Before another expensive Cap build

After resolving the validator, split building from testing and retain runnable binary packages. Package each instrumented executable with the runtime frameworks/libraries it needs, retain source hashes and executable checksums, and test that the package launches after extraction. A test-only workflow should download a pinned build artifact rather than invoke Cargo. Preserve the raw images and all validation values even when a check fails. Excluding a tiny changing display region from equality checks would be a separate methodological decision that must be explicit in any performance report.

This packaging change is planned, not implemented by the decoder diagnostic. Existing Cap executables were excluded from the failed runs' artifacts, so those runs do not provide downloadable binaries for a test-only retry.
