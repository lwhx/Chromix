# Fingerprint backlog coverage

This record tracks the implementation requested by `/root/fingerprint-p0-p2-backlog.md` against Chromium **152.0.7977.82**. A persona is the configured browser identity for one launch. Returning configured values is not sufficient if permissions, rendering, network traffic, or actual capabilities disagree.

## Acceptance rules

Each backlog category remains open until it has all of the following:

1. An explicit policy for high-entropy host data and necessary capability checks.
2. Agreement between browser/network, renderer, and supported worker contexts.
3. Defined lifetime and origin/profile isolation of stable values.
4. Correct permissions, exceptions, unavailable-device behavior, and resource limits.
5. Static regression tests and a smoke test using a browser built with these exact changes.
6. A passing patch check and full Python test suite.
7. A successful Linux x64 Chromium build and native browser run from the matching source revision.

A standalone C++ test with stubs checks the extracted algorithm or getter contract, not Chromium integration. The Canvas helper also has a syntax-only check against locally available real Chromium/Skia headers; that check caught and fixed a `base::span` length-type mismatch but does not compile either complete Blink translation unit. Applying patches to Linux/macOS/Windows source fixtures is not a native build on those platforms. Existing builds started before these changes cannot validate them.

## P0 coverage

| Category | Existing coverage and this round's focus | Remaining acceptance work |
|---|---|---|
| UA / Client Hints | Browser-level UA/version/brand configuration; remove the renderer-only second rewrite and preserve UA suffix tokens. Normalize SDK `windows`/`macos`/`linux` aliases and provide desktop CH defaults without falling back to another host platform. | Verify JS and negotiated HTTP hints in window, iframe, and workers; explicit custom UA overrides and Android/mobile cases. |
| WebRTC | ICE policy, candidate/SDP presentation, explicit default-off fake candidate support. | Real connection through the configured proxy; IPv4/IPv6/mDNS/ports, related addresses, codec capabilities, and stats. Rewriting an address does not route traffic through it. |
| MediaDevices | Preserve browser-provided permission filtering, salted IDs, ordering, and empty enumerations instead of inventing unusable devices. | Real permission transitions, device constraints, origin separation, and profile-restart stability. No virtual media backend is implemented. |
| Intl / ICU | Move locale initialization before JS use rather than changing process-global ICU state from a language getter. | Default and explicit locale/calendar/numbering/hour-cycle cases across all contexts; ICU fallback, timezone initialization, and restart tests. |
| Canvas | Fix RGBA/BGRA channel ordering, transparent pixels, coordinate arithmetic, and private-copy encoding in both ImageDataBuffer constructors; preserve source pixels and padding. | Async idle encoding, F16, ImageBitmap, premultiplication/color-space agreement, common WebGL/WebGPU seed isolation, and browser readback/encode tests remain open. |
| WebAudio | Preserve mutable AudioBuffer data, silence, native analyser projections, and the actual rendering sample rate. | A coherent graph-level privacy mechanism; OfflineAudioContext, compressor/oscillator output, channel counts, and base/output latency. Getter-only noise is not a graph-level solution. |
| WebGPU | Explicit empty feature allowlists return no features; nonempty lists intersect real support. Adapter/device limits retain Dawn values; reject invalid requested alignment. GPU identity uses one complete vendor/architecture tuple; software fallback status remains native and native device/driver fields are not mixed into a synthetic tuple. | Subgroup data, default format and buffer readback; validate advertised/requestable capabilities on real hardware. The synthetic Windows GPU templates are declared test templates, not measured-device samples or market-share weights. |
| Screen / Window / Viewport | Existing screen, outer-window, DPR and detailed-screen getters. | Actual layout, inner dimensions, visualViewport, CSS device size/DPR, zoom, and multi-screen agreement. |
| Performance Timing | Remove the recursive synthetic network-phase fallback and preserve native ordering/zero rules. | Navigation/resource/paint/event/longtask/worker/RAF precision and background lifecycle tests. |

