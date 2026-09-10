// CloakBrowser-compatible API behavior tests for the chromix Node SDK.
// Run:  node --test sdk/node/test/*.test.*
import { after, test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync, rmSync, existsSync, statSync } from "node:fs";
import fsPromises from "node:fs/promises";
import { syncBuiltinESMExports } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { spawn } from "node:child_process";
import {
  buildArgs, buildContextOptions, getDefaultStealthArgs, binaryInfo,
  resolveHumanConfig,
} from "../index.js";

test("buildArgs priority: stealth < user < dedicated; no duplicate keys", () => {
  const args = buildArgs({
    stealthArgs: true,
    extraArgs: ["--fingerprint=42", "--lang=fr-FR", "--window-size=800,600"],
    timezone: "Europe/Berlin",
    locale: "de-DE",
    headless: true,
  });
  assert.ok(args.includes("--fingerprint=42"), "user seed override");
  assert.ok(args.includes("--lang=de-DE") && !args.includes("--lang=fr-FR"), "dedicated locale wins");
  assert.ok(args.includes("--fingerprint-timezone=Europe/Berlin"), "timezone flag");
  assert.ok(args.includes("--window-size=800,600"), "geometry passthrough");
  const keys = args.map((a) => a.split("=", 1)[0]);
  assert.equal(new Set(keys).size, keys.length, "no duplicate keys");
});

test("buildArgs maximize suppressed by geometry / added when free", () => {
  assert.ok(!buildArgs({ extraArgs: ["--window-size=800,600"], startMaximized: true })
    .includes("--start-maximized"), "suppressed by geometry");
  assert.ok(buildArgs({ startMaximized: true }).includes("--start-maximized"), "added when free");
});

test("default stealth args carry one seed and preserve the sandbox", () => {
  const sa = getDefaultStealthArgs();
  assert.equal(sa.filter((a) => a.startsWith("--fingerprint=")).length, 1);
  assert.ok(!sa.includes("--no-sandbox"));
});

test("context options: default viewport, explicit null, CDP emulation stripped", () => {
  const ctx = buildContextOptions({ headless: true, args: [
    "--uxr-screen-width=1920", "--uxr-screen-height=1080", "--uxr-taskbar-height=48",
  ] });
  assert.equal(ctx.viewport?.width, 1920);
  assert.equal(ctx.viewport?.height, 947);
  assert.equal(buildContextOptions({ headless: true, viewport: null }).viewport, null);
  const ctx3 = buildContextOptions({ headless: true, contextOptions: { locale: "de-DE", foo: 1 } });
  assert.equal(ctx3.locale, undefined, "contextOptions.locale stripped");
  assert.equal(ctx3.foo, 1, "other contextOptions forwarded");
  assert.equal(buildContextOptions({ userAgent: "x" }).userAgent, "x");
});

test("binaryInfo shape", () => {
  const info = binaryInfo();
  assert.equal(info.tier, "open-source");
  assert.ok(typeof info.version === "string" && info.version.length > 0);
});

test("human config presets and overrides", () => {
  const cfg = resolveHumanConfig("careful", { mistype: 0.5 });
  assert.equal(cfg.typingDelay, 130, "careful preset slower");
  assert.equal(cfg.mistype, 0.5, "override applied");
});

for (const [platform, persona] of [["linux", "linux"], ["win32", "windows"], ["darwin", "macos"], ["freebsd", null]]) {
  test(`native platform defaults and explicit override: ${platform}`, (t) => {
    const original = Object.getOwnPropertyDescriptor(process, "platform");
    Object.defineProperty(process, "platform", { value: platform, configurable: true });
    t.after(() => Object.defineProperty(process, "platform", original));
    assert.deepEqual(getDefaultStealthArgs().filter((a) => a.startsWith("--fingerprint-platform=")),
      persona ? [`--fingerprint-platform=${persona}`] : []);
    assert.deepEqual(buildArgs({ extraArgs: ["--fingerprint-platform=macos"] })
      .filter((a) => a.startsWith("--fingerprint-platform=")), ["--fingerprint-platform=macos"]);
    assert.ok(!buildArgs({ stealthArgs: false }).some((a) => a.startsWith("--fingerprint")));
  });
}

