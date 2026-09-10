// GeoIP is metadata only: it does not set ICE candidates or change routing.
import http from "node:http";
import https from "node:https";
import { isIP } from "node:net";

const GEOIP_URL = "http://ip-api.com/json/?fields=status,timezone,countryCode,query";
const MAX_RESPONSE = 64 * 1024;
const COUNTRIES = new Set(("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW").split(" "));
const LOCALES = {
  AE: "ar-AE", AR: "es-AR", AT: "de-AT", AU: "en-AU", BD: "bn-BD", BE: "nl-BE",
  BG: "bg-BG", BR: "pt-BR", CA: "en-CA", CH: "de-CH", CL: "es-CL", CN: "zh-CN",
  CO: "es-CO", CZ: "cs-CZ", DE: "de-DE", DK: "da-DK", EE: "et-EE", EG: "ar-EG",
  ES: "es-ES", FI: "fi-FI", FR: "fr-FR", GB: "en-GB", GR: "el-GR", HK: "zh-HK",
  HR: "hr-HR", HU: "hu-HU", ID: "id-ID", IE: "en-IE", IL: "he-IL", IN: "hi-IN",
  IS: "is-IS", IT: "it-IT", JP: "ja-JP", KR: "ko-KR", LT: "lt-LT", LV: "lv-LV",
  MX: "es-MX", MY: "ms-MY", NG: "en-NG", NL: "nl-NL", NO: "nb-NO", NZ: "en-NZ",
  PE: "es-PE", PH: "fil-PH", PK: "ur-PK", PL: "pl-PL", PT: "pt-PT", RO: "ro-RO",
  RS: "sr-RS", RU: "ru-RU", SA: "ar-SA", SE: "sv-SE", SG: "en-SG", SI: "sl-SI",
  SK: "sk-SK", TH: "th-TH", TR: "tr-TR", TW: "zh-TW", UA: "uk-UA", US: "en-US",
  VE: "es-VE", VN: "vi-VN", ZA: "en-ZA",
};

function proxyError() { return new Error("Invalid proxy URL or credentials"); }

export function splitProxy(proxy) {
  if (proxy == null || proxy === "") return undefined;
  const input = typeof proxy === "string" ? { server: proxy } : proxy;
  let url, username, password;
  try {
    if (typeof input.server !== "string" || /[\s\x00-\x1f\x7f]/.test(input.server)) throw proxyError();
    url = new URL(input.server.includes("://") ? input.server : `http://${input.server}`);
    if (!url.hostname || url.port === "0" || url.search || url.hash || !["", "/"].includes(url.pathname) ||
        !["http:", "https:", "socks:", "socks4:", "socks4a:", "socks5:", "socks5h:"].includes(url.protocol)) throw proxyError();
    username = url.username ? decodeURIComponent(url.username) : undefined;
    password = url.password ? decodeURIComponent(url.password) : undefined;
    if (input.server.includes("@")) {
      username ??= "";
      password ??= "";
    }
    if (input.username !== undefined) username = input.username;
    if (input.password !== undefined) password = input.password;
    for (const value of [username, password])
      if (value !== undefined && (typeof value !== "string" || /[\x00-\x1f\x7f]/.test(value))) throw proxyError();
  } catch { throw proxyError(); }
  return { server: `${url.protocol}//${url.host}`,
    ...(username !== undefined ? { username } : {}),
    ...(password !== undefined ? { password } : {}),
    ...(input.bypass !== undefined ? { bypass: input.bypass } : {}),
  };
}

export function extractProxyUrl(proxy) {
  const config = splitProxy(proxy);
  if (!config) return null;
  if (config.username === undefined && config.password === undefined) return config.server;
  const url = new URL(config.server);
  return `${url.protocol}//${encodeURIComponent(config.username ?? "")}:${encodeURIComponent(config.password ?? "")}@${url.host}`;
}

export function networkArgs(args = [], proxy) {
  const result = [...(args || [])];
  for (const arg of result) {
    if (/^--(?:fingerprint|uxr)-webrtc-(?:ip|fake-srflx(?:-allow-udp)?)(?:=|$)/.test(arg))
      throw new Error(`${arg.split("=", 1)[0]} is retired: fake ICE candidates do not route traffic. Configure a real proxy and --force-webrtc-ip-handling-policy=disable_non_proxied_udp instead.`);
  }
  if (proxy && !result.some((a) => a.split("=", 1)[0] === "--force-webrtc-ip-handling-policy"))
    result.push("--force-webrtc-ip-handling-policy=disable_non_proxied_udp");
  return result;
}

