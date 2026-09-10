// Synthetic Windows display templates; the SDK passes geometry explicitly.
// Explicit caller settings override defaults and still require layout validation.
// The 85px UI strip is a template value, not a measured native window frame.

// Entries match GetSeededScreen's templates, not its C++ selection algorithm.
export const SCREEN_POOL = [
  [1920, 1080, 1.0, 0.42],
  [1366, 768, 1.0, 0.14],
  [2560, 1440, 1.0, 0.12],
  [1536, 864, 1.25, 0.11],
  [1440, 900, 1.0, 0.05],
  [1680, 1050, 1.0, 0.05],
  [1280, 720, 1.0, 0.03],
  [1600, 900, 1.0, 0.03],
  [1920, 1200, 1.0, 0.02],
  [2560, 1440, 1.5, 0.02],
  [1280, 800, 1.0, 0.01],
];

// Synthetic taskbar templates; these weights are not measured population data.
export const TASKBAR_POOL = [[48, 0.70], [40, 0.30]];

export const CHROME_UI_STRIP = 85;

function weightedPick(entries, rand) {
  const total = entries.reduce((s, e) => s + e[e.length - 1], 0);
  let roll = rand() * total;
  for (const e of entries) {
    roll -= e[e.length - 1];
    if (roll <= 0) return e;
  }
  return entries[entries.length - 1];
}

// One weighted pick from the pool; returns the full derived geometry.
export function pickPersonaScreen(rand = Math.random) {
  const [w, h, dpr] = weightedPick(SCREEN_POOL, rand);
  const [taskbar] = weightedPick(TASKBAR_POOL, rand);
  return { width: w, height: h, dpr, taskbar,
           availHeight: h - taskbar, innerHeight: h - taskbar - CHROME_UI_STRIP };
}

function fmtDpr(dpr) {
  return String(Math.round(dpr * 1000) / 1000);
}

function seededRandom(seed) {
  let state = (seed ^ 0x7363726e) >>> 0;
  return () => {
    state = (state + 0x6D2B79F5) >>> 0;
    let value = Math.imul(state ^ (state >>> 15), 1 | state);
    value ^= value + Math.imul(value ^ (value >>> 7), 61 | value);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

// Complete the screen persona in `args`; returns { args, switches, geometry }.
// Numeric fingerprint seeds use the same generator as the Python SDK.
// Explicit geometry wins; completed args are idempotent.
export function ensurePersonaGeometry(args, rand) {
  const existing = new Map();
  for (const a of args || []) {
    const eq = a.indexOf("=");
    if (eq > 0) existing.set(a.slice(2, eq), a.slice(eq + 1));
  }
  for (const dim of ["width", "height"]) {
    const alias = `fingerprint-screen-${dim}`, key = `uxr-screen-${dim}`;
    if (!existing.has(key) && existing.has(alias)) existing.set(key, existing.get(alias));
  }
  if (!rand) {
    const seed = Number(existing.get("fingerprint"));
    rand = Number.isInteger(seed) && seed > 0 && seed <= 0xFFFFFFFF
      ? seededRandom(seed) : Math.random;
  }
  const numberAt = (key, zero = false) => {
    if (!existing.has(key) || existing.get(key) === "") return null;
    const value = Number(existing.get(key));
    return Number.isFinite(value) && (zero ? value >= 0 : value > 0) ? value : null;
  };
  const pick = pickPersonaScreen(rand);
  const w = numberAt("uxr-screen-width") ?? pick.width;
  const h = numberAt("uxr-screen-height") ?? pick.height;
  const dpr = numberAt("uxr-device-pixel-ratio") ?? pick.dpr;
  const available = numberAt("uxr-screen-avail-height");
  const tb = numberAt("uxr-taskbar-height", true) ?? (available === null ? pick.taskbar : h - available);
  const windowSize = (existing.get("window-size") || "").split(",").map(Number);
  const validSize = windowSize.length === 2 && windowSize.every((v) => Number.isFinite(v) && v > 0);
  const outerWidth = numberAt("uxr-outer-width") ?? (validSize ? windowSize[0] : w);
  const outerHeight = numberAt("uxr-outer-height") ?? (validSize ? windowSize[1] : h - tb);
  const switches = [];
  const put = (key, val) => { if (!existing.has(key)) switches.push(`--${key}=${val}`); };
  put("uxr-screen-width", w);
  put("uxr-screen-height", h);
  put("uxr-device-pixel-ratio", fmtDpr(dpr));
  put("uxr-taskbar-height", tb);
  put("uxr-outer-width", outerWidth);
  put("uxr-outer-height", outerHeight);
  const geometry = { width: w, height: h, dpr, taskbar: tb,
                     availHeight: h - tb, outerWidth, outerHeight,
                     innerHeight: outerHeight - CHROME_UI_STRIP };
  return { args: [...switches, ...(args || [])], switches, geometry };
}
