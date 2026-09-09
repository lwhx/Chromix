# Fingerprint coverage matrix

Remaining Camoufox parity work:

- UA Client Hints: brands, mobile, platformVersion, architecture, bitness, model, wow64.
- Intl: DateTimeFormat, NumberFormat, Locale region/script/calendar/numberingSystem.
- Permissions, webdriver descriptors, PluginArray/MimeTypeArray shape.
- devicePixelRatio coherence with screen/window/canvas backing.
- WebRTC IPv4/IPv6/local/mDNS candidates and ICE ordering.
- HTTP headers, HTTP/2 and TLS ClientHello coherence.
- WebGPU adapter identity, features, limits, architecture.
- Audio outputLatency/maxChannelCount and per-context seed.
- Fonts CSS2 keywords, spacing noise, per-context list.
- Window history/scroll/body dimensions/orientation.
- Media connection/MediaCapabilities/Notification.permission.
- WebAuthn/PDF and performance timing surfaces.

The patch layer is one-file and must be rebased against the pinned Chromium source before compilation.
