// CloakBrowser-compatible API behavior tests for the chromix Node SDK.
// Run:  node --test sdk/node/test/*.test.*
import { test } from "node:test";
import assert from "node:assert/strict";
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
  const ctx = buildContextOptions({ headless: true });
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
