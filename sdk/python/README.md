# chromix (Python)

Drive the Chromix stealth Chromium engine with a **CloakBrowser-compatible API** —
function names, keyword arguments, return types (Playwright `Browser` / `BrowserContext`)
and `CLOAKBROWSER_*` env-var names all match the [`cloakbrowser`](https://github.com/CloakHQ/CloakBrowser)
wrapper, so existing CloakBrowser scripts run on Chromix by changing only the import:

```diff
- from cloakbrowser import launch
+ from chromix import launch
```

```python
from chromix import launch

browser = launch(proxy="http://user:pass@proxy:8080", geoip=True, humanize=True)
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

## Install

```bash
pip install chromix playwright
```

The distribution and import package are both named `chromix`. To install the
SDK directly from a repository checkout instead, run:

```bash
pip install ./sdk/python playwright
```

On first launch the stealth Chromium binary is downloaded from this repo's GitHub
Release, SHA256-verified, and cached under `~/.cache/chromix`. Point
`CLOAKBROWSER_BINARY_PATH` at a local build (e.g. your own `chrome.exe`) to skip
the download.

## API

| Function | Description |
|---|---|
| `launch(**opts)` | Returns a Playwright `Browser` |
| `launch_async(**opts)` | Async variant |
| `launch_context(**opts)` | Returns a `BrowserContext` (native viewport by default) |
| `launch_context_async(**opts)` | Async variant |
| `launch_persistent_context(user_data_dir, **opts)` | Persistent profile |
| `launch_persistent_context_async(user_data_dir, **opts)` | Async variant |
| `build_args` / `get_default_stealth_args` | Arg assembly (32-bit random seed + native platform claim) |
| `maybe_resolve_geoip(geoip, proxy, tz, locale, args)` | Egress IP → (tz, locale, exit_ip) |
| `ensure_binary` / `clear_cache` / `binary_info` / `check_for_update` | Binary management |
| `HumanConfig` / `resolve_human_config` | Behavioral-layer config (`default` / `careful` presets) |
| `ProxySettings` | Playwright-shaped proxy TypedDict |

Options (`headless, proxy, args, stealth_args, timezone, locale, geoip, humanize,
human_preset, human_config, extension_paths, license_key, browser_version,
release_channel, user_agent, viewport, color_scheme`) match CloakBrowser
name-for-name; `**kwargs` passes through to `playwright.chromium.launch()` /
`browser.new_context()`.

Persistent contexts create `.chromix-fingerprint-seed` inside
`user_data_dir` on first stealth launch and reuse it thereafter. The file is
one decimal 32-bit seed followed by a newline, uses the same format as the
Node SDK, and is published atomically for concurrent first launches. An
explicit `--fingerprint=...` in `args` wins without creating or rewriting the
file; `stealth_args=False` also skips seed I/O. Defaults claim the native
persona: `linux`, `windows`, or `macos`. Default viewport geometry is native.
Seeded synthetic geometry now requires `args=["--uxr-synthetic-device-tests=true"]`;
that test-only mode retains deterministic cross-SDK templates. Explicit viewport
options still win outside measured mode.

### Measured device launch

`launch_context(device_pool={"host": "record.json", "records": ["record.json"],
"seed": "42"})` validates whole evidence bundles and native host capabilities,
then checks five live contexts before returning. Async and persistent context
variants support the same option; the async persistent directory is keyword-only.
Point `CLOAKBROWSER_BINARY_PATH` at the collected executable. Evidence defaults to
a 24-hour maximum age, and extra launch/context overrides are rejected. Persistent
profiles bind record and seed rather than rotating identities. Browser-returning
`launch` does not support this option. See [device pool documentation](../../docs/device-pool.md)
for collection, configuration, native fallback and remaining acceptance limits.

### Custom font directory

`fonts_dir="path/to/fonts"` parses `.ttf` / `.otf` / `.ttc` family names and,
on Linux, configures the actual Fontconfig directory. It does not install fonts
into the Windows/macOS font backend or prove the file used for each glyph.
Family whitelisting, substitution and persona fallback now require
`--uxr-synthetic-device-tests=true`; normal launches keep native font selection.
An explicit whitelist overrides SDK-generated names only in that test mode.
Measured device mode rejects `fonts_dir` and other per-field overrides.

```python
browser = launch(fonts_dir="C:/fontsets/win11-segoe-only")
```

## Env vars

- `CLOAKBROWSER_BINARY_PATH` — use a local chrome binary instead of downloading
- `CLOAKBROWSER_VERSION` / `CLOAKBROWSER_RELEASE_CHANNEL` — pin a version/channel
- `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS` — geoip lookup timeout
- `CLOAKBROWSER_WIDEVINE_CDM` — explicit Widevine CDM dir (DRM); `CLOAKBROWSER_WIDEVINE=0` disables DRM
- `CHROMIX_CACHE_DIR` / `CHROMIX_DOWNLOAD_HOST` — cache location / release host override

High-risk engine ports are available only through explicit browser `args`:

```python
browser = launch(args=[
    "--fingerprint-devtools-runtime-suppression",
    "--fingerprint-canvas-bridge=127.0.0.1:9228",
    "--fingerprint-canvas-bridge-unsafe",
])
```

Runtime suppression can break console/binding-based automation. Canvas Bridge
removes the sandbox from bridge renderer processes and forwards canvas/WebGL
operations to the configured endpoint.

### Proxy and GeoIP behavior

GeoIP is metadata, not a routing mechanism. The lookup uses the effective
HTTP/HTTPS proxy and does not inherit environment proxies or `NO_PROXY`
bypasses. Failed lookups do not fall back to the host connection. SOCKS
remains a browser proxy option; use `geoip=False` and explicit `timezone`
/ `locale` for SOCKS.

With a proxy, the SDK defaults to the native
`--force-webrtc-ip-handling-policy=disable_non_proxied_udp` unless an explicit
native policy was supplied. This does not guarantee the routing of all DNS,
HTTP, QUIC or operating-system traffic.

The SDK rejects `--fingerprint-webrtc-ip`, `--fingerprint-webrtc-fake-srflx`
and `--fingerprint-webrtc-fake-srflx-allow-udp` (including `uxr` equivalents).
GeoIP no longer appends an ICE address override. Its HTTP metadata service
is unauthenticated and must not be treated as proof of an exit route.

GeoIP lookup failures now raise `ValueError`. The timeout defaults to 10
seconds and accepts values greater than zero and at most 60. Python's
synchronous DNS/connection setup cannot always be interrupted at that
deadline; a late connection is rejected before sending the GeoIP request.
IANA timezone data must be installed for timezone validation. Creating a
later context with another proxy does not recompute browser-level locale
or timezone.

## CLI

```bash
python -m chromix install      # pre-download the binary
python -m chromix info         # binary / cache info
python -m chromix widevine     # fetch the Widevine CDM (Linux x64)
python -m chromix clear-cache
```

## Intentional differences from CloakBrowser

1. `license_key` is accepted and ignored (one open tier).
2. `geoip` queries ip-api.com over HTTP instead of a local GeoLite2 database;
   explicit `timezone=` / `locale=` always win.
3. No `cloakbrowser/puppeteer` subpath — use the Playwright surface.
4. Widevine is enabled automatically when a CDM is present (installed Chrome,
   `CLOAKBROWSER_WIDEVINE_CDM`, or `python -m chromix widevine`).

## License

The Python SDK is available under the BSD 3-Clause License. See
[`LICENSE`](LICENSE).