const SEED_FILE = ".chromix-fingerprint-seed";
const fixture = mkdtempSync(join(tmpdir(), "chromix-api-"));
const playwright = join(new URL("../node_modules", import.meta.url).pathname, "playwright-core");
mkdirSync(playwright, { recursive: true });
writeFileSync(join(playwright, "package.json"), JSON.stringify({ type: "module", exports: "./index.js" }));
writeFileSync(join(playwright, "index.js"), `
export const calls = [];
export const control = { fail: false };
function context(options) { return { options, pages: () => [], close: async () => {} }; }
export const chromium = {
  launchPersistentContext: async (dir, options) => {
    calls.push({ dir, options });
    if (control.fail) throw new Error("fixture launch failure");
    return context(options);
  },
  launch: async (options) => {
    calls.push({ options });
    return { ...context(options), newContext: async (ctx) => ({ ...context(options), contextOptions: ctx }),
             newPage: async (ctx) => ({ ...context(options), contextOptions: ctx }) };
  },
};
`);
const fixtureUrl = new URL("../index.js", import.meta.url).href;
const fixtureApi = await import(new URL("../index.js", import.meta.url));
const { calls, control } = await import(pathToFileURL(join(playwright, "index.js")).href);
after(() => rmSync(playwright, { recursive: true, force: true }));

after(() => rmSync(fixture, { recursive: true, force: true }));

function offline(t) {
  const root = mkdtempSync(join(fixture, "profile-test-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [key, value] of [["CLOAKBROWSER_BINARY_PATH", new URL("../index.js", import.meta.url).pathname], ["CLOAKBROWSER_WIDEVINE", "0"]]) {
    const original = process.env[key];
    process.env[key] = value;
    t.after(() => {
      if (original === undefined) delete process.env[key];
      else process.env[key] = original;
    });
  }
  t.mock.method(globalThis, "fetch", async () => { throw new Error("Network attempted"); });
  calls.length = 0;
  control.fail = false;
  return root;
}

function fingerprint(args) {
  const seeds = args.filter((a) => a.startsWith("--fingerprint=")).map((a) => a.slice("--fingerprint=".length));
  assert.equal(seeds.length, 1);
  return seeds[0];
}

async function persistent(userDataDir, options = {}) {
  const ctx = await fixtureApi.launchPersistentContext({ userDataDir, ...options });
  await ctx.close();
  return ctx.options.args;
}

test("persistent profile reuses one seed without rewriting or mutating options", async (t) => {
  const root = offline(t), profile = join(root, "nested", "profile");
  const options = { args: ["--fingerprint-platform=macos", "--window-size=800,600"], timezone: "UTC", locale: "en-US" };
  const snapshot = structuredClone(options);
  const first = await persistent(profile, options);
  const path = join(profile, SEED_FILE), before = statSync(path);
  const second = await persistent(profile);
  assert.equal(fingerprint(first), fingerprint(second));
  assert.match(readFileSync(path, "utf8"), /^[1-9][0-9]*\n$/);
  assert.equal(readFileSync(path, "utf8"), `${fingerprint(first)}\n`);
  assert.deepEqual([statSync(path).ino, statSync(path).mtimeMs], [before.ino, before.mtimeMs]);
  assert.ok(first.includes("--fingerprint-platform=macos"));
  assert.ok(first.includes("--fingerprint-timezone=UTC") && first.includes("--lang=en-US"));
  assert.equal(first.length, new Set(first.map((a) => a.split("=", 1)[0])).size);
  assert.deepEqual(options, snapshot);
  assert.notEqual(fingerprint(await persistent(join(root, "other"))), fingerprint(first));
  assert.deepEqual(readdirSync(profile), [SEED_FILE]);
  if (process.platform !== "win32") assert.equal(statSync(path).mode & 0o777, 0o600);
});

for (const value of ["42", "off", "0", ""]) {
  test(`explicit fingerprint skips all profile seed I/O: ${value}`, async (t) => {
    const root = offline(t), profile = join(root, "profile");
    const options = { args: ["--fingerprint=13", `--fingerprint=${value}`] };
    assert.equal(fingerprint(await persistent(profile, options)), value);
    assert.equal(existsSync(profile), false);
    mkdirSync(profile);
    writeFileSync(join(profile, SEED_FILE), "corrupt");
    assert.equal(fingerprint(await persistent(profile, options)), value);
    assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), "corrupt");
    assert.deepEqual(options.args, ["--fingerprint=13", `--fingerprint=${value}`]);
  });
}

