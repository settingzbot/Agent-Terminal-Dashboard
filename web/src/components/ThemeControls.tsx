// Theme controls — the tab-strip dropdown that tunes the dashboard's primary
// color. A flat strip button (swatch + ▾) opens a small popover of sliders: HUE
// (which color), SATURATION (how vivid), BRIGHTNESS (the page's dark↔light
// dial), and a background wash pair — BG HUE (which color) + BG STRENGTH (how
// much, 0 = off). Plus Dark/Light snaps and a reset to the original
// amber-on-dark default.
//
// The owning state lives in App.tsx (persisted to localStorage); this component
// is presentational — it renders the current `settings` and calls `onChange`
// with the next value on every slider/button interaction so the whole app
// recolors live.
//
// The popover is rendered through a portal to document.body and positioned
// `fixed` under the button. It HAS to escape the portal: the tab strip is
// `overflow-x: auto` inside an `overflow: hidden` card, so an absolutely-
// positioned panel would be clipped at the strip's edge.

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  hslToHex,
  HUE_MIN, HUE_MAX, SATURATION_MIN, SATURATION_MAX,
  BRIGHTNESS_MIN, BRIGHTNESS_MAX, BG_TINT_MIN, BG_TINT_MAX,
  BG_TINT_STRENGTH_MIN, BG_TINT_STRENGTH_MAX, ACCENT_LIGHTNESS,
  DEFAULT_THEME_SETTINGS,
  type Theme, type ThemeSettings,
} from '../theme';

type Props = {
  settings: ThemeSettings;
  onChange: (next: ThemeSettings) => void;
  theme: Theme;
  accent: string;
  isMobile?: boolean;
};

// Full hue wheel as a CSS gradient for the hue slider's track. Built once from
// hslToHex so it tracks the exact palette the accent is sampled from.
const HUE_GRADIENT = `linear-gradient(to right, ${
  [0, 60, 120, 180, 240, 300, 360]
    .map(h => hslToHex(h, 95, ACCENT_LIGHTNESS))
    .join(', ')
})`;

// Background-hue track: the full hue wheel (the wash is always a valid hue now;
// whether it shows at all is governed by the separate Strength dial). Muted sat
// to hint that the wash itself is subtle, not a full-strength accent.
const BG_TINT_GRADIENT = `linear-gradient(to right, ${
  [0, 60, 120, 180, 240, 300, 360]
    .map(h => hslToHex(h, 70, 50))
    .join(', ')
})`;

