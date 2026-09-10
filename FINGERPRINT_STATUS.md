# Fingerprint backlog coverage

This record tracks the implementation requested by `/root/fingerprint-p0-p2-backlog.md` against Chromium **152.0.7977.82**. A persona is the configured browser identity for one launch. Returning configured values is not sufficient if permissions, rendering, network traffic, or actual capabilities disagree.

## Acceptance rules

Latest Canvas follow-up: legacy readback/export noise and the remote Canvas
Bridge now require `--uxr-synthetic-device-tests=true`; default execution retains
native pixels. A standalone five-context, three-launch codec/color/alpha audit
and offline oracle regressions are added; see [Canvas chain](docs/canvas-chain.md).
The installed Chrome 153 control fails this audit. It is not the matching
Chromium 152 build, so this is diagnostic evidence, not patched-build acceptance.
Historical validation counts below do not apply to these latest edits.

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
| WebRTC | Preserve native candidate/SDP/STUN success data; retire address substitution. Persona policies only restrict existing routes, and `obfuscate` requests native mDNS without re-enabling UDP. Validate unusable STUN mapped addresses as failures and keep per-server success/failure sets disjoint, so a keepalive error cannot finish gathering while another server is pending. | Real connection through the configured proxy/TURN server; IPv4/IPv6/mDNS/ports, related addresses, codec capabilities, and stats. SDK proxy/GeoIP loopback tests are not ICE connectivity acceptance. |
| MediaDevices | Preserve browser-provided permission filtering, salted IDs, ordering, and empty enumerations instead of inventing unusable devices. | Real permission transitions, device constraints, origin separation, and profile-restart stability. No virtual media backend is implemented. |
| Intl / ICU | Move locale initialization before JS use rather than changing process-global ICU state from a language getter. | Default and explicit locale/calendar/numbering/hour-cycle cases across all contexts; ICU fallback, timezone initialization, and restart tests. |
| Canvas | Coordinate-correct RGBA/BGRA readback, transparent-pixel preservation and private-copy encoding; suppress duplicate upstream noise. Async idle/worker/thread-pool encoding prepares and owns one buffer, with failure completion and late-task guards. | Native browser PNG/JPEG/WebP and ImageBitmap results, F16 privacy, transformed color-space/premultiplication agreement, and common WebGL/WebGPU origin/profile seed isolation remain open. |
| WebAudio | Preserve mutable AudioBuffer data, silence, native analyser projections, and the actual rendering sample rate. | A coherent graph-level privacy mechanism; OfflineAudioContext, compressor/oscillator output, channel counts, and base/output latency. Getter-only noise is not a graph-level solution. |
| WebGPU | Feature allowlists intersect real support; explicit empty sets deny features, and embedded NUL tokens cannot alias valid names. Adapter/device limits and requested alignment validation retain Dawn semantics. Native preferred format preserves backend interoperability restrictions. Synthetic GPU identity remains one complete tuple with native software fallback status. | Subgroup data and real canvas configure/copy/map/request tests remain unverified on hardware. The synthetic Windows GPU templates are declared test templates, not measured-device samples or market-share weights. |
| Screen / Window / Viewport | Existing screen, outer-window, DPR and detailed-screen getters. | Actual layout, inner dimensions, visualViewport, CSS device size/DPR, zoom, and multi-screen agreement. |
| Performance Timing | Remove the recursive synthetic network-phase fallback and preserve native ordering/zero rules. | Navigation/resource/paint/event/longtask/worker/RAF precision and background lifecycle tests. |

## P1 coverage