## P1 coverage

| Category | Status and next verification |
|---|---|
| Permissions | Notification getters/query results must use the browser authority also used by requests and observers. Clipboard, USB, Bluetooth, serial, sensors, and geolocation need end-to-end verification. Never bypass a permission check to make a reported status appear consistent. |
| Codecs / MSE / EME | MediaCapabilities filters now intersect native supported, smooth and power-efficient results; they cannot promote false results. This is not a shared codec policy: compare playback, MSE, EME, WebRTC and actual decoding. |
| Storage | Removed seed-derived and explicit renderer-only quota replacement. StorageManager and buckets retain browser accounting, individual limits and error paths. Backend quota enforcement, persistence and IndexedDB/Cache partitioning still require real browser tests. |
| Network Information | Removed RTT/downlink-only overrides so getters, cached state, effectiveType, saveData and native change events share the notifier again. Real network transitions and any future notifier-level test policy remain unverified. |
| Font provenance | Family-name filtering does not prove the selected font is bundled. Check generic, local, last-resort, emoji/CJK/math fallback and Font Loading API. |
| Plugins / MIME / PDF | A reported PDF plugin cannot create a missing/disabled viewer. Verify actual PDF display and extension exposure. |
| Input / device capabilities | Keyboard map overrides do not change actual key/code input. Touch/pointer CSS, gamepad, orientation, motion, and sensors remain open. |
| CSS media features | Existing overrides cover selected preferences, not full gamut/HDR rendering, print media, scrollbars, and layout. |
| WebGL | Remove discarded native renderer/vendor queries in persona identity branches. Actual capability clamps remain necessary; extensions, shader precision/compilation, antialiasing, and readbacks need real GPU tests. |
| Wasm / SIMD / threads / SharedArrayBuffer | No unified capability policy established; hardwareConcurrency/deviceMemory getters do not control these execution capabilities. |

## P2 coverage

| Category | Status and next verification |
|---|---|
| TLS ClientHello | No persona TLS configuration layer established. Capture and compare negotiated TLS versions, cipher suites, extensions, GREASE, and signature algorithms. |
| ALPN / HTTP/2 / HTTP/3 | Preserve valid negotiation; no completed persona policy for protocol selection. |
| HTTP/2 | SETTINGS, priority, initial windows and pseudo-header ordering require wire-level tests. |
| HTTP/3 / QUIC | Transport parameters and connection retry behavior require packet-level verification. |
| HTTP headers | UA and Accept-Language need joint JS/server checks. Accept-Encoding must match real decoder support. |
| DNS / proxy / connection reuse | WebRTC policy is not an HTTP/QUIC/DNS routing policy. Verify actual routes, IPv4/IPv6 priority and connection reuse. |
| Date / timezone / DST / Temporal | Existing timezone substitution does not complete initialization, DST boundary, host timezone change, and Temporal testing. |
| Timer quantization | No unified persona quantization for Date.now, performance.now, RAF and IdleCallback. |
| Page lifecycle | Preserve native visibility, throttling, freeze/resume behavior until a specified, tested policy exists. |

## Real-device pool gaps and next priorities

The current three Windows GPU records are synthetic templates. No measured full-device corpus, sample provenance, or measured population distribution is implemented. Seed stability is implemented, but it does not turn independently configured values into real device samples.