export function ThemeControls({ settings, onChange, theme, accent, isMobile = false }: Props) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Fixed-position anchor for the portaled panel, measured from the button.
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);

  // Keep the panel pinned under the button across resizes / strip scrolls.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setPos({ top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) });
    };
    place();
    window.addEventListener('resize', place);
    // capture: catch scrolls on the tab strip and any ancestor, not just window.
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);

  // Dismiss on outside pointer / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      const t = e.target as Node;
      if (panelRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('touchstart', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('touchstart', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const patch = (p: Partial<ThemeSettings>) => onChange({ ...settings, ...p });

  // Saturation track: gray (current hue, 0% sat) → full vivid at the current
  // hue, so the dial previews exactly what dragging it does.
  const satGradient = `linear-gradient(to right, ${
    hslToHex(settings.hue, SATURATION_MIN, ACCENT_LIGHTNESS)
  }, ${hslToHex(settings.hue, SATURATION_MAX, ACCENT_LIGHTNESS)})`;
  // Brightness track mirrors the page's dark→white lerp endpoints (deriveTheme).
  const brightGradient = 'linear-gradient(to right, #0f0e0c, #ffffff)';
  // BG-strength track: neutral gray at OFF easing into the CURRENT background
  // hue, so the dial previews exactly the wash that dragging it up applies.
  const bgStrengthGradient = `linear-gradient(to right, ${
    hslToHex(0, 0, 45)
  }, ${hslToHex(settings.bgTint, 70, 50)})`;

  const labelStyle: React.CSSProperties = {
    display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
    marginBottom: 6,
    fontFamily: theme.fontMono, fontSize: 10, fontWeight: 600,
    letterSpacing: '0.06em', textTransform: 'uppercase',
    color: theme.text2,
  };
  const valueStyle: React.CSSProperties = {
    fontVariantNumeric: 'tabular-nums', color: accent, fontWeight: 700,
  };

  const slider = (
    key: 'hue' | 'saturation' | 'brightness' | 'bgTint' | 'bgTintStrength',
    label: string,
    min: number, max: number,
    track: string,
    fmt: (n: number) => string,
  ) => (
    <div style={{ marginBottom: 16 }} key={key}>
      <div style={labelStyle}>
        <span>{label}</span>
        <span style={valueStyle}>{fmt(settings[key])}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        value={settings[key]}
        onChange={e => patch({ [key]: Number(e.target.value) } as Partial<ThemeSettings>)}
        aria-label={label}
        className="theme-ctrl-slider"
        style={{ width: '100%', background: track, ['--thumb' as string]: accent }}
      />
    </div>
  );

  // Snap buttons (Dark/Light = brightness 0/100; Reset = ship defaults).
  const snapBtn = (label: string, onClick: () => void, activeWhen: boolean) => (
    <button
      onClick={onClick}
      style={{
        flex: 1,
        padding: '6px 0',
        background: activeWhen ? `${accent}1f` : 'transparent',
        color: activeWhen ? accent : theme.text2,
        border: `1px solid ${activeWhen ? accent : theme.border}`,
        borderRadius: theme.radius,
        cursor: 'pointer',
        fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
        letterSpacing: '0.06em', textTransform: 'uppercase',
      }}
    >
      {label}
    </button>
  );

  return (
    <>
      {/* Inject the range thumb styling once. Track color is set inline per
          slider; the thumb reads the accent from the --thumb custom prop. */}
      <style>{`
        .theme-ctrl-slider {
          -webkit-appearance: none; appearance: none;
          height: 6px; border-radius: 3px; outline: none; cursor: pointer;
          margin: 0;
        }
        .theme-ctrl-slider::-webkit-slider-thumb {
          -webkit-appearance: none; appearance: none;
          width: 15px; height: 15px; border-radius: 50%;
          background: var(--thumb); border: 2px solid #fff;
          box-shadow: 0 0 0 1px rgba(0,0,0,0.35); cursor: pointer;
        }
        .theme-ctrl-slider::-moz-range-thumb {
          width: 15px; height: 15px; border-radius: 50%;
          background: var(--thumb); border: 2px solid #fff;
          box-shadow: 0 0 0 1px rgba(0,0,0,0.35); cursor: pointer;
        }
      `}</style>

      <button
        ref={btnRef}
        onClick={() => setOpen(o => !o)}
        title="Theme color — hue, saturation, brightness"
        aria-label="Theme color controls"
        aria-haspopup="dialog"
        aria-expanded={open}
        style={{
          display: 'flex', alignItems: 'center', gap: 6,
          marginLeft: isMobile ? 2 : 4,
          padding: isMobile ? '5px 8px' : '6px 10px',
          background: open ? `${accent}14` : 'transparent',
          color: theme.text2,
          border: 'none',
          borderRight: `1px solid ${theme.border}`,
          cursor: 'pointer',
          fontFamily: theme.fontMono, fontSize: isMobile ? 9 : 10, fontWeight: 600,
          letterSpacing: '0.04em',
          flexShrink: 0,
        }}
      >
        {/* Live accent swatch so the current color reads at a glance. */}
        <span style={{
          width: 11, height: 11, borderRadius: '50%',
          background: accent, flexShrink: 0,
          boxShadow: `0 0 0 1px ${theme.border}`,
        }}/>
        <span style={{ color: theme.text2 }}>▾</span>
      </button>

      {open && pos && createPortal(
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Theme color controls"
          style={{
            position: 'fixed',
            top: pos.top,
            right: pos.right,
            width: 248,
            zIndex: 1000,
            padding: 16,
            background: theme.bg2,
            border: `1px solid ${theme.borderHi}`,
            borderRadius: theme.radius * 2,
            boxShadow: '0 10px 30px rgba(0,0,0,0.45)',
            backdropFilter: 'blur(12px)',
            WebkitBackdropFilter: 'blur(12px)',
          }}
        >
          <div style={{
            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
            marginBottom: 14,
          }}>
            <span style={{
              fontFamily: theme.fontDisplay, fontSize: 12, fontWeight: 700,
              letterSpacing: '0.08em', textTransform: 'uppercase', color: theme.text,
            }}>Theme</span>
            <button
              onClick={() => onChange(DEFAULT_THEME_SETTINGS)}
              title="Reset to the default amber theme"
              style={{
                padding: '3px 8px',
                background: 'transparent',
                color: theme.text3,
                border: `1px solid ${theme.border}`,
                borderRadius: theme.radius,
                cursor: 'pointer',
                fontFamily: theme.fontMono, fontSize: 9, fontWeight: 700,
                letterSpacing: '0.06em', textTransform: 'uppercase',
              }}
            >Reset</button>
          </div>

          {slider('hue', 'Hue', HUE_MIN, HUE_MAX, HUE_GRADIENT, n => `${Math.round(n)}°`)}
          {slider('saturation', 'Saturation', SATURATION_MIN, SATURATION_MAX, satGradient, n => `${Math.round(n)}%`)}
          {slider('brightness', 'Brightness', BRIGHTNESS_MIN, BRIGHTNESS_MAX, brightGradient, n => `${Math.round(n)}%`)}
          {slider('bgTint', 'BG Hue', BG_TINT_MIN, BG_TINT_MAX, BG_TINT_GRADIENT, n => `${Math.round(n)}°`)}
          {slider('bgTintStrength', 'BG Strength', BG_TINT_STRENGTH_MIN, BG_TINT_STRENGTH_MAX, bgStrengthGradient, n => n <= 0 ? 'Off' : `${Math.round(n)}%`)}

          <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
            {snapBtn('Dark', () => patch({ brightness: BRIGHTNESS_MIN }), settings.brightness <= BRIGHTNESS_MIN)}
            {snapBtn('Light', () => patch({ brightness: BRIGHTNESS_MAX }), settings.brightness >= BRIGHTNESS_MAX)}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