| Category | Status and next verification |
|---|---|
| Permissions | Notification getters/query results must use the browser authority also used by requests and observers. Clipboard, USB, Bluetooth, serial, sensors, and geolocation need end-to-end verification. Never bypass a permission check to make a reported status appear consistent. |
| Codecs / MSE / EME | MediaCapabilities filters intersect native results, including WebRTC callbacks and missing-history fallback; EME fallback retains key-system access. Mixed recording configs also check their audio codec. MediaRecorder MIME queries remain native, and synchronous encoder-start failure restores inactive state. A shared codec policy across playback, MSE, EME, WebRTC and actual decoding remains open. Queries and standalone callback tests do not verify a real encoder/decoder. |
| Storage | Removed seed-derived and explicit renderer-only quota replacement. StorageManager and buckets retain browser accounting, individual limits and error paths. Backend quota enforcement, persistence and IndexedDB/Cache partitioning still require real browser tests. |
| Network Information | Removed RTT/downlink-only overrides so getters, cached state, effectiveType, saveData and native change events share the notifier again. Real network transitions and any future notifier-level test policy remain unverified. |
| Font provenance | Default native selection; legacy whitelist/substitution/fallback require synthetic-test opt-in. Probe v2 checks CSS/Canvas/loading samples; CDP audit records platform font names and glyph counts. File-to-glyph binding and rasterization remain open. |
| Plugins / MIME / PDF | A reported PDF plugin cannot create a missing/disabled viewer. Verify actual PDF display and extension exposure. |
| Input / device capabilities | Keyboard map overrides do not change actual key/code input. Touch/pointer CSS, gamepad, orientation, motion, and sensors remain open. |
| CSS media features | Existing overrides cover selected preferences, not full gamut/HDR rendering, print media, scrollbars, and layout. |
| WebGL | Native readback bytes/errors/pack layout are retained across CPU/PBO/float paths; removed CPU-only postprocessing and unsafe bridge replacement. Extension enumeration and requests share one native support filter; shader precision stays native and limit clamps preserve zero capability. A common backend-level readback privacy mechanism is not implemented; real GPU acceptance remains open. |
| Wasm / SIMD / threads / SharedArrayBuffer | Legacy CPU/RAM getter overrides require synthetic-test opt-in; heap remains native. Probe v2 executes Wasm/SIMD and bounded memory growth. Isolated-origin audit tests SAB/Atomics worker transfer. Real maximum heap allocation and matching native-build acceptance remain open. |

## P2 coverage

| Category | Status and next verification |
|---|---|
| TLS ClientHello | No persona TLS configuration layer established. Capture and compare negotiated TLS versions, cipher suites, extensions, GREASE, and signature algorithms. |
| ALPN / HTTP/2 / HTTP/3 | Preserve valid negotiation; no completed persona policy for protocol selection. |
| HTTP/2 | SETTINGS, priority, initial windows and pseudo-header ordering require wire-level tests. |
| HTTP/3 / QUIC | Transport parameters and connection retry behavior require packet-level verification. |
| HTTP headers | UA and Accept-Language need joint JS/server checks. Accept-Encoding must match real decoder support. |
| DNS / proxy / connection reuse | SDK GeoIP metadata requests use the effective HTTP/HTTPS proxy without environment bypass or direct fallback; proxied launches default to the native non-proxied-UDP restriction. WebRTC policy is not an HTTP/QUIC/DNS routing policy. Verify actual routes, IPv4/IPv6 priority and connection reuse; TLS/HTTP personas remain unimplemented. |
| Date / timezone / DST / Temporal | Existing timezone substitution does not complete initialization, DST boundary, host timezone change, and Temporal testing. |
| Timer quantization | No unified persona quantization for Date.now, performance.now, RAF and IdleCallback. |
| Page lifecycle | Preserve native visibility, throttling, freeze/resume behavior until a specified, tested policy exists. |

## Real-device pool gaps and next priorities

An evidence collector and conservative whole-record preflight selector now live
in `tools/collect_device.py` and `tools/device_pool.py`; see
[`docs/device-pool.md`](docs/device-pool.md). They collect five browser contexts,
host inventory and restart/profile observations, reject inconsistent evidence,
and select only exact backend-observation matches. `tools/device_wire_evidence.py`
parses existing packet captures without treating protocol presence as verification.
Measured selection now gates the Python sync/async and Node context/persistent
launch APIs, rejects field overrides, binds persistent record/seed manifests and
verifies all five contexts before returning them. Default SDK geometry is native;
independent C++ CPU/RAM/display/GPU pools now require the explicit
`--uxr-synthetic-device-tests=true` flag. Heap limits retain V8's native value.
Legacy explicit CPU/RAM/GPU identity overrides and font substitution/whitelisting
also require that test flag. These are native-default changes, not backend emulation.
No reviewed real-hardware pool or cross-device backend emulation is bundled.
Stock Chrome launch workflow tests pass; matching native Chromix build acceptance
and real wire capture validation remain open.

The current three Windows GPU records are synthetic templates. No measured full-device corpus, sample provenance, or measured population distribution is implemented. Seed stability is implemented, but it does not turn independently configured values into real device samples.