| Priority | Gap confirmed in the current patches | Required implementation or evidence |
|---|---|---|
| 1 | Screen/window/DPR overrides remain separate from actual layout. `screen.avail*` is not bounded by the declared screen, window coordinates reject valid negatives, and detailed-screen flags do not remove extra screens. | Configure display geometry at the browser/emulation layer; validate inner/outer/visualViewport, CSS resolution queries, zoom, orientation changes and multiple monitors together. |
| 1 | CPU count, deviceMemory and JS heap limit remain independent settings; a 64-bit UA can force a roughly 4 GiB reported heap without changing V8. | Use measured device-class records and preserve actual V8 allocation limits and execution capabilities. Test worker contexts, Wasm/SIMD/threads and memory observations. |
| 1 | Font filtering allows generic, unique-name and last-resort paths without proving which file provided the glyphs. | Record bundled font file provenance; validate fallback, emoji/CJK/math shaping, CSS Font Loading and text rasterization on each supported OS. |
| 2 | GPU identity templates do not prove the native backend implements a corresponding physical device. Linux/macOS/Windows ARM retain native identity. | Collect internally consistent identity/capability records with browser/driver/build versions, then validate requestable features and rendered/readback results on the matching hardware. |
| 2 | Media devices, audio, codec queries, permissions and capture are not a single device model. | Use actual or explicit virtual device backends; test permission transitions, capture, latency, decoding and playback without inventing support. |
| 2 | Network/proxy/DNS/WebRTC/TLS/HTTP behavior has no unified measured policy. | Verify actual routes and packet-level behavior separately from JavaScript estimates; network and storage state must be allowed to change normally. |

A future measured record should retain its collection provenance, Chromium/build/OS/architecture/driver versions, complete correlated values, and the native capabilities needed to use it. Selection must be constrained to compatible backends and stable for a persistent profile. Unsupported combinations should fall back to native behavior or be rejected, rather than partially copying another device. This is remaining work, not an implemented pool.

## Compatibility changes

Renderer-only overrides that contradicted actual browser behavior are retired; capability filters that remain enabled are constrained as follows:

- `--uxr-storage-quota` / `--fingerprint-storage-quota` no longer changes only reported storage capacity, and Canvas seeds no longer generate fictitious quotas. Origin estimates and individual bucket limits remain browser-owned.
- `--uxr-net-rtt` and `--uxr-net-downlink` no longer override isolated getters. Network Information values and change events remain notifier-owned; zero RTT/downlink is a valid observation.
- `--uxr-codec-*` / `--fingerprint-codec-*` filters only restrict native supported/smooth/power-efficient results. They cannot enable a decoder, smooth playback or hardware acceleration.
- `--uxr-webgpu-limit-*` no longer overrides the adapter/device getters. Actual limits and request validation remain owned by Dawn; a persona-aware backend limit policy is still open.
- `--uxr-media-devices` no longer inserts nonexistent devices into an empty enumeration. Use a real or explicitly configured Chromium test device backend when testing media capture.
- `--uxr-notification-permission` no longer rewrites permission getters or query results. Configure real browser permissions instead.
- `--uxr-audio-samplerate` no longer changes only the reported sample rate. Use `AudioContext({sampleRate: ...})` or the corresponding OfflineAudioContext option; validate the resulting actual rate.
- `--uxr-audio-seed` no longer mutates AudioBuffer PCM on read or adds separate time/frequency analyser noise. The legacy CLI may still supply this key; that does not imply a working graph-level audio privacy mechanism.

The numbered patches retain short invariant comments so existing patch numbering remains stable. The functional change is removal of the incorrect overrides, not the comments themselves. Apply the revised series to a clean, matching pre-Chromix source layer; do not stack revised patches over old versions of the same patches.

Desktop platform aliases (`windows`/`Win32`, `macos`/`MacIntel`, `linux`/`Linux x86_64`) now initialize coherent UA/CH platform fields. Generic aliases on the native OS preserve native architecture, bitness, model, WoW64 and platform version; cross-OS templates use x86/64-bit, empty model, non-WoW64, and platform versions `10.0.0`, `10.15.7`, and empty respectively. Explicit native high-entropy options still win. These are declared test personas, not detected host versions. `--fingerprint=off` also restores native UA branding/headless handling and explicitly selects real GPU identity with readback noise disabled. This does not claim every historical unconditional Chromix patch is disabled. GPU template selection is restricted to the Windows x86/x64 test path, uses `uxr-fingerprint-seed` before the legacy Canvas seed fallback, and has a deterministic integer mapping; other platforms preserve native GPU identity. The three templates are synthetic test fixtures, not measured-device samples or market-share weights. Persistent SDK profiles reuse `.chromix-fingerprint-seed` across Node/Python and concurrent launches.

