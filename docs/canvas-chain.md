# Canvas Chain Audit

## Default Policy

Patches 0020 and 0031 retain native Canvas readback and export pixels by default:
legacy seed/persona noise requires `--uxr-synthetic-device-tests=true`.
Patch 0069 also requires that flag before parsing an endpoint or constructing a
Canvas Bridge client. Existing bridge and unsafe opt-ins are still required.
This disables incomplete remote substitutions across text, readback and export
in ordinary launches; it does not complete the bridge's color/codec protocol.

Synthetic tests are not measured-device emulation. Their old 8-bit noise and
bridge paths do not implement a common F16/ImageBitmap/GPU rendering backend.
The older fingerprint smoke runner explicitly opts its non-native scenarios
into synthetic mode to retain the meaning of its seed-effect tests. Its native
control and this audit do not enable the synthetic flag.

## Run

Requires Python, Playwright and Pillow with WebP and LittleCMS support. No
browser is downloaded. Use an explicit executable and a new output filename:

```powershell
python -X utf8 tools/canvas_chain_audit.py --browser C:/path/to/chrome.exe --output .chromix-local-build/device-pool-canvas/report.json
```

Two loopback servers provide a same-origin probe and a non-CORS image for taint
tests. The runner reuses the device collector's five-context launch harness:
window, iframe, dedicated worker, shared worker and service worker. It launches
profile A twice and fresh profile B once. Temporary profiles and servers are
closed even on failure. It uses `NATIVE_ARGS` from `_device_launch.py`, not an OS
network sandbox. Browser/probe hashes, browser and decoder versions, launch
arguments, raw pixels, encoded images and per-comparison metrics are recorded.

Exit 0 requires all tested paths to pass without unavailable optional cases.
Missing optional P3/F16 support produces `incomplete` and a nonzero exit; a
mismatch produces `failed`. Exceptions and incomplete matrices never pass.

## Coverage

- HTML Canvas in window/iframe and OffscreenCanvas in all five contexts.
- sRGB/Display-P3 and alpha true/false, actual context attributes, deterministic
  RGBA input, same-space and sRGB readback, crops, transparent out-of-bounds
  regions, zero-size reads and source stability after export.
- Independent fresh/full-read/crop-read OOB cases with omitted versus explicit
  colorSpace options. These preserve options/history-specific failures.
- Canvas-to-ImageBitmap, premultiplyAlpha none/premultiply/default, a separate
  colorSpaceConversion none case, bitmap close, Offscreen transfer and reset.
- F16 readback type/metadata, bounded values and actual F16 ImageData input.
- PNG/JPEG/WebP blob export at quality 0.92, repeated exports, HTML data URL/blob
  agreement, browser bitmap decoding and independent Pillow/ICC decoding.
- Unsupported MIME fallback, empty HTML callbacks/data URLs, Offscreen empty
  rejection and cross-origin read/export SecurityError behavior.
- Same-kind cross-context pixel/codec comparisons and full raw-observation
  signatures across restarts/profiles. Independent decoder annotations do not
  mutate those observations.

Comparisons use visible premultiplied RGB rather than undefined RGB at alpha
zero. Lossless maximum/mean differences are 2/0.6 in 8-bit units; lossy bounds
are 36/7. Alpha error is at most 1. JPEG expectations composite onto black.
Lossy bounds are engineering quality thresholds, not Web-platform conformance
limits: chroma subsampling can fail them without a fingerprint patch defect.
PNG independent color-managed comparison uses the lossless bounds; decoder
rounding differences remain reported instead of being silently accepted.

## Evidence And Limits

The 2026-09-10 local run uses installed Chrome 153.0.8010.37, not a verified
Chromix executable built from the target Chromium 152 patch series. Reports in
`.chromix-local-build/device-pool-canvas/` are local, ignored artifacts. Earlier
failed runs are retained. The audit is **not passing**: alpha:false read/export
differences, options/history-dependent Offscreen OOB results and cross-run
differences require investigation on a matching build. Error totals include
multiple checks of the same underlying mismatch, not unique defects.

Final local artifact: `audit-final.json`, 3 launches, 28 main rows per launch,
42 fresh/history edge cases per launch, 828 failed checks in total, no skipped
capabilities. Per-launch errors: 276/275/276, plus one restart/profile signature
mismatch. Independent decoders: Pillow 12.2.0, LittleCMS 2.18, WebP 1.6.0.
Same-kind cross-context comparisons pass in this final run; earlier failed
artifacts also include intermediate validator behavior and are not final results.

Final targeted regression: 395 passed, 29 skipped across the new audit,
Canvas C++/extended tests, smoke/surface validators and WebGL correctness tests.
The new audit module alone has 48 passing tests. The 124-patch linter, JS syntax
and `git diff --check` pass. Compiled Canvas harnesses use LLVM 22 with ASan/UBSan;
skips include absent local Chromium source prerequisites and a Linux-only Node
path. A broader run including CI-stage tests has one separate reproducible
failure: the Rust missing-bundle test invokes `python3` on this Windows host and
receives no expected diagnostic. That test was not changed or counted as passing.

Passing offline tests only validate the oracle, malformed-evidence rejection
and extracted C++ contracts. Their generated codec fixtures are not physical
device samples. A full native Chromium build and full patch-chain application
have not been rerun for these latest Canvas changes.

Still open: bitmaprenderer, transferControlToOffscreen across worker ownership,
ImageBitmap crop/resize/flip, wide-gamut/HDR F16 values, context loss/restoration,
parallel exports and source mutation while encoding, software/GPU backend
comparison, encoder quality boundaries and a shared backend-level privacy
policy. This audit deliberately does not claim these paths are complete or that
native fallback provides profile-distinct rendering identities.