test("stealthArgs false skips seed I/O and preserves explicit flags", async (t) => {
  const root = offline(t), profile = join(root, "profile");
  assert.ok(!(await persistent(profile, { stealthArgs: false })).some((a) => a.startsWith("--fingerprint")));
  assert.equal(existsSync(profile), false);
  mkdirSync(profile);
  writeFileSync(join(profile, SEED_FILE), "corrupt");
  await persistent(profile, { stealthArgs: false });
  assert.equal(fingerprint(await persistent(profile, { stealthArgs: false, args: ["--fingerprint=42"] })), "42");
  assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), "corrupt");
});

test("explicit seed and off never replace the saved identity", async (t) => {
  const profile = offline(t), original = fingerprint(await persistent(profile));
  assert.equal(fingerprint(await persistent(profile, { args: ["--fingerprint=42"] })), "42");
  assert.equal(fingerprint(await persistent(profile, { args: ["--fingerprint=off"] })), "off");
  assert.equal(fingerprint(await persistent(profile)), original);
  assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), `${original}\n`);
});

for (const [index, data] of ["", "garbage", "0\n", "-1\n", "4294967296\n", "1.0\n", "01\n", " 1\n", "1", "1\r\n", "1\n2\n", Buffer.from([255, 10])].entries()) {
  test(`invalid seed fails without identity rotation: ${index}`, async (t) => {
    const profile = offline(t), path = join(profile, SEED_FILE);
    writeFileSync(path, data);
    await assert.rejects(persistent(profile), /Invalid Chromix profile seed file/);
    assert.deepEqual(readFileSync(path), Buffer.from(data));
    assert.deepEqual(readdirSync(profile), [SEED_FILE]);
    assert.equal(calls.length, 0);
  });
}

for (const seed of [1, 4294967295]) {
  test(`pre-existing seed boundary: ${seed}`, async (t) => {
    const profile = offline(t);
    writeFileSync(join(profile, SEED_FILE), `${seed}\n`);
    assert.equal(fingerprint(await persistent(profile)), String(seed));
  });
}

for (const profile of [undefined, null, ""]) {
  test(`persistent requires a profile: ${profile}`, async (t) => {
    offline(t);
    await assert.rejects(persistent(profile), /requires options.userDataDir/);
    assert.equal(calls.length, 0);
  });
}

test("launch failure retains published identity for retry", async (t) => {
  const profile = offline(t);
  control.fail = true;
  await assert.rejects(persistent(profile), /fixture launch failure/);
  const seed = readFileSync(join(profile, SEED_FILE), "utf8").trim();
  control.fail = false;
  assert.equal(fingerprint(await persistent(profile)), seed);
});

for (const stage of ["readFile", "open", "link"]) {
  test(`seed ${stage} failure propagates without random fallback`, async (t) => {
    const profile = offline(t), original = fsPromises[stage];
    fsPromises[stage] = async () => { throw Object.assign(new Error("fixture denied"), { code: "EACCES" }); };
    syncBuiltinESMExports();
    try {
      await assert.rejects(persistent(profile), { code: "EACCES" });
    } finally {
      fsPromises[stage] = original;
      syncBuiltinESMExports();
    }
    assert.deepEqual(readdirSync(profile), []);
    assert.equal(calls.length, 0);
  });
}

test("concurrent async first launches publish only a complete seed", async (t) => {
  const profile = offline(t), original = fsPromises.link;
  let ready = 0, release;
  const gate = new Promise((resolve) => { release = resolve; });
  fsPromises.link = async (source, target) => {
    assert.match(await fsPromises.readFile(source, "utf8"), /^[1-9][0-9]*\n$/);
    if (++ready === 12) release();
    await gate;
    return original(source, target);
  };
  syncBuiltinESMExports();
  try {
    const results = await Promise.all(Array.from({ length: 12 }, () => persistent(profile)));
    assert.equal(new Set(results.map(fingerprint)).size, 1);
    assert.equal(new Set(results.map((args) => JSON.stringify(geometryArgs(args)))).size, 1);
    assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), `${fingerprint(results[0])}\n`);
  } finally {
    fsPromises.link = original;
    syncBuiltinESMExports();
  }
  assert.deepEqual(readdirSync(profile), [SEED_FILE]);
});

test("orphan temporary file does not block seed initialization", async (t) => {
  const profile = offline(t), orphan = join(profile, `${SEED_FILE}.interrupted`);
  writeFileSync(orphan, "12");
  const seed = fingerprint(await persistent(profile));
  assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), `${seed}\n`);
  assert.equal(readFileSync(orphan, "utf8"), "12");
});

