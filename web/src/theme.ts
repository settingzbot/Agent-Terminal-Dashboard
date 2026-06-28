// Dashboard design tokens.
//
// TWO full palettes (warm-beige dark + flat-white light) bridged by a
// continuous BRIGHTNESS dial. The page background lerps dark->white across
// the whole range; every OTHER token (text, surfaces, borders, semantic
// colors) crossfades late and fast -- smoothstep over the FLIP window -- so
// the lower two-thirds of the dial keeps the original "cream text on
// darkening frosted cards" look, and the top of the dial is a true light
// mode: near-black ink on flat-white glass. The Dark/Light toggle buttons
// just snap brightness to 0 / 100.
//
// Continuous-hue accent -- see hslToHex below.
//
// Defaults: brightness 0 (= the original Dark) + amber + dense.

export type Theme = {
  name: string;
  bg0: string; bg1: string; bg2: string; bg3: string;
  // Translucent variants of bg0 / bg1 used wherever a panel should let the
  // constellation background show through. Pair with backdrop-filter blur.
  bg0Glass: string; bg1Glass: string;
  // Higher-alpha variant for the chart's price pane -- keeps candles readable
  // while still hinting at the constellation behind them.
  bg1Chart: string;
  // Glass tint behind dashboard cards. Scales with brightness: transparent at
  // 0, progressively darker frosted-glass at higher brightness so cream text
  // on cards stays readable when the page bg goes cream.
  panelBg: string;
  border: string; borderHi: string;
  text: string; text2: string; text3: string;
  green: string; red: string; amber: string; blue: string;
  // Vibrant "strong trend" variants -- used when an HTF gauge crosses its
  // strong threshold. Same hue family as green/red but saturated, so a
  // strong reading reads as MORE of the trend, not as an amber warning.
  greenStrong: string; redStrong: string;
  // EMA trace colors that can't ride the semantic tokens: EMA9 is the
  // "ink" line (white on dark, espresso on light) and EMA21's cyan has no
  // sibling in the palette. Hex in both palettes -- safe to concat alpha.
  ema9: string; ema21: string;
  fontBody: string; fontDisplay: string; fontMono: string;
  radius: number;
  cardShadow: string;
  gridLine: string;
};

// Standardised blur levels for glass surfaces.
// GLASS_BLUR_LIGHT -- the unified dashboard glass level. Cards, stats cells,
//                    chart card, sidebar, top bar, trades container all use
//                    this with `theme.panelBg` so the constellation tints
//                    through and the cards darken with brightness.
// GLASS_BLUR -- the heavier recipe (16px). Kept for any future surface that
//              needs more body (mobile overlays, modals), not used on the main
//              dashboard widgets.
export const GLASS_BLUR = 'blur(16px)';
export const GLASS_BLUR_LIGHT = 'blur(3px)';

export type DensityKey = 'comfortable' | 'compact' | 'dense';

export function hslToHex(hue: number, sat = 95, light = 50): string {
  const s = sat / 100;
  const l = light / 100;
  const k = (n: number) => (n + hue / 30) % 12;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  const toHex = (x: number) => Math.round(x * 255).toString(16).padStart(2, '0');
  return `#${toHex(f(0))}${toHex(f(8))}${toHex(f(4))}`;
}

export type Density = {
  pad: number; gap: number; rowH: number;
  fsBody: number; fsLabel: number; fsValue: number;
};

// Brightness range. 0 = the original Dark page bg; 100 = warm cream Light.
// Values lerp linearly through the brand's warm-beige axis (no neutral grays
// or pure whites -- those would clash with the serif headlines and ember tint).
export const BRIGHTNESS_MIN = 0;
export const BRIGHTNESS_MAX = 100;
export const DEFAULT_BRIGHTNESS = 0;

