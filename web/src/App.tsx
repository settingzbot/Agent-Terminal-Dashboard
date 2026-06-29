import { useEffect, useMemo, useState } from 'react'
import { ClaudeTerminalCard } from './components/ClaudeTerminalCard'
import { LandingBackground } from './components/LandingBackground'
import { FishTankBackground } from './components/FishTankBackground'
import { useIsMobile } from './hooks/useIsMobile'
import {
  deriveTheme, deriveVignette, hslToHex, ACCENT_LIGHTNESS, DEFAULT_THEME_SETTINGS,
  HUE_MIN, HUE_MAX, SATURATION_MIN, SATURATION_MAX, BRIGHTNESS_MIN, BRIGHTNESS_MAX,
  BG_TINT_MIN, BG_TINT_MAX, BG_TINT_STRENGTH_MIN, BG_TINT_STRENGTH_MAX,
} from './theme'
import type { BackgroundMode, Theme, ThemeSettings } from './theme'

// Background modes that survive a localStorage round-trip — anything else folds
// back to the flat default.
const BACKGROUND_MODES: readonly BackgroundMode[] = ['none', 'constellation', 'fishtank']

// Persisted user theme dials (hue / saturation / brightness). Extracted from
// Trident's dashboard, where the color theme was operator-tunable.
const THEME_KEY = 'claudeThemeSettings'

const clampInto = (n: unknown, lo: number, hi: number, fallback: number): number =>
  typeof n === 'number' && Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : fallback

function loadThemeSettings(): ThemeSettings {
  try {
    const raw = localStorage.getItem(THEME_KEY)
    if (raw) {
      const p = JSON.parse(raw) as Partial<ThemeSettings>
      // Pre-split saves stored bgTint as a single dial (0 = off, 1–360 = hue at
      // a fixed 0.14 mix) with no bgTintStrength. The presence of bgTintStrength
      // is what tells the two formats apart — in a new save bgTint = 0 is a
      // legitimate hue (red), not "off". Fold an old save forward: an old hue
      // > 0 becomes the same hue at strength 14 (≈ the old fixed mix) so the
      // tint survives the upgrade; an old 0/absent (off) maps to the default hue
      // at strength 0 (still off), since 0 is no longer a valid "off" hue.
      const isNewFormat = typeof p.bgTintStrength === 'number'
      const hadOldTint = !isNewFormat && typeof p.bgTint === 'number' && p.bgTint > 0
      return {
        hue: clampInto(p.hue, HUE_MIN, HUE_MAX, DEFAULT_THEME_SETTINGS.hue),
        saturation: clampInto(p.saturation, SATURATION_MIN, SATURATION_MAX, DEFAULT_THEME_SETTINGS.saturation),
        brightness: clampInto(p.brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX, DEFAULT_THEME_SETTINGS.brightness),
        bgTint: (isNewFormat || hadOldTint)
          ? clampInto(p.bgTint, BG_TINT_MIN, BG_TINT_MAX, DEFAULT_THEME_SETTINGS.bgTint)
          : DEFAULT_THEME_SETTINGS.bgTint,
        bgTintStrength: clampInto(
          p.bgTintStrength, BG_TINT_STRENGTH_MIN, BG_TINT_STRENGTH_MAX,
          hadOldTint ? 14 : DEFAULT_THEME_SETTINGS.bgTintStrength,
        ),
        backgroundMode: BACKGROUND_MODES.includes(p.backgroundMode as BackgroundMode)
          ? (p.backgroundMode as BackgroundMode)
          : DEFAULT_THEME_SETTINGS.backgroundMode,
      }
    }
  } catch { /* private mode / malformed — fall through to defaults */ }
  return DEFAULT_THEME_SETTINGS
}

export default function App() {
  const isMobile = useIsMobile()

  const [themeSettings, setThemeSettings] = useState<ThemeSettings>(loadThemeSettings)

  // The whole app recolors off these two derived values.
  const theme: Theme = useMemo(
    () => deriveTheme(themeSettings.brightness, themeSettings.bgTint, themeSettings.bgTintStrength),
    [themeSettings.brightness, themeSettings.bgTint, themeSettings.bgTintStrength],
  )
  const accent = useMemo(
    () => hslToHex(themeSettings.hue, themeSettings.saturation, ACCENT_LIGHTNESS),
    [themeSettings.hue, themeSettings.saturation],
  )

  // Persist the dials so the chosen theme survives reloads.
  useEffect(() => {
    try { localStorage.setItem(THEME_KEY, JSON.stringify(themeSettings)) }
    catch { /* quota / private mode — theme just won't persist */ }
  }, [themeSettings])

  // Keep the document background in step with the page bg so light mode doesn't
  // flash the hard-coded dark from index.html on overscroll / before paint.
  useEffect(() => {
    document.body.style.background = theme.bg0
  }, [theme.bg0])

  const { backgroundMode } = themeSettings

  return (
    <div style={{
      width: '100vw',
      height: '100dvh',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
      // The flat tone / animated field paints behind via the fixed layers below
      // (zIndex 0); the content wrapper sits above at zIndex 1. Transparent here
      // so the chosen background shows through the terminal's glass surfaces.
      background: 'transparent',
      color: theme.text,
      fontFamily: theme.fontBody,
      position: 'relative',
    }}>
      {/* Background tone — a flat bg0 fill behind everything for Flat AND
          Constellation, so the constellation canvas (which is transparent) has
          a page color to drift over. Fish Tank paints its own opaque water. */}
      {backgroundMode !== 'fishtank' && (
        <div style={{
          position: 'fixed', inset: 0, zIndex: 0,
          pointerEvents: 'none', background: theme.bg0,
        }} />
      )}
      {/* Animated layer on top of the tone. */}
      {backgroundMode === 'constellation' ? (
        <LandingBackground
          mode="constellation"
          density={isMobile ? 1.4 : 0.9}
          speed={isMobile ? 0.7 : 0.5}
          grain={0}
          intensity={isMobile ? 2.5 : 1.5}
          anchor="viewport"
          theme={theme}
          accent={accent}
          vignette={deriveVignette(themeSettings.brightness)}
        />
      ) : backgroundMode === 'fishtank' ? (
        <FishTankBackground />
      ) : null}

      {/* Content above the background. */}
      <div style={{
        position: 'relative', zIndex: 1,
        flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0,
      }}>
        <ClaudeTerminalCard
          theme={theme}
          accent={accent}
          themeSettings={themeSettings}
          onThemeChange={setThemeSettings}
          isMobile={isMobile}
          isActive={true}
        />
      </div>
    </div>
  )
}
