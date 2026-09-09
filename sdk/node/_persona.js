// Persona screen geometry: one coherent pick drives every surface.
//
// The engine (patch 0018/0012) falls back to its own seed-pooled screen when
// no explicit --uxr-screen-* switches are present. A fixed SDK viewport on top
// of a randomly pooled screen is an instant tell (innerWidth > screen.width),
// so the SDK picks the geometry itself and passes every piece explicitly:
//
//   screen.width/height = pick (w, h)           --uxr-screen-width/height
//   screen.availHeight  = h - taskbar           --uxr-taskbar-height
//   window.outerWidth   = w                     --uxr-outer-width
//   window.outerHeight  = h - taskbar           --uxr-outer-height
//   window.innerWidth   = w          (headless: CDP viewport)
//   window.innerHeight  = h - taskbar - 85      (headless: CDP viewport)
//   devicePixelRatio    = pick (dpr, joint)     --uxr-device-pixel-ratio
//
// 85px is the Win11 Chrome UI strip (title + tabs + omnibox), same constant
// the engine uses for its outerHeight default in patch 0012.

// Mirrors the engine pool in patch 0002 (base/uxr_config.cc GetSeededScreen):
// joint (logical size, dpr) picks so width * dpr lands on a real panel.
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

// Win11 taskbar 48px (~70%) / Win10 40px (~30%), mirrors GetSeededTaskbarHeight.
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

// Complete the screen persona in `args`; returns { args, switches, geometry }.
// Any geometry piece the caller already set via --uxr-* wins; missing pieces
// come from a single pooled pick so screen/avail/outer/inner always agree.
// Idempotent: running on already-completed args adds nothing.
export function ensurePersonaGeometry(args, rand = Math.random) {
  const existing = new Map();
  for (const a of args || []) {
    if (a.startsWith("--uxr-")) {
      const eq = a.indexOf("=");
      if (eq > 0) existing.set(a.slice(2, eq), a.slice(eq + 1));
    }
  }
  const intAt = (k) => {
    const v = parseInt(existing.get(k) || "", 10);
    return Number.isFinite(v) && v > 0 ? v : 0;
  };
  const floatAt = (k) => {
    const v = parseFloat(existing.get(k) || "");
    return Number.isFinite(v) && v > 0 ? v : 0;
  };
  let w = intAt("uxr-screen-width"), h = intAt("uxr-screen-height");
  let dpr = floatAt("uxr-device-pixel-ratio"), tb = intAt("uxr-taskbar-height");
  if (!(w && h && dpr && tb)) {
    const pick = pickPersonaScreen(rand);
    w = w || pick.width; h = h || pick.height;
    dpr = dpr || pick.dpr; tb = tb || pick.taskbar;
  }
  const switches = [];
  const put = (key, val) => { if (!existing.has(key)) switches.push(`--${key}=${val}`); };
  put("uxr-screen-width", w);
  put("uxr-screen-height", h);
  put("uxr-device-pixel-ratio", fmtDpr(dpr));
  put("uxr-taskbar-height", tb);
  put("uxr-outer-width", w);
  put("uxr-outer-height", h - tb);
  const geometry = { width: w, height: h, dpr, taskbar: tb,
                     availHeight: h - tb, innerHeight: h - tb - CHROME_UI_STRIP };
  // Persona switches go first so explicit user args keep priority in
  // buildArgs' key dedupe.
  return { args: [...switches, ...(args || [])], switches, geometry };
}