| Priority | Gap confirmed in the current patches | Required implementation or evidence |
|---|---|---|
| 1 | Screen/window/DPR overrides remain separate from actual layout. `screen.avail*` is not bounded by the declared screen, window coordinates reject valid negatives, and detailed-screen flags do not remove extra screens. | Configure display geometry at the browser/emulation layer; validate inner/outer/visualViewport, CSS resolution queries, zoom, orientation changes and multiple monitors together. |
| 1 | Legacy CPU/deviceMemory overrides now require synthetic-test opt-in. UA bitness and explicit heap getters no longer replace V8's native limit. | Wasm/SIMD/memory-growth and COOP/COEP/SAB harness passed on stock Chrome. Maximum JS heap allocation and matching patched-browser integration remain unverified. |
| 1 | Native font selection avoids unsupported family substitution by default; CDP platform-font names do not prove the source file. | Bind glyphs to font hashes and validate fallback/rasterization on each supported OS. CSS/Canvas/loading matrix is implemented, not full shaping acceptance. |
| 2 | GPU identity templates do not prove the native backend implements a corresponding physical device. Linux/macOS/Windows ARM retain native identity. | Collect internally consistent identity/capability records with browser/driver/build versions, then validate requestable features and rendered/readback results on the matching hardware. |
| 2 | Media devices, audio, codec queries, permissions and capture are not a single device model. | Use actual or explicit virtual device backends; test permission transitions, capture, latency, decoding and playback without inventing support. |
| 2 | Network/proxy/DNS/WebRTC/TLS/HTTP behavior has no unified measured policy. | Verify actual routes and packet-level behavior separately from JavaScript estimates; network and storage state must be allowed to change normally. |

Measured records retain collection provenance, executable hash, OS/driver inventory
and correlated observations. Selection only admits exact native matches and binds
whole records to persistent profiles. Unsupported candidates use verified native
fallback; partial device copying is not implemented. Expanding beyond exact-native
records still requires real backend support and hardware acceptance evidence.

### P0-3 Through P0-6 Implementation Update

| Item | Implemented | Still open |
|---|---|---|
| P0-3 CPU/memory/execution | Explicit getter override gates; native heap; five-context Wasm/SIMD execution, bounded memory growth and isolated SAB/Atomics roundtrip. | Maximum JS heap allocation, worker scheduling behavior and matching native build. |
| P0-4 GPU backend | Native default identity/GL limits; WebGL shader/texture readback and extension requests; WebGPU requestDevice/compute/map/texture readback. | Full features/limits/shader/format boundary matrix, device-loss tests and physical driver/backend attribution. |
| P0-5 Font source | Native selection gates; 20 CSS/Canvas/Font Loading samples including CJK/emoji/missing characters; CDP platform-font evidence. | Font-file-to-glyph binding, rasterization and cross-platform shaping. |
| P0-6 Network identity/routes | Five-context local UA/CH/header comparison; offline decoded protocol fields and captured-egress endpoint policy checks. | Real proxy/DNS/QUIC/WebRTC capture, complete route/process attribution and TLS/HTTP2 profile comparison. |

`tools/device_p0_audit.py` passed on stock Chrome 153.0.8010.37 in both isolation
modes. That is harness verification only. No matching Chromix build or real wire
capture was accepted, so these P0 items are not marked complete.

## Compatibility changes

Renderer-only overrides that contradicted actual browser behavior are retired; capability filters that remain enabled are constrained as follows:

- `--uxr-storage-quota` / `--fingerprint-storage-quota` no longer changes only reported storage capacity, and Canvas seeds no longer generate fictitious quotas. Origin estimates and individual bucket limits remain browser-owned.
- `--uxr-net-rtt` and `--uxr-net-downlink` no longer override isolated getters. Network Information values and change events remain notifier-owned; zero RTT/downlink is a valid observation.
- `--uxr-codec-*` / `--fingerprint-codec-*` filters only restrict native supported/smooth/power-efficient results. They cannot enable a decoder, smooth playback or hardware acceleration. `--uxr-codec-matrix` no longer bypasses MediaRecorder's native MIME/encoder checks.
- Canvas export no longer applies the legacy random transform inside `ImageEncoder`. Valid persona seeds use the prepared Canvas buffer once; `--uxr-disable-fingerprint-noise` keeps that path unmodified. Without a valid persona seed, the optional legacy Canvas readback feature can still affect `getImageData`; legacy readback/export privacy is not a unified implementation.
- `--uxr-webgpu-canvas-format` no longer replaces the backend's preferred format; an application can explicitly request a supported format through normal WebGPU configuration.
- WebGL readback no longer applies CPU-only noise or replaces packed pixels from Canvas Bridge. PBO, floating-point, error and native pack semantics remain intact; unified GPU privacy is still open.
- The SDKs reject `--fingerprint-webrtc-ip`, `--fingerprint-webrtc-fake-srflx`, `--fingerprint-webrtc-fake-srflx-allow-udp` and their `uxr` counterparts. Direct engine `uxr` retirement flags produce a WebRTC configuration error. Use real proxy/TURN routes and native IP handling policy; synthetic addresses are not routing.
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
  --seed 0x100000001 --other-seed 0x8000000000000001 \
  --output /tmp/chromix-fingerprint-smoke.json
