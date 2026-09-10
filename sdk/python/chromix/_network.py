"""Bounded GeoIP metadata requests; never synthesize ICE candidates."""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

GEOIP_URL = "http://ip-api.com/json/?fields=status,timezone,countryCode,query"
MAX_RESPONSE = 64 * 1024
_COUNTRIES = set("AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split())
_LOCALES = dict(item.split(":") for item in (
    "AE:ar-AE AR:es-AR AT:de-AT AU:en-AU BD:bn-BD BE:nl-BE BG:bg-BG BR:pt-BR "
    "CA:en-CA CH:de-CH CL:es-CL CN:zh-CN CO:es-CO CZ:cs-CZ DE:de-DE DK:da-DK "
    "EE:et-EE EG:ar-EG ES:es-ES FI:fi-FI FR:fr-FR GB:en-GB GR:el-GR HK:zh-HK "
    "HR:hr-HR HU:hu-HU ID:id-ID IE:en-IE IL:he-IL IN:hi-IN IS:is-IS IT:it-IT "
    "JP:ja-JP KR:ko-KR LT:lt-LT LV:lv-LV MX:es-MX MY:ms-MY NG:en-NG NL:nl-NL "
    "NO:nb-NO NZ:en-NZ PE:es-PE PH:fil-PH PK:ur-PK PL:pl-PL PT:pt-PT RO:ro-RO "
    "RS:sr-RS RU:ru-RU SA:ar-SA SE:sv-SE SG:en-SG SI:sl-SI SK:sk-SK TH:th-TH "
    "TR:tr-TR TW:zh-TW UA:uk-UA US:en-US VE:es-VE VN:vi-VN ZA:en-ZA"
).split())


def split_proxy(proxy):
    if proxy is None or proxy == "":
        return None
    config = {"server": proxy} if isinstance(proxy, str) else proxy
    try:
        server = config["server"]
        if not isinstance(server, str) or re.search(r"[\s\x00-\x1f\x7f]", server):
            raise ValueError
        url = urlsplit(server if "://" in server else "http://" + server)
        if (not url.hostname or url.query or url.fragment or url.path not in ("", "/") or
                url.scheme not in ("http", "https", "socks", "socks4", "socks4a", "socks5", "socks5h")):
            raise ValueError
        host = "[" + url.hostname + "]" if ":" in url.hostname else url.hostname.encode("idna").decode("ascii")
        port = url.port
        if port == 0:
            raise ValueError
        authority = host + (":" + str(port) if port is not None else "")
        result = {"server": url.scheme + "://" + authority}
        for key, value in (("username", url.username), ("password", url.password)):
            if value is not None:
                if re.search(r"%(?![0-9a-fA-F]{2})", value):
                    raise ValueError
                value = unquote(value, encoding="utf-8", errors="strict")
            if key in config:
                value = config[key]
            if value is not None:
                if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
                    raise ValueError
                result[key] = value
        if "bypass" in config:
            result["bypass"] = config["bypass"]
        return result
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise ValueError("Invalid proxy URL or credentials") from None


def extract_proxy_url(proxy):
    config = split_proxy(proxy)
    if not config:
        return None
    if "username" not in config and "password" not in config:
        return config["server"]
    scheme, authority = config["server"].split("://", 1)
    return (scheme + "://" + quote(config.get("username", ""), safe="") + ":" +
            quote(config.get("password", ""), safe="") + "@" + authority)


def network_args(args, proxy=None):
    result = list(args or [])
    for arg in result:
        if re.match(r"^--(?:fingerprint|uxr)-webrtc-(?:ip|fake-srflx(?:-allow-udp)?)(?:=|$)", arg):
            raise ValueError(
                f"{arg.split('=', 1)[0]} is retired: fake ICE candidates do not route traffic. "
                "Configure a real proxy and --force-webrtc-ip-handling-policy=disable_non_proxied_udp instead.")
    if proxy and not any(a.split("=", 1)[0] == "--force-webrtc-ip-handling-policy" for a in result):
        result.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    return result


def _validate_geoip(data):
    if not isinstance(data, dict) or data.get("status") != "success":
        raise ValueError("GeoIP returned invalid status")
    ip, timezone, country = (data.get(k) for k in ("query", "timezone", "countryCode"))
    if not isinstance(ip, str) or "%" in ip:
        raise ValueError("GeoIP returned invalid IP")
    ipaddress.ip_address(ip)
    if (not isinstance(timezone, str) or len(timezone) > 100 or
            not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", timezone)):
        raise ValueError("GeoIP returned invalid timezone")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(timezone)
    except ImportError:
        if timezone != "UTC" and not any((Path(root) / timezone).is_file() for root in (
                "/usr/share/zoneinfo", "/usr/lib/zoneinfo", "/usr/share/lib/zoneinfo")):
            raise ValueError("GeoIP timezone cannot be validated") from None
    except (KeyError, ValueError):
        raise ValueError("GeoIP returned an unknown timezone") from None
    if not isinstance(country, str) or country not in _COUNTRIES:
        raise ValueError("GeoIP returned invalid countryCode")
    # A country code is not a language. Unknown language mappings stay unset.
    return timezone, _LOCALES.get(country), ip


def geoip_http(proxy_url, endpoint=None):
    proxy = split_proxy(proxy_url)
    target = urlsplit(endpoint or GEOIP_URL)
    if (target.scheme != "http" or not target.hostname or target.username is not None or
            target.password is not None or target.fragment):
        raise ValueError("GeoIP endpoint must be an HTTP URL without credentials or fragment")
    route = urlsplit(proxy["server"]) if proxy else target
    if route.scheme not in ("http", "https"):
        raise ValueError("GeoIP lookup supports only HTTP/HTTPS proxies, not SOCKS; set geoip=False "
                         "and supply timezone/locale explicitly. No direct fallback.")
    try:
        timeout = float(os.environ.get("CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS", "10"))
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError
    except ValueError:
        raise ValueError("CLOAKBROWSER_GEOIP_TIMEOUT_SECONDS must be greater than 0 and at most 60") from None
    headers = {"Host": target.netloc, "Accept": "application/json", "Accept-Encoding": "identity"}
    if proxy and ("username" in proxy or "password" in proxy):
        if ":" in proxy.get("username", ""):
            raise ValueError("GeoIP Basic proxy username cannot contain ':'")
        credentials = (proxy.get("username", "") + ":" + proxy.get("password", "")).encode("utf-8")
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
    # http.client never consults HTTP_PROXY/HTTPS_PROXY/NO_PROXY, even without a proxy.
    cls = http.client.HTTPSConnection if route.scheme == "https" else http.client.HTTPConnection
    conn = cls(route.hostname, route.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    transport = None

    def expire():
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        # System DNS is blocking; reject late connects before sending the request.
        conn.connect()
        transport = conn.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        transport.settimeout(remaining)
        path = target.geturl() if proxy else (target.path or "/") + ("?" + target.query if target.query else "")
        conn.request("GET", path, headers=headers)
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError(f"GeoIP HTTP {response.status}; redirects are disabled; no direct fallback")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE):
                raise ValueError("GeoIP response exceeds 65536 bytes or has invalid length")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                chunk = response.read1(min(8192, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError("GeoIP response exceeds 65536 bytes")
            if length is not None and size != int(length):
                raise ValueError("GeoIP response was truncated")
        try:
            return _validate_geoip(json.loads(b"".join(chunks).decode("utf-8")))
        except (ValueError, TypeError):
            raise ValueError("GeoIP returned invalid JSON, status, IP, timezone or countryCode") from None
    except (OSError, http.client.HTTPException):
        raise ValueError("GeoIP connection failed or timed out; no direct fallback") from None
    finally:
        timer.cancel()
        conn.close()