## Local browser smoke runner

`tools/fingerprint_smoke.py` requires an explicitly supplied existing native executable and the Python Playwright package. It does not discover, install, or download a browser. A valid binary may be named `chrome`, `chrome.exe`, or `Chromium`; its name and computed hash do not authenticate its provenance.

```bash
python3 tools/fingerprint_smoke.py \
  --browser /path/to/verified/chromix/chrome \
  --platform linux windows \
  --locale de-DE fr-FR ja-JP \
  --seed 0x13579BDF --other-seed 0x2468ACE0 \
  --output /tmp/chromix-fingerprint-smoke.json
```

Use `--expected-sha256` with an independently obtained **executable** hash when available. The release archive hash is not the executable hash. `--no-sandbox` requires explicit opt-in; the runner never adds it automatically for root.

The runner serves only its own loopback HTTP origin, rejects external proxy requests, restricts page/worker requests and WebSockets, and disables selected browser background networking. These controls are not an operating-system network sandbox. It grants no media permissions and invokes capture only inside a document whose policy denies camera/microphone access.

Checks cover negotiated HTTP hints versus JS in window/iframe/dedicated worker, requested platform/default Intl locale, repeated Canvas reads, selected offline audio invariants, denied media behavior, reload stability, and persistent-profile restart stability. Network Information and StorageManager estimates are also collected in all three contexts and checked for types/ranges and explicit API failures. Dynamic network estimates and quotas are excluded from identity/restart comparisons; existing usage above a reduced quota is allowed. These probes do not test connection events, real throughput, disk enforcement or storage buckets. Each persona/seed family gets a fresh temporary profile; restarts reuse that profile. The control uses `--fingerprint=off` without locale/platform overrides, not a separate stock Chromium binary. Missing evidence, crashes, mismatches and unexpected external requests produce a failed JSON report and nonzero exit. Unsupported optional probes are reported separately and do not complete their backlog category.

## Merge integration (2026-09-10)

The local merge of `origin/main` at `a727c817` retains the earlier Canvas copy/bounds fixes, notifier-owned network state, mutable AudioBuffer data, native WebGPU capabilities, and shared persistent SDK seeds. It also preserves the correct Windows restored-source patch paths and `fingerprint_data.h` build input. The duplicate trailing time-clamper patch is removed rather than applied twice.

Incoming CPU/memory and display templates are synthetic, not a measured device corpus. Configuration seeds accept the full unsigned 64-bit range and use integer-weight selection instead of standard-library-specific floating distributions. Typed configuration accessors support the merged GPU code. The incoming independent seed-derived `outerHeight` and DPR defaults are not enabled: `screen` and actual layout do not consume the same defaults, so explicit configuration and native fallbacks are retained. The SDK supplies explicit geometry separately; full browser layout agreement still requires runtime validation.

The new planning documents under `docs/` describe future work, not completed cross-process integration. Font mappings do not prove glyph-file provenance; `0112` only adds includes and does not implement layout-theme system fonts. Speech voice-list configuration changes reported entries, not the installed speech backend. Added battery, shader, font and timing hooks likewise require matching-build and browser validation before their categories can be accepted. Native audio latency and maximum-channel reporting are retained rather than enabling getter-only replacements. The CSS pointer/hover patch is rebased to the actual Chromium 152 method locations and uses its declared Mojo enum names.

## Merge validation results

Final validation was run on the merged working tree with local pre-Chromix source fixtures supplied; no tests were deselected.

