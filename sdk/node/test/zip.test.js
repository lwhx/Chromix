// Offline download/extraction integration tests; fixtures are real ZIP bytes.
import { after, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync, existsSync, statSync, lstatSync, readlinkSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { Readable } from "node:stream";
import { deflateRawSync } from "node:zlib";

const hostPlatform = process.platform;
const cache = mkdtempSync(join(tmpdir(), "chromix-zip-"));
const originalCache = process.env.CHROMIX_CACHE_DIR;
process.env.CHROMIX_CACHE_DIR = cache;
const binary = await import("../_binary.js");
const api = await import("../index.js");
if (originalCache === undefined) delete process.env.CHROMIX_CACHE_DIR;
else process.env.CHROMIX_CACHE_DIR = originalCache;
after(() => rmSync(cache, { recursive: true, force: true }));
beforeEach(() => {
  for (const name of readdirSync(cache)) rmSync(join(cache, name), { recursive: true, force: true });
});
const host = "https://fixtures.invalid/release";
const tag = binary.CHANNELS.stable.tag;
const options = { releaseChannel: "stable" };
const file = (name, data = "fixture", mode = 0o100644) => ({ name, data, mode });
const link = (name, target) => file(name, target, 0o120777);

function crc32(data) {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function zipFixture(entries) {
  const local = [], central = [];
  let offset = 0;
  for (const { name, data, mode } of entries) {
    const filename = Buffer.from(name), bytes = Buffer.from(data), compressed = deflateRawSync(bytes);
    const header = Buffer.alloc(30);
    header.writeUInt32LE(0x04034b50, 0);
    header.writeUInt16LE(20, 4);
    header.writeUInt16LE(8, 8);
    header.writeUInt32LE(crc32(bytes), 14);
    header.writeUInt32LE(compressed.length, 18);
    header.writeUInt32LE(bytes.length, 22);
    header.writeUInt16LE(filename.length, 26);
    local.push(header, filename, compressed);
    const record = Buffer.alloc(46);
    record.writeUInt32LE(0x02014b50, 0);
    record.writeUInt16LE(0x0314, 4);
    header.copy(record, 6, 4, 28);
    record.writeUInt32LE((mode * 65536) >>> 0, 38);
    record.writeUInt32LE(offset, 42);
    central.push(record, filename);
    offset += header.length + filename.length + compressed.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, directory, end]);
}

function bundle(plat) {
  return [file("chromix/", "", 0o40755), file(binary.ASSETS[plat].launcher),
    file(relative(".", binary.binaryPath(plat, ".")).split("\\").join("/"), "chrome fixture"),
    file("chromix/helper", "helper", 0o104755), file("chromix/resources.pak", "resources")];
}

function mockRelease(t, plat, bytes, failure = "") {
  const urls = [];
  t.mock.method(globalThis, "fetch", async (url) => {
    urls.push(url);
    if (url.endsWith("/SHA256SUMS")) {
      const hash = failure === "checksum" ? "0".repeat(64) : createHash("sha256").update(bytes).digest("hex");
      return new Response(`${hash.toUpperCase()} *${binary.ASSETS[plat].asset}\n`, { status: failure === "manifest" ? 404 : 200 });
    }
    assert.equal(url, `${host}/${binary.ASSETS[plat].asset}`);
    if (failure === "http") return new Response("missing", { status: 404 });
    if (failure === "network") throw new Error("network down");
    if (failure === "stream") return { ok: true, body: Readable.from((async function* () {
      yield bytes.subarray(0, 10);
      throw new Error("stream interrupted");
    })()) };
    return new Response(bytes);
  });
  return urls;
}

function mockPlatform(t, platform, arch) {
  const descriptors = Object.fromEntries(["platform", "arch"].map((key) => [key, Object.getOwnPropertyDescriptor(process, key)]));
  Object.defineProperty(process, "platform", { value: platform, configurable: true });
  Object.defineProperty(process, "arch", { value: arch, configurable: true });
  t.after(() => Object.defineProperties(process, descriptors));
  for (const [key, value] of [["CHROMIX_DOWNLOAD_HOST", host], ["CLOAKBROWSER_BINARY_PATH", ""]]) {
    const original = process.env[key];
    process.env[key] = value;
    t.after(() => {
      if (original === undefined) delete process.env[key];
      else process.env[key] = original;
    });
  }
}

for (const [platform, arch, plat] of [["linux", "x64", "linux-x64"], ["linux", "arm64", "linux-arm64"],
  ["win32", "x64", "win-x64"], ["darwin", "x64", "mac-x64"], ["darwin", "arm64", "mac-arm64"]]) {
  test(`ZIP download, public API and cache: ${plat}`, async (t) => {
    mockPlatform(t, platform, arch);
    const urls = mockRelease(t, plat, zipFixture(bundle(plat)));
    assert.equal(binary.resolvePlatform(), plat);
    assert.equal(api.binaryInfo(options).installed, false);
    const root = join(cache, tag, plat), chrome = binary.binaryPath(plat, root);
    assert.equal(await api.ensureBinary(options), chrome);
    assert.equal(readFileSync(chrome, "utf8"), "chrome fixture");
    assert.equal(api.binaryInfo(options).path, chrome);
    assert.equal(api.binaryInfo(options).installed, true);
    assert.equal(await binary.ensureNative(plat, host, tag), join(root, binary.ASSETS[plat].launcher));
    assert.equal(await api.ensureBinary(options), chrome);
    assert.deepEqual(urls, [`${host}/${binary.ASSETS[plat].asset}`, `${host}/SHA256SUMS`]);
    assert.deepEqual(readdirSync(join(cache, tag)), [plat]);
    assert.equal(existsSync(join(root, binary.ASSETS[plat].asset)), false);
    if (plat.startsWith("mac-")) assert.ok(chrome.endsWith(join("Chromium.app", "Contents", "MacOS", "Chromium")));
    if (hostPlatform !== "win32" && platform !== "win32") {
      for (const path of [chrome, join(root, binary.ASSETS[plat].launcher), join(root, "chromix/helper")])
        assert.equal(statSync(path).mode & 0o7777, 0o755);
      assert.equal(statSync(join(root, "chromix/resources.pak")).mode & 0o777, 0o644);
    }
  });
}

test("ZIP preserves framework links, chains and internal parent-relative targets", { skip: process.platform === "win32" }, async (t) => {
  const plat = "mac-arm64", root = join(cache, tag, plat);
  const entries = [...bundle(plat), file("chromix/Framework/Versions/A/Library", "library", 0o100755),
    link("chromix/Framework/Library", "Versions/Current/Library"),
    link("chromix/Framework/Versions/Current", "A"), link("chromix/Framework/chrome", "../Chromium.app/Contents/MacOS/Chromium")];
  mockRelease(t, plat, zipFixture(entries));
  await binary.ensureNative(plat, host, tag);
  const library = join(root, "chromix/Framework/Library");
  assert.ok(lstatSync(library).isSymbolicLink());
  assert.equal(readlinkSync(library), "Versions/Current/Library");
  assert.equal(readFileSync(library, "utf8"), "library");
  assert.equal(readFileSync(join(root, "chromix/Framework/chrome"), "utf8"), "chrome fixture");
});

for (const failure of ["http", "network", "stream", "checksum", "corrupt", "missing-launcher", "missing-binary", "directory-binary"]) {
  test(`ZIP ${failure} cleans staging, keeps old cache and permits retry`, async (t) => {
    const plat = "linux-x64", root = join(cache, tag, plat), marker = join(root, "old-cache");
    mkdirSync(root, { recursive: true });
    writeFileSync(marker, "keep");
    let entries = bundle(plat);
    if (failure === "missing-launcher") entries = entries.filter((entry) => entry.name !== binary.ASSETS[plat].launcher);
    if (failure === "missing-binary") entries = entries.filter((entry) => entry.name !== "chromix/chrome");
    if (failure === "directory-binary") entries = [...entries.filter((entry) => entry.name !== "chromix/chrome"), file("chromix/chrome/", "", 0o40755)];
    mockRelease(t, plat, failure === "corrupt" ? Buffer.from("not a ZIP") : zipFixture(entries), failure);
    await assert.rejects(binary.ensureNative(plat, host, tag));
    assert.equal(readFileSync(marker, "utf8"), "keep");
    assert.deepEqual(readdirSync(join(cache, tag)), [plat]);
    mockRelease(t, plat, zipFixture(bundle(plat)));
    await binary.ensureNative(plat, host, tag);
    assert.equal(existsSync(marker), false);
    assert.equal(binary.bundleComplete(plat, root), true);
  });
}

for (const missing of ["launcher", "binary", "directory", "external-link"]) {
  test(`public API rejects incomplete cache: ${missing}`, { skip: missing === "external-link" && process.platform === "win32" }, async (t) => {
    mockPlatform(t, "linux", "x64");
    const plat = "linux-x64", root = join(cache, tag, plat);
    const launcher = join(root, binary.ASSETS[plat].launcher), chrome = binary.binaryPath(plat, root);
    mkdirSync(dirname(chrome), { recursive: true });
    if (missing !== "launcher") writeFileSync(launcher, "old");
    if (missing === "directory") mkdirSync(chrome);
    else if (missing === "external-link") {
      writeFileSync(join(cache, "outside"), "outside");
      symlinkSync(join(cache, "outside"), chrome);
    } else if (missing !== "binary") writeFileSync(chrome, "old");
    assert.equal(api.binaryInfo(options).installed, false);
    assert.equal(api.binaryInfo(options).path, null);
    const urls = mockRelease(t, plat, zipFixture(bundle(plat)));
    assert.equal(await api.ensureBinary(options), chrome);
    assert.equal(urls.length, 2);
  });
}

const unsafeEntries = {
  traversal: [file("chromix/../../outside", "changed")],
  absolute: [file("/chromix/outside")],
  backslash: [file("chromix/..\\outside")],
  drive: [file("C:/outside")],
  ads: [file("chromix/chrome:stream")],
  reserved: [file("chromix/NUL")],
  trailing: [file("chromix/chrome.")],
  duplicate: [file("chromix/chrome")],
  caseAlias: [file("chromix/CHROME")],
  special: [file("chromix/device", "", 0o020644)],
  symlinkWrite: [link("chromix/link", "../.."), file("chromix/link/outside", "changed")],
  symlinkCaseWrite: [link("chromix/Link", "../.."), file("chromix/link/outside", "changed")],
  symlinkEscape: [link("chromix/link", "../../../outside")],
  symlinkAbsolute: [link("chromix/link", "/outside")],
  symlinkDrive: [link("chromix/link", "C:\\outside")],
  symlinkDangling: [link("chromix/link", "absent")],
  symlinkCycle: [link("chromix/a", "b"), link("chromix/b", "a")],
  symlinkChain: [link("chromix/a", "b"), link("chromix/b", "../../../outside")],
  symlinkRoot: [link("chromix", "../../outside")],
};
for (const [name, entries] of Object.entries(unsafeEntries)) {
  test(`ZIP rejects ${name} without escaping or publishing cache`, async (t) => {
    const plat = "linux-x64";
    writeFileSync(join(cache, "outside"), "untouched");
    mockRelease(t, plat, zipFixture(name === "symlinkRoot" ? entries : [...bundle(plat), ...entries]));
    await assert.rejects(binary.ensureNative(plat, host, tag));
    assert.equal(readFileSync(join(cache, "outside"), "utf8"), "untouched");
    assert.deepEqual(readdirSync(join(cache, tag)), []);
  });
}

test("missing SHA256SUMS retains optional-manifest behavior", async (t) => {
  mockRelease(t, "linux-x64", zipFixture(bundle("linux-x64")), "manifest");
  await binary.ensureNative("linux-x64", host, tag);
  assert.equal(binary.bundleComplete("linux-x64", join(cache, tag, "linux-x64")), true);
});
