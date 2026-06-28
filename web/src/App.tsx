import { useEffect, useMemo, useState } from 'react'
import { ClaudeTerminalCard } from './components/ClaudeTerminalCard'
import { useIsMobile } from './hooks/useIsMobile'
import {
  deriveTheme, hslToHex, ACCENT_LIGHTNESS, DEFAULT_THEME_SETTINGS,
  HUE_MIN, HUE_MAX, SATURATION_MIN, SATURATION_MAX, BRIGHTNESS_MIN, BRIGHTNESS_MAX,
  BG_TINT_MIN, BG_TINT_MAX,
} from './theme'
import type { Theme, ThemeSettings } from './theme'

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
      return {
        hue: clampInto(p.hue, HUE_MIN, HUE_MAX, DEFAULT_THEME_SETTINGS.hue),
        saturation: clampInto(p.saturation, SATURATION_MIN, SATURATION_MAX, DEFAULT_THEME_SETTINGS.saturation),
        brightness: clampInto(p.brightness, BRIGHTNESS_MIN, BRIGHTNESS_MAX, DEFAULT_THEME_SETTINGS.brightness),
        bgTint: clampInto(p.bgTint, BG_TINT_MIN, BG_TINT_MAX, DEFAULT_THEME_SETTINGS.bgTint),
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
    () => deriveTheme(themeSettings.brightness, themeSettings.bgTint),
    [themeSettings.brightness, themeSettings.bgTint],
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

  return (
    <div style={{
      width: '100vw',
      height: '100dvh',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
      background: theme.bg0,
      color: theme.text,
      fontFamily: theme.fontBody,
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
  )
}