for (const key of ["launchOptions", "contextOptions"]) {
  test(`${key}.args explicit seed/off wins and never creates metadata`, async (t) => {
    const root = offline(t), profile = join(root, "profile");
    for (const value of ["42", "off"]) {
      const options = { args: ["--fingerprint=13"], [key]: { args: [`--fingerprint=${value}`] } };
      const snapshot = structuredClone(options);
      assert.equal(fingerprint(await persistent(profile, options)), value);
      assert.equal(existsSync(profile), false);
      assert.deepEqual(options, snapshot);
    }
  });
  test(`${key}.args without a seed uses the persistent identity`, async (t) => {
    const profile = offline(t), options = { args: ["--fingerprint=off"], [key]: { args: ["--custom-flag"] } };
    const snapshot = structuredClone(options);
    const first = await persistent(profile, options), second = await persistent(profile, options);
    assert.equal(fingerprint(first), fingerprint(second));
    assert.ok(first.includes("--custom-flag"));
    assert.notEqual(fingerprint(first), "off");
    assert.deepEqual(options, snapshot);
    assert.deepEqual(await persistent(profile, { ...options, stealthArgs: false }), ["--custom-flag"]);
  });
}

test("nonpersistent launch family remains random and creates no seed file", async (t) => {
  const profile = offline(t), seeds = [];
  for (const launch of [fixtureApi.launch, fixtureApi.launchContext]) {
    for (let i = 0; i < 2; i++) {
      const result = await launch({ userDataDir: profile });
      seeds.push(fingerprint(result.options.args));
      await result.close();
    }
  }
  assert.equal(new Set(seeds).size, 4);
  assert.deepEqual(readdirSync(profile), []);
});

test("separate Node processes converge on the same first seed", { timeout: 30000 }, async (t) => {
  const profile = offline(t), children = [];
  t.after(() => { for (const child of children) child.kill(); });
  let ready = 0;
  const source = `
    import fs from "node:fs/promises";
    import { syncBuiltinESMExports } from "node:module";
    const link = fs.link;
    fs.link = async (...args) => {
      process.send("ready");
      await new Promise(resolve => process.once("message", resolve));
      return link(...args);
    };
    syncBuiltinESMExports();
    globalThis.fetch = async () => { throw new Error("Network attempted"); };
    const api = await import(${JSON.stringify(fixtureUrl)});
    const ctx = await api.launchPersistentContext({ userDataDir: ${JSON.stringify(profile)} });
    process.stdout.write(ctx.options.args.find(a => a.startsWith("--fingerprint=")));
    await ctx.close();
    process.disconnect();
  `;
  const jobs = Array.from({ length: 8 }, () => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["--input-type=module", "-e", source], { stdio: ["ignore", "pipe", "pipe", "ipc"] });
    children.push(child);
    let output = "", errors = "";
    child.stdout.on("data", (data) => { output += data; });
    child.stderr.on("data", (data) => { errors += data; });
    child.on("error", reject);
    child.on("message", () => {
      if (++ready === 8) for (const peer of children) peer.send("publish");
    });
    child.on("exit", (code) => code === 0 ? resolve(output) : reject(new Error(errors || `child exit ${code}`)));
  }));
  const seeds = await Promise.all(jobs);
  assert.equal(new Set(seeds).size, 1);
  assert.equal(seeds[0], `--fingerprint=${readFileSync(join(profile, SEED_FILE), "utf8").trim()}`);
  assert.deepEqual(readdirSync(profile), [SEED_FILE]);
});

test("font dir parser reproduces the bundled Windows families", async () => {
  const { fontFamiliesInDir, fontDirWhitelistArg } = await import("../_fonts.js");
  const { fileURLToPath } = await import("node:url");
  const fontsDir = fileURLToPath(new URL("../../../assets/fonts", import.meta.url));
  const families = fontFamiliesInDir(fontsDir);
  assert.ok(families.length >= 50, `expected >= 50 families, got ${families.length}`);
  for (const f of ["Arial", "Arial Narrow", "Calibri", "Cambria Math", "Consolas",
                   "MS Gothic", "MS PGothic", "Segoe UI", "Segoe UI Light",
                   "Tahoma", "Times New Roman", "Verdana", "Wingdings 3",
                   "ＭＳ ゴシック"]) {
    assert.ok(families.includes(f), `missing family: ${f}`);
  }
  const arg = fontDirWhitelistArg(fontsDir);
  assert.ok(arg.startsWith("--uxr-font-whitelist="));
  assert.ok(arg.includes("Segoe UI"));
});