// Accent (the app's primary color) is a continuous HSL: a hue dial around the
// full 0–360 wheel, a saturation dial for how vivid it reads, and a fixed
// lightness of 50 (the brightness dial drives the PAGE, not the accent). The
// original dashboard shipped amber — hue 43, saturation 95 (see App.tsx's old
// hard-coded hslToHex(43, 95, 50)).
export const HUE_MIN = 0;
export const HUE_MAX = 360;
export const SATURATION_MIN = 0;
export const SATURATION_MAX = 100;
export const ACCENT_LIGHTNESS = 50;
export const DEFAULT_HUE = 43;
export const DEFAULT_SATURATION = 95;

// Background tint. A subtle hue wash applied to the PAGE background (bg0) only —
// surfaces, cards and text stay neutral, so the page reads as gently colored
// behind a stack of neutral glass. 0 = OFF (neutral bg = the original look, so
// existing users see no change until they opt in); 1–360 picks the wash hue.
// The wash is mixed at a fixed gentle strength and keeps bg0's perceived
// lightness, so the brightness dial still owns dark↔light independently.
export const BG_TINT_MIN = 0;
export const BG_TINT_MAX = 360;
export const DEFAULT_BG_TINT = 0;
// How vivid the injected hue is (BG_TINT_SAT) and how far bg0 is mixed toward it
// (BG_TINT_STRENGTH). Tuned low on purpose: a cast, not a color.
const BG_TINT_SAT = 65;
const BG_TINT_STRENGTH = 0.14;

// The three user-tunable theme dials, persisted as one object. `hue` +
// `saturation` build the accent (via hslToHex at ACCENT_LIGHTNESS); `brightness`
// feeds deriveTheme to lerp the whole page dark->light.
export type ThemeSettings = {
  hue: number;        // 0–360
  saturation: number; // 0–100
  brightness: number; // 0–100
  bgTint: number;     // 0 = off, 1–360 = background hue wash
};

export const DEFAULT_THEME_SETTINGS: ThemeSettings = {
  hue: DEFAULT_HUE,
  saturation: DEFAULT_SATURATION,
  brightness: DEFAULT_BRIGHTNESS,
  bgTint: DEFAULT_BG_TINT,
};

const BG0_DARK = '#0f0e0c';
// Flat white light-mode page bg (was warm cream #f5f0e3 -- Nathan wanted no
// cream tint at all). Every other light surface below is on a neutral gray
// axis to match; only text + semantic data colors keep their hue.
const BG0_LIGHT = '#ffffff';
// Panel-tint maximum at brightness 100. 0.55 was tuned by eye against cream:
// enough to read cream text on cards without losing the constellation behind.
const PANEL_TINT_MAX = 0.55;

function clamp01(t: number): number { return Math.max(0, Math.min(1, t)); }

function lerpHex(a: string, b: string, t: number): string {
  const ah = a.replace('#', '');
  const bh = b.replace('#', '');
  const ar = parseInt(ah.slice(0, 2), 16), ag = parseInt(ah.slice(2, 4), 16), ab = parseInt(ah.slice(4, 6), 16);
  const br = parseInt(bh.slice(0, 2), 16), bg = parseInt(bh.slice(2, 4), 16), bb = parseInt(bh.slice(4, 6), 16);
  const lerp = (x: number, y: number) => Math.round(x + (y - x) * t);
  const hex = (n: number) => n.toString(16).padStart(2, '0');
  return `#${hex(lerp(ar, br))}${hex(lerp(ag, bg))}${hex(lerp(ab, bb))}`;
}

// Shared across both palettes -- type, radius.
const FONTS = {
  fontBody: '"JetBrains Mono", "Menlo", monospace',
  fontDisplay: '"JetBrains Mono", "Menlo", monospace',
  fontMono: '"JetBrains Mono", "Menlo", monospace',
  radius: 4,
};

