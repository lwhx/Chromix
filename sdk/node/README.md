# @xiaozhou26/chromix

Drive the Chromix Chromium engine with a **CloakBrowser-compatible API**.
Function names, option names (camelCase), and return types (Playwright
`Browser` / `BrowserContext` via `playwright-core`) match the
[`cloakbrowser`](https://github.com/CloakHQ/CloakBrowser) wrapper, so existing
CloakBrowser scripts can migrate by changing the import:

```diff
- import { launch } from 'cloakbrowser';
+ import { launch } from '@xiaozhou26/chromix';
```

```javascript
import { launch } from '@xiaozhou26/chromix';

const browser = await launch({
  proxy: 'http://user:pass@residential-proxy:port',
  geoip: true,       // match timezone + locale to proxy IP
  headless: false,
  humanize: true,    // human-like mouse, keyboard, scroll
});
const page = await browser.newPage();
await page.goto('https://example.com');
await browser.close();
```

Convenience wrappers:

```javascript
import {
  launchContext,
  launchPersistentContext,
} from '@xiaozhou26/chromix';

const context = await launchContext({
  userAgent: 'Custom UA',
  viewport: { width: 1920, height: 1080 },
});
const persistentContext = await launchPersistentContext({
  userDataDir: './chrome-profile',
  headless: false,
});
```

## Install

```bash
npm install @xiaozhou26/chromix playwright-core
```

The unscoped npm name `chromix` belongs to an unrelated project. This SDK is
published under the `@xiaozhou26` scope; use the full scoped name when
installing or importing it.

The SDK has no production dependencies of its own and loads an installed
`playwright-core` or `playwright` package at launch time. On first launch, the
Chromix binary is downloaded from this repository's GitHub Release,
SHA256-verified when the release manifest is available, and cached under
`~/.cache/chromix`. Point `CLOAKBROWSER_BINARY_PATH` at a local build to skip
the download.

## Options

CloakBrowser options work unchanged: `headless, proxy, args, stealthArgs,
timezone, locale, geoip, humanize, humanPreset, humanConfig, userAgent,
viewport, colorScheme, extensionPaths, browserVersion, releaseChannel,
licenseKey, contextOptions, launchOptions, userDataDir` (+ `startMaximized`).

Environment variables: `CLOAKBROWSER_BINARY_PATH`, `CLOAKBROWSER_VERSION`,
`CLOAKBROWSER_RELEASE_CHANNEL`, `CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS`,
`CLOAKBROWSER_WIDEVINE_CDM` / `CLOAKBROWSER_WIDEVINE=0` (DRM), and
`CHROMIX_CACHE_DIR` / `CHROMIX_DOWNLOAD_HOST` (cache / release host override).

## Intentional differences from CloakBrowser

1. `licenseKey` is accepted and ignored (one open tier).
2. `geoip` queries ip-api.com instead of a local GeoLite2 database.
3. No `cloakbrowser/puppeteer` subpath is provided; use the Playwright surface.
4. Widevine/DRM is enabled automatically when a CDM is present (installed
   Chrome or `CLOAKBROWSER_WIDEVINE_CDM`); on Linux, fetch one with
   `python -m chromix widevine`.

High-risk engine ports are available only through explicit `args`:

```javascript
const browser = await launch({ args: [
  '--fingerprint-devtools-runtime-suppression',
  '--fingerprint-canvas-bridge=127.0.0.1:9228',
  '--fingerprint-canvas-bridge-unsafe',
  '--fingerprint-webrtc-fake-srflx=203.0.113.20',
  '--fingerprint-webrtc-fake-srflx-allow-udp',
] });
```

Runtime suppression can break console/binding-based automation. Canvas Bridge
removes the sandbox from bridge renderer processes and forwards canvas/WebGL
operations to the configured endpoint. Fake srflx does not enable non-proxied
UDP unless the separate `allow-udp` flag is supplied.

## CLI

After installation, the package provides the `chromix` executable:

```bash
npx chromix --version
npx chromix install       # pre-download the binary
npx chromix info          # binary / cache info
npx chromix clear-cache
```

Run the registry package without installing it first:

```bash
npx @xiaozhou26/chromix --version
```

## Versioning

The npm package follows SemVer independently of Chromium's four-part version.
The bundled SDK currently targets Chromium source `152.0.7977.82`; the actual
binary release selected by `stable` or `latest` is shown by `chromix info`.

## License

The Node SDK is available under the BSD 3-Clause License. See [`LICENSE`](LICENSE).