test("persona geometry is complete, coherent and idempotent", async () => {
  const { ensurePersonaGeometry, SCREEN_POOL } = await import("../_persona.js");
  const r = ensurePersonaGeometry(undefined, () => 0.5);
  const keys = new Set(r.switches.map((a) => a.split("=", 1)[0]));
  for (const k of ["--uxr-screen-width", "--uxr-screen-height",
                   "--uxr-device-pixel-ratio", "--uxr-taskbar-height",
                   "--uxr-outer-width", "--uxr-outer-height"]) {
    assert.ok(keys.has(k), k);
  }
  const g = r.geometry;
  assert.equal(g.availHeight, g.height - g.taskbar);
  assert.equal(g.innerHeight, g.availHeight - 85);
  assert.ok(g.innerHeight >= 580);
  assert.ok(SCREEN_POOL.some((s) => s[0] === g.width && s[2] === g.dpr));
  const r2 = ensurePersonaGeometry(r.args, () => 0.1);
  assert.deepEqual(r2.args, r.args, "idempotent");
});

test("persona geometry respects explicit values and dpr viewport", async () => {
  const { ensurePersonaGeometry } = await import("../_persona.js");
  const r = ensurePersonaGeometry(
    ["--uxr-screen-width=1366", "--uxr-screen-height=768"], () => 0.5);
  assert.equal(r.geometry.width, 1366);
  assert.equal(r.geometry.height, 768);
  assert.ok(!r.switches.some((a) => a.startsWith("--uxr-screen-width=")));
  // forced pick of 1536x864@1.25 (roll 0.7) -> deviceScaleFactor-ready geometry
  const r2 = ensurePersonaGeometry([], () => 0.7);
  assert.equal(r2.geometry.dpr, 1.25);
  assert.equal(r2.geometry.innerHeight, 731);
  assert.ok(r2.switches.includes("--uxr-device-pixel-ratio=1.25"));
});

test("context viewport comes from the same persona pick per options object", () => {
  const opts = { args: [] };
  const c1 = buildContextOptions(opts);
  const c2 = buildContextOptions(opts);
  assert.deepEqual(c1.viewport, c2.viewport, "one pick per options object");
  assert.ok(c1.viewport.width > 0 && c1.viewport.height >= 580);
  // explicit viewport wins
  const c3 = buildContextOptions({ viewport: null, args: [] });
  assert.equal(c3.viewport, null);
});

function geometryArgs(args) {
  return args.filter((a) => /^--uxr-(screen-|outer-|taskbar-|device-pixel)/.test(a));
}

for (const [seed, width, height, taskbar] of [[1, 1920, 1200, 48], [42, 1680, 1050, 40],
                                           [101, 1600, 900, 40], [4294967295, 1920, 1080, 40]]) {
  test(`seeded geometry cross-SDK vector: ${seed}`, async () => {
    const { ensurePersonaGeometry } = await import("../_persona.js");
    const first = ensurePersonaGeometry([`--fingerprint=${seed}`]);
    const second = ensurePersonaGeometry([`--fingerprint=${seed}`]);
    assert.deepEqual(first, second);
    assert.deepEqual([first.geometry.width, first.geometry.height, first.geometry.taskbar],
                     [width, height, taskbar]);
  });
}

test("persistent geometry follows the saved seed before context construction", async (t) => {
  const profile = offline(t);
  writeFileSync(join(profile, SEED_FILE), "42\n");
  const first = await persistent(profile);
  const firstOptions = calls.at(-1).options;
  const second = await persistent(profile);
  assert.deepEqual(geometryArgs(first), geometryArgs(second));
  assert.deepEqual(firstOptions.viewport, { width: 1680, height: 925 });
  assert.deepEqual(calls.at(-1).options.viewport, firstOptions.viewport);
  assert.equal(readFileSync(join(profile, SEED_FILE), "utf8"), "42\n");
});