| Validation | Result | Scope |
|---|---|---|
| Patch linter | 120 patches; all 8 checks passed | Contiguous `0001`–`0120`, syntax and patch conventions |
| Full Python suite | 2,560 passed, 50 skipped, 1 existing warning | Includes added-patch source/API tests and full three-platform patch-chain tests |
| Node SDK suite | 108 passed | Node v24.8.0; launch, persistent seed, geometry, fonts and packaging regressions |
| Three-platform full patch chain | 3 passed; also included in full suite | Linux/macOS/Windows sparse copies, normal/restored equivalence, repeat/check mode, input bytes/mtime unchanged |
| Typed configuration and CSS harnesses | 22 passed; also included in full suite | Actual extracted C++ functions compiled with interface stubs, including uint64 boundaries and Chromium 152 pointer/hover enums |
| SDK cross-language geometry comparison | 1,000 seeds matched | Node/Python explicit geometry generation; not native window verification |
| Whitespace and merge conflicts | Passed; no unresolved entries | Working tree and staged diff checks |
| Matching Chromium build / real browser | Not run | No matching verified executable, no browser integration available, no new build dispatched |

The 50 skipped tests remain unverified. The warning comes from the existing intentional duplicate-ZIP-entry fixture. These results validate the merge's patch application and local contracts, not full-device fidelity or browser integration.

## Historical pre-merge results

The results below predate the merge and cover the storage/network/codec follow-up and expanded smoke probes.

| Validation | Result | Scope |
|---|---|---|
| `python3 tools/check_patches.py` | 110 patches; all 8 checks passed | Patch syntax, ordering, single-file scope and naming invariants |
| `python3 -m pytest -q` | 2,438 passed, 53 skipped, 1 warning | Full repository suite; skipped tests are not counted as verified |
| GPU/WebGPU standalone harness | 765 passed, 4 skipped | Complete patched `GPUAdapterInfo`, persona patches, platform gates, seed mapping, fallback, partial identities and capability preservation; provenance checks skipped without `CHROMIX_GPU_BASELINE_ROOT` |
| UA/locale/restored-context targeted tests | 40 passed, 6 skipped | Static platform/locale consistency and optional recovered-source checks |
| Storage/network/codec + smoke + Python SDK | 469 passed, no skips | Includes all new local-baseline provenance checks, native/patched C++ stubs and probe error/type handling |
| Three-platform full patch-chain tests | 3 passed | All 110 patches on Linux/macOS/Windows sparse source copies, normal/restored equivalence and unchanged source inputs |
| Node SDK tests | 93 passed on the follow-up run | Fixed local Node v24.8.0 executable; the earlier failure was PATH availability, not an absent installation |
| `git diff --check` | Passed | Whitespace checks |
| Read-only baseline SHA-256 comparison | 812 Linux, 812 macOS, 813 Windows files unchanged | Original verification inputs preserved |
| Real browser smoke | Not run | No supplied verified Chromix executable or installed Python Playwright |
| Matching Linux x64 `gn gen` / `ninja chrome` | Not run | No new build dispatched or local Chromium build performed |

The single Python warning is the existing duplicate-ZIP-entry test fixture warning. Storage/network/codec harnesses execute the patched callbacks/getters/helpers with dependency stubs, not real disk enforcement, IPC or decoding. An independent review found that explicit `usageDetails: null` could bypass probe validation; it is now rejected and covered by regression tests. Canvas standalone tests use AddressSanitizer and UndefinedBehaviorSanitizer; the real-header check is syntax-only. Media permission/device tests use stubs, including nonempty capability lists and separate permission listeners. The Resource Timing standalone comparison covers 11,264 input combinations. These are distinct from a browser run.

## Verification boundary

This round is local patch and test work. It does not dispatch another CI build, cancel existing runs, publish an asset, download a browser, or compile Chromium locally. A local Chromium executable and browser automation integration were not available at inspection time. Runtime and matching-build acceptance therefore remain open.

The Windows legacy cache migration in `build/windows/update-restored-source.ps1` is outside this paused Windows build scope. It contains older UA rewrite logic; its output must not be treated as validation of the updated patch series.