function validateGeoip(data) {
  if (!data || data.status !== "success" || typeof data.query !== "string" ||
      data.query.includes("%") || !isIP(data.query) || typeof data.timezone !== "string" ||
      data.timezone.length > 100 || !/^[A-Za-z0-9_+-]+(?:\/[A-Za-z0-9_+-]+)*$/.test(data.timezone) ||
      typeof data.countryCode !== "string" || !COUNTRIES.has(data.countryCode))
    throw new Error("GeoIP returned invalid status, IP, timezone or countryCode");
  try { new Intl.DateTimeFormat("en", { timeZone: data.timezone }); }
  catch { throw new Error("GeoIP returned an unknown timezone"); }
  // Country is not a language; leave unmapped regions unset rather than emit e.g. 'jp'.
  return { timezone: data.timezone, locale: LOCALES[data.countryCode] ?? null, exitIp: data.query };
}

export async function geoipHttp(proxyUrl, endpoint = GEOIP_URL) {
  const proxy = splitProxy(proxyUrl);
  const target = new URL(endpoint);
  if (target.protocol !== "http:" || target.username || target.password || target.hash)
    throw new Error("GeoIP endpoint must be an HTTP URL without credentials or fragment");
  const route = proxy ? new URL(proxy.server) : target;
  if (!["http:", "https:"].includes(route.protocol))
    throw new Error("GeoIP lookup supports only HTTP/HTTPS proxies, not SOCKS; set geoip=false and supply timezone/locale explicitly. No direct fallback.");
  const timeout = Number(process.env.CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS ?? 10);
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 60)
    throw new Error("CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS must be greater than 0 and at most 60");
  const headers = { Host: target.host, Accept: "application/json", "Accept-Encoding": "identity" };
  if (proxy && (proxy.username !== undefined || proxy.password !== undefined)) {
    if (proxy.username?.includes(":")) throw new Error("GeoIP Basic proxy username cannot contain ':'");
    headers["Proxy-Authorization"] = `Basic ${Buffer.from(`${proxy.username ?? ""}:${proxy.password ?? ""}`, "utf8").toString("base64")}`;
  }
  // No environment proxy discovery, bypass rules, redirects, retries or direct fallback.
  return new Promise((resolve, reject) => {
    let request, response, done = false;
    const finish = (error, value) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      response?.destroy();
      request?.destroy();
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => finish(new Error("GeoIP lookup timed out; no direct fallback")), timeout * 1000);
    try {
      request = (route.protocol === "https:" ? https : http).request({
        hostname: route.hostname.replace(/^\[|\]$/g, ""), port: route.port || undefined,
        servername: isIP(route.hostname.replace(/^\[|\]$/g, "")) ? "" : route.hostname,
        method: "GET", path: proxy ? target.href : target.pathname + target.search,
        headers, agent: false,
      }, (res) => {
        response = res;
        res.on("error", () => finish(new Error("GeoIP response failed; no direct fallback")));
        if (res.statusCode !== 200) return finish(new Error(`GeoIP HTTP ${res.statusCode}; redirects are disabled; no direct fallback`));
        if (Number(res.headers["content-length"]) > MAX_RESPONSE)
          return finish(new Error("GeoIP response exceeds 65536 bytes"));
        const chunks = [];
        let size = 0;
        res.on("data", (chunk) => {
          size += chunk.length;
          if (size > MAX_RESPONSE) return finish(new Error("GeoIP response exceeds 65536 bytes"));
          chunks.push(chunk);
        });
        res.on("end", () => {
          if (done) return;
          try { finish(null, validateGeoip(JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))))); }
          catch { finish(new Error("GeoIP returned invalid JSON, status, IP, timezone or countryCode")); }
        });
      });
      request.on("error", () => finish(new Error("GeoIP connection failed; no direct fallback")));
      request.end();
    } catch { finish(new Error("GeoIP request failed; no direct fallback")); }
  });
}