test("launch and context options share geometry with DPR at context level", async (t) => {
  offline(t);
  const options = { args: ["--fingerprint=42", "--uxr-device-pixel-ratio=1.25"] };
  const context = buildContextOptions(options);
  const launch = await fixtureApi.buildLaunchOptions(options);
  assert.deepEqual(context.viewport, { width: 1680, height: 925 });
  assert.equal(context.deviceScaleFactor, 1.25);
  assert.equal(context.viewport.deviceScaleFactor, undefined);
  assert.ok(launch.args.includes("--uxr-screen-width=1680"));
  options.args = ["--fingerprint=1"];
  assert.deepEqual(buildContextOptions(options).viewport, { width: 1920, height: 1067 });
  const nested = buildContextOptions({ ...options, contextOptions: { viewport: { width: 800, height: 600 } } });
  assert.deepEqual(nested.viewport, { width: 800, height: 600 });
});

test("browser newPage and newContext inherit geometry but allow explicit overrides", async (t) => {
  offline(t);
  const options = { args: ["--fingerprint=42", "--uxr-device-pixel-ratio=1.25"] };
  const browser = await fixtureApi.launch(options);
  for (const method of ["newPage", "newContext"]) {
    const context = await browser[method]();
    assert.deepEqual(context.contextOptions.viewport, { width: 1680, height: 925 });
    assert.equal(context.contextOptions.deviceScaleFactor, 1.25);
    const native = await browser[method]({ viewport: null });
    assert.equal(native.contextOptions.viewport, null);
    assert.equal(native.contextOptions.deviceScaleFactor, undefined);
  }
  await browser.close();
  const reused = {};
  const first = await fixtureApi.launch(reused), second = await fixtureApi.launch(reused);
  assert.notEqual(fingerprint(first.options.args), fingerprint(second.options.args));
  await first.close(); await second.close();
});

test("disabled stealth and fingerprint off do not inject geometry", async (t) => {
  offline(t);
  for (const options of [{ stealthArgs: false }, { args: ["--fingerprint=off"] }]) {
    assert.deepEqual(geometryArgs((await fixtureApi.buildLaunchOptions(options)).args), []);
  }
});

test("screen aliases, explicit outer size and headed window remain coherent", async (t) => {
  offline(t);
  const options = { headless: false, args: ["--fingerprint=42", "--fingerprint-screen-width=1366",
    "--fingerprint-screen-height=768", "--uxr-taskbar-height=0", "--uxr-outer-width=1000", "--uxr-outer-height=700"] };
  const launch = await fixtureApi.buildLaunchOptions(options);
  assert.ok(!launch.args.some((a) => a.startsWith("--uxr-screen-width=")));
  assert.ok(launch.args.includes("--window-size=1000,700"));
  assert.equal(buildContextOptions(options).viewport, null);
  assert.deepEqual(buildContextOptions({ ...options, headless: true }).viewport, { width: 1000, height: 615 });
  const sized = { headless: false, args: ["--fingerprint=42", "--window-size=800,600"] };
  const sizedLaunch = await fixtureApi.buildLaunchOptions(sized);
  assert.ok(sizedLaunch.args.includes("--uxr-outer-width=800"));
  assert.ok(sizedLaunch.args.includes("--uxr-outer-height=600"));
  assert.ok(!sizedLaunch.args.includes("--start-maximized"));
});

test("font directory reaches launch args and isolated Fontconfig paths", async (t) => {
  const root = offline(t);
  const { linuxFontEnv } = await import("../_fonts.js");
  const fontsDir = new URL("../../../assets/fonts", import.meta.url).pathname;
  const launch = await fixtureApi.buildLaunchOptions({ fontsDir, launchOptions: { env: { FIXTURE: "yes" } } });
  assert.ok(launch.args.some((a) => a.startsWith("--uxr-font-whitelist=") && a.includes("Arial")));
  assert.equal(launch.env.FIXTURE, "yes");
  const override = await fixtureApi.buildLaunchOptions({ fontsDir, args: ["--uxr-font-whitelist=Custom"] });
  assert.deepEqual(override.args.filter((a) => a.startsWith("--uxr-font-whitelist=")), ["--uxr-font-whitelist=Custom"]);
  if (process.platform === "linux") {
    const first = linuxFontEnv("unused", join(root, "one & two")).FONTCONFIG_FILE;
    const before = readFileSync(first, "utf8");
    const second = linuxFontEnv("unused", join(root, "other")).FONTCONFIG_FILE;
    t.after(() => { rmSync(first, { force: true }); rmSync(second, { force: true }); });
    assert.notEqual(first, second);
    assert.match(before, /one &amp; two/);
    assert.equal(readFileSync(first, "utf8"), before);
  }
});

test("published Node package includes its persona import", () => {
  const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.ok(pkg.files.includes("_persona.js"));
});