```

Use `--expected-sha256` with an independently obtained **executable** hash when available. The release archive hash is not the executable hash. `--no-sandbox` requires explicit opt-in; the runner never adds it automatically for root.

The runner serves only its own loopback HTTP origin, rejects external proxy requests, restricts page/worker requests and WebSockets, and disables selected browser background networking. These controls are not an operating-system network sandbox. It grants no media permissions and invokes capture only inside a document whose policy denies camera/microphone access.

Checks cover negotiated HTTP hints versus JS in window/iframe/dedicated worker, requested platform/default Intl locale, Canvas crops/transparent borders/PNG export/ImageBitmap/F16 stability, WebGL shader/readback/error/pack behavior, WebGPU request/configure/copy/map, offline oscillator/compressor/silence/mutable PCM and PCM16 WAV decode, negative codec queries, denied media behavior, reload and profile restart. The runner also exercises offline/online events in window and iframe and checks a loopback fetch's ResourceTiming.

Network Information and StorageManager estimates are collected in all three contexts and checked for types/ranges and explicit API failures. Dynamic estimates and quotas are excluded from identity/restart comparisons; existing usage above a reduced quota is allowed. Offline emulation is not real throughput, proxy/TURN routing, DNS, disk enforcement or storage-bucket acceptance. Each persona/seed family gets a fresh temporary profile; restarts reuse that profile. The control uses the same executable with `--fingerprint=off` and no locale/platform overrides, not a stock Chromium binary. Missing evidence, crashes, mismatches and unexpected external requests fail the report and exit code. Explicitly unsupported optional APIs are reported separately; they do not complete a backlog category. Node mocks and validator fixtures are never marked as browser runtime evidence.

## Canvas / GPU / media / network integration (2026-09-10)

This local work adds Canvas async and encoder patches `0121`–`0124` to the active series. The constructor prepares one encoding buffer after dimension limiting and makes a private copy before noise or alpha conversion; idle tasks keep ownership and worker/encoder tasks receive it without reapplying noise. The shared ImageEncoder no longer randomly mutates its input, so synchronous, worker and thread-pool encoding do not add a second transform or bypass the noise-disable switch. Conversion/allocation failure completes the callback or promise, and late idle tasks do not reinitialize released state. F16 pixels retain native representation without an invented privacy transform.

Canvas parses strict decimal nonzero uint64 seeds and mixes their upper bits into the existing 32-bit pixel hash; legacy uint32 output is retained. This is a many-to-one noise hash, not a collision-free 64-bit mapping. The GPU seed helper also consumes high seed bits and no longer caches configuration before renderer initialization. A bare browser `--fingerprint` generates a nonzero uint64 seed; SDK saved/default seeds remain the compatible uint32 format.

GPU changes preserve native shader precision, readback errors and packing, backend-preferred WebGPU format, and zero hardware limits. WebGL extension filtering now applies to both enumeration and requests. Restoring native readback avoids the inconsistent CPU-only transform; it does not implement common Canvas/WebGL/WebGPU rendering privacy.

Media fixes preserve real MIME/encoder checks, restore MediaRecorder state and clean up partial backend initialization after failed start, check audio support in mixed recording queries, retain EME key-system access on history-service fallback, and discard WebRTC capability callbacks after context destruction. Shared playback/MSE/EME/WebRTC codec policy and graph-level audio privacy remain open.

WebRTC now retains native candidates and SDP and rejects unusable STUN mapped addresses. Success and failure sets remain disjoint: a failed keepalive cannot double-count an already successful server, and a late success replaces its earlier failure. Malformed responses from an established allocation still schedule the next native keepalive while within its lifetime; a first invalid response completes as a failure without starting a retry chain. Completion still waits for outstanding servers and native mDNS readiness; errors retain their native notification path. Extracted allocator tests check that another server's candidate arrives before completion, not just the set sizes.

The SDKs use the effective proxy for GeoIP, reject failed or invalid metadata and retired candidate flags, and default proxied launches to Chromium's native non-proxied-UDP restriction. Explicit native policy still wins. Tests use only local proxy/metadata servers. GeoIP's existing HTTP service is unauthenticated; Python synchronous DNS/connect can overrun its nominal timeout, but a late connection is rejected before sending the metadata request. A later context with another proxy does not recompute browser-level locale/timezone. Real routing, DNS, TLS and HTTP protocol acceptance remain separate work.

## Current domain validation results (2026-09-10)

These results cover the uncommitted Canvas/GPU/audio/media/network changes on top of `167c468`, including patches `0121`–`0124` and the final STUN keepalive correction. Validation finished at **09:48:47 UTC**; no tests were deselected.

| Validation | Result | Scope |
|---|---|---|
| Patch linter | 124 patches; all 8 checks passed | Contiguous `0001`–`0124`, syntax, naming and patch conventions |
| Full Python suite | 3,182 passed, 31 skipped, 1 existing warning | Includes source provenance, compiled extracted C++ contracts, SDK loopback tests and browser-probe validators |
| Node SDK suite | 137 passed, no skips | Node v24.8.0; proxy precedence/authentication, GeoIP failure handling, launch/context options and packaging |
| Three-platform full patch chain | 3 passed; included in full suite | Linux/macOS/Windows sparse copies, zero-fuzz application, normal/restored equivalence and repeat/check mode; not native builds |
| Canvas targeted tests | 53 passed, no skips | ASan/UBSan, uint64 boundaries, async ownership, read-only encoder input, original-mutation control and real-header syntax checks; codecs remain stubbed |
| WebRTC targeted tests | 258 passed, no skips | Native extracted success/failure/completion and allocator methods, plus owned pending-request queue and lifetime cases |
| Keepalive mutation control | Passed | Reintroducing the early return in a temporary harness makes the pending-request assertion fail; no live STUN server or timer |
| Input integrity | 4,937 source/fixture inputs unchanged | SHA-256 and mtime comparisons; 230 implementation/test files also unchanged throughout the final run |
| Whitespace / merge state | Passed | `git diff --check`; no unresolved entries; build/CI implementation unchanged |
| Matching Chromium build / browser | Not run | No matching verified executable, Python Playwright or browser MCP tool; no build or download initiated |

Thirty skipped tests require PowerShell; one requires a native Windows junction. The warning is the existing intentional duplicate-ZIP-entry fixture. Browser probes were expanded but only their validators/mocks were executed. The suite does not verify native GPU rendering, codecs, ICE/TURN connectivity, DNS/proxy routing, GC/thread behavior or packet-level TLS/HTTP fingerprints.

Local evidence is retained under `/tmp/chromix-fingerprint-domains-20260910T082739Z/`: `final-keepalive/` contains final logs, JUnit XML, file-integrity checks and `acceptance-summary.json`; earlier failed runs remain preserved. Two obsolete assertions were corrected during integration. Independent source review found the downstream Canvas mutation, STUN double terminal counting and truncated keepalive chain; each received a targeted fix and regression coverage.

## Historical merge integration (2026-09-10)

The local merge of `origin/main` at `a727c817` retains the earlier Canvas copy/bounds fixes, notifier-owned network state, mutable AudioBuffer data, native WebGPU capabilities, and shared persistent SDK seeds. It also preserves the correct Windows restored-source patch paths and `fingerprint_data.h` build input. The duplicate trailing time-clamper patch is removed rather than applied twice.

Incoming CPU/memory and display templates are synthetic, not a measured device corpus. Configuration seeds accept the full unsigned 64-bit range and use integer-weight selection instead of standard-library-specific floating distributions. Typed configuration accessors support the merged GPU code. The incoming independent seed-derived `outerHeight` and DPR defaults are not enabled: `screen` and actual layout do not consume the same defaults, so explicit configuration and native fallbacks are retained. The SDK supplies explicit geometry separately; full browser layout agreement still requires runtime validation.

The new planning documents under `docs/` describe future work, not completed cross-process integration. Font mappings do not prove glyph-file provenance; `0112` only adds includes and does not implement layout-theme system fonts. Speech voice-list configuration changes reported entries, not the installed speech backend. Added battery, shader, font and timing hooks likewise require matching-build and browser validation before their categories can be accepted. Native audio latency and maximum-channel reporting are retained rather than enabling getter-only replacements. The CSS pointer/hover patch is rebased to the actual Chromium 152 method locations and uses its declared Mojo enum names.

## Historical merge validation results

These results apply to merge commit `167c468`, before the Canvas/GPU/media/network work described above. Validation used local pre-Chromix source fixtures; no tests were deselected.

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