// Token pairs MUST match formats positionally: hex-with-hex, rgba-with-rgba.
// Several call sites concat alpha onto hex tokens (`${theme.green}22`), so a
// token that is hex in one palette has to stay hex in the other.
const DARK_TOKENS = {
  bg1: '#171614', bg2: '#1f1d1a', bg3: '#28251f',
  bg1Glass: 'rgba(23,22,20,0.7)', bg1Chart: 'rgba(23,22,20,0.04)',
  border: 'rgba(232,220,200,0.08)', borderHi: 'rgba(232,220,200,0.16)',
  text: '#f3eade', text2: 'rgba(243,234,222,0.55)', text3: 'rgba(243,234,222,0.3)',
  green: '#86d9a5', red: '#e88b8b', amber: '#e3b363', blue: '#8ab4e8',
  greenStrong: '#4ee68d', redStrong: '#f56b6b',
  ema9: '#ffffff', ema21: '#22d3ee',
  cardShadow: '0 1px 0 rgba(232,220,200,0.03) inset',
  gridLine: 'rgba(232,220,200,0.04)',
};

// Flat-white light palette. Neutral surfaces (white page, light-gray card
// ramp) -- no warm-cream tint (Nathan's call 2026-06-13, superseding the
// earlier "warm-cream inversion" handoff intent). Borders/shadows/grid are
// neutral gray too. Text stays near-black ink and the semantic
// green/red/amber/blue reuse the HTF grade report's hand-tuned light values
// (RX_LIGHT in HtfGrade/shared.tsx) so the two surfaces agree on what "green
// on light" looks like.
const LIGHT_TOKENS = {
  bg1: '#f7f7f7', bg2: '#f0f0f0', bg3: '#e6e6e6',
  bg1Glass: 'rgba(247,247,247,0.7)', bg1Chart: 'rgba(255,255,255,0.45)',
  border: 'rgba(30,30,30,0.12)', borderHi: 'rgba(30,30,30,0.22)',
  text: '#1a1816', text2: 'rgba(26,24,22,0.62)', text3: 'rgba(26,24,22,0.40)',
  green: '#1a7a42', red: '#b83030', amber: '#b87d10', blue: '#2558a8',
  greenStrong: '#0c8f43', redStrong: '#c91f1f',
  ema9: '#1a1816', ema21: '#0c7e9c',
  cardShadow: '0 1px 0 rgba(255,255,255,0.5) inset, 0 1px 2px rgba(30,30,30,0.06)',
  gridLine: 'rgba(30,30,30,0.07)',
};

// Light-end panel glass -- a 45% white veil, matched to the chart's price-pane
// tint (bg1Chart light = rgba(255,255,255,0.45)). The white wash hides the
// constellation drawn behind the page, so cards read as the same clean,
// slightly-brightened glass the candlestick chart already has. Was fully
// transparent (alpha 0) until 2026-06-13, when Nathan noted the chart looked
// visibly brighter than the cluster of stat widgets -- transparent let the
// busy constellation show through the cards while the chart's veil washed it
// out. Lifting this to 0.45 unifies all card surfaces with the chart. NOTE:
// the chart card's OUTER box is therefore set transparent in DashboardTab
// (its interior panes already paint bg1Chart), otherwise it would double up
// to ~0.70 and read brighter than the cards again.
const PANEL_BG_LIGHT = 'rgba(255,255,255,0.45)';

// The flip window (as t = brightness/100). Below FLIP_START the dial behaves
// exactly as it always has (cream text, panels frost darker as the page
// lightens). Across the window every token smoothsteps to the light palette.
// Kept late + narrow on purpose: the mid-blend has inherently muddy contrast,
// so we spend as little of the dial in it as possible.
const FLIP_START = 0.55;
const FLIP_END = 0.85;

function smoothstep(e0: number, e1: number, x: number): number {
  const u = clamp01((x - e0) / (e1 - e0));
  return u * u * (3 - 2 * u);
}

type RGBA = { r: number; g: number; b: number; a: number };

function parseColor(c: string): RGBA {
  if (c.startsWith('#')) {
    const h = c.slice(1);
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16),
      a: 1,
    };
  }
  const m = c.match(/rgba?\(([^)]+)\)/);
  const [r, g, b, a = '1'] = (m ? m[1] : '0,0,0').split(',').map(s => s.trim());
  return { r: Number(r), g: Number(g), b: Number(b), a: Number(a) };
}

// Mix two CSS colors. Hex pairs stay hex (call sites concat alpha digits onto
// hex tokens); anything else comes back as rgba.
function mixColor(dark: string, light: string, m: number): string {
  if (m <= 0) return dark;
  if (m >= 1) return light;
  if (dark.startsWith('#') && light.startsWith('#')) return lerpHex(dark, light, m);
  const d = parseColor(dark);
  const l = parseColor(light);
  const lerp = (x: number, y: number) => x + (y - x) * m;
  return `rgba(${Math.round(lerp(d.r, l.r))},${Math.round(lerp(d.g, l.g))},${Math.round(lerp(d.b, l.b))},${lerp(d.a, l.a).toFixed(3)})`;
}

// Hex color -> rgba string at the given alpha. For tokens that are hex in
// both palettes (green/red/amber/blue, text, bg1-3 -- see the format note on
// DARK_TOKENS), so callers can build intensity ramps that follow the theme.
export function withAlpha(hex: string, a: number): string {
  const { r, g, b } = parseColor(hex);
  return `rgba(${r},${g},${b},${a})`;
}

// Wash a hue over a neutral bg color while preserving its perceived lightness,
// so the brightness dial still drives dark↔light. bgTint 0 passes the neutral
// color straight through (no tint). Returns hex — several call sites concat
// alpha onto theme.bg0 (`${theme.bg0}cc`), so this MUST stay #rrggbb.
function tintBg(bg: string, bgTint: number): string {
  if (bgTint <= 0) return bg;
  const { r, g, b } = parseColor(bg);
  const lightness = ((r + g + b) / 3 / 255) * 100;
  return lerpHex(bg, hslToHex(bgTint, BG_TINT_SAT, lightness), BG_TINT_STRENGTH);
}

export function deriveTheme(brightness: number, bgTint: number = DEFAULT_BG_TINT): Theme {
  const t = clamp01(brightness / 100);
  const m = smoothstep(FLIP_START, FLIP_END, t);
  const bg0 = tintBg(lerpHex(BG0_DARK, BG0_LIGHT, t), bgTint);
  // bg0Glass is just bg0 at 55% opacity -- cheap version, used by mobile bars.
  // Parse the lerped bg0 hex back to rgb for the rgba string.
  const r = parseInt(bg0.slice(1, 3), 16);
  const g = parseInt(bg0.slice(3, 5), 16);
  const b = parseInt(bg0.slice(5, 7), 16);
  // Dark-regime panel tint keeps its original ramp (transparent at 0,
  // frosting darker as the page lightens), then crossfades to cream glass.
  const panelBgDark = `rgba(15,14,12,${(t * PANEL_TINT_MAX).toFixed(3)})`;
  const mixed = Object.fromEntries(
    (Object.keys(DARK_TOKENS) as (keyof typeof DARK_TOKENS)[]).map(k => [
      k,
      // cardShadow is a multi-part shadow string -- not mixable; step at the
      // window midpoint instead.
      k === 'cardShadow'
        ? (m < 0.5 ? DARK_TOKENS[k] : LIGHT_TOKENS[k])
        : mixColor(DARK_TOKENS[k], LIGHT_TOKENS[k], m),
    ]),
  ) as typeof DARK_TOKENS;
  return {
    ...FONTS,
    ...mixed,
    name: t < 0.5 ? 'Dark' : 'Light',
    bg0,
    bg0Glass: `rgba(${r},${g},${b},0.55)`,
    panelBg: mixColor(panelBgDark, PANEL_BG_LIGHT, m),
  };
}

export const DENSITIES: Record<DensityKey, Density> = {
  comfortable: { pad: 16, gap: 12, rowH: 44, fsBody: 14, fsLabel: 11, fsValue: 20 },
  compact:     { pad: 12, gap: 8,  rowH: 38, fsBody: 13, fsLabel: 10, fsValue: 18 },
  dense:       { pad: 9,  gap: 6,  rowH: 32, fsBody: 12, fsLabel: 9.5, fsValue: 16 },
};
