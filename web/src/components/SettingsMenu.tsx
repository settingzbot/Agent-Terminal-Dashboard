// Settings menu — the far-right gear (⚙) dropdown in the terminal tab strip.
// Consolidates the controls that used to sit loose in the strip (launch
// command, terminal font, font size, theme color, split layout) into one
// popover, and adds a "Restart Dashboard" action that bounces the server so a
// rebuilt frontend / edited backend takes effect without leaving the browser.
//
// Structurally a twin of ThemeControls: a flat strip button opens a panel
// rendered through a portal to document.body, positioned `fixed` under the
// button. The portal is required for the same reason — the tab strip is
// `overflow-x: auto` inside an `overflow: hidden` card, so an absolutely-
// positioned panel would be clipped at the strip's edge.
//
// The Theme row embeds the existing ThemeControls component, which opens its
// OWN portaled color panel. That nested popover lives outside this panel's DOM
// subtree, so the outside-click dismiss here keys off `[role="dialog"]` (which
// both panels carry) rather than DOM containment — a click in the color panel
// keeps the settings menu open instead of collapsing the whole stack.

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { performRestart, performShutdown } from '../api/dashboard';
import { restartTerminalManager } from '../api/terminal';
import { restartAgentManager } from '../api/agents';
import { ThemeControls } from './ThemeControls';
import type { Theme, ThemeSettings } from '../theme';

type Option<T extends string | number> = { value: T; label: string };

// How each daemon-restart `method` reads back to the operator. Both managers
// respawn lazily on the next API access, so a stop IS the restart.
const METHOD_LABEL: Record<string, string> = {
  'rpc': 'stopped cleanly',
  'pid-kill': 'force-stopped',
  'not-running': 'was not running',
};

type Props = {
  theme: Theme;
  accent: string;
  isMobile: boolean;
  // Launch command auto-typed into new sessions.
  bootstrapCmd: string;
  bootstrapOptions: readonly Option<string>[];
  onBootstrapChange: (value: string) => void;
  // Terminal font family.
  termFont: string;
  fontOptions: readonly Option<string>[];
  onFontChange: (value: string) => void;
  // Terminal font size (px).
  termFontSize: number;
  fontSizeOptions: readonly number[];
  onFontSizeChange: (size: number) => void;
  // Theme dials (threaded through to the embedded ThemeControls).
  themeSettings: ThemeSettings;
  onThemeChange: (next: ThemeSettings) => void;
  // Desktop split-view: how many terminals show side by side (1–3).
  layoutCount: 1 | 2 | 3;
  onLayoutChange: (n: 1 | 2 | 3) => void;
};

type RestartState = 'idle' | 'restarting' | 'error';

export function SettingsMenu({
  theme, accent, isMobile,
  bootstrapCmd, bootstrapOptions, onBootstrapChange,
  termFont, fontOptions, onFontChange,
  termFontSize, fontSizeOptions, onFontSizeChange,
  themeSettings, onThemeChange,
  layoutCount, onLayoutChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [restart, setRestart] = useState<RestartState>('idle');
  // Full-stop lifecycle for the "Exit & Shut Down Everything" button. Its
  // middle three states line up with ShutdownState from performShutdown; the
  // extra 'idle'/'confirm' drive the two-click destructive-confirm gate.
  const [shutdown, setShutdown] = useState<'idle' | 'confirm' | 'shutting-down' | 'done' | 'error'>('idle');
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  // Fixed-position anchor for the portaled panel, measured from the button.
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  // Auto-disarm timer for the shutdown confirm (mirrors ManagerRow's pattern).
  const shutdownTimer = useRef<number | null>(null);
  const clearShutdownTimer = () => {
    if (shutdownTimer.current !== null) { window.clearTimeout(shutdownTimer.current); shutdownTimer.current = null; }
  };
  useEffect(() => clearShutdownTimer, []);

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

  // Dismiss on outside pointer / Escape. A click inside ANY open dialog (this
  // panel or the embedded color panel — both carry role="dialog") is treated
  // as inside, so picking a hue doesn't slam the whole menu shut.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      const t = e.target as Element | null;
      if (btnRef.current?.contains(t as Node)) return;
      if (t && t.closest('[role="dialog"]')) return;
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

  const doRestart = () => {
    if (restart === 'restarting') return;
    void performRestart(setRestart);
  };

  // Exit & Shut Down Everything — the full-stop counterpart to doRestart. Two
  // clicks (the second within 4s) to confirm, then drive performShutdown, whose
  // 'shutting-down' | 'done' | 'error' states feed straight into setShutdown. No
  // reload afterward: nothing relaunches, so the dashboard is gone for good.
  const doShutdown = () => {
    if (shutdown === 'shutting-down' || shutdown === 'done') return;
    // First click only arms the confirm, auto-reverting to idle after 4s.
    if (shutdown !== 'confirm') {
      setShutdown('confirm');
      clearShutdownTimer();
      shutdownTimer.current = window.setTimeout(() => setShutdown('idle'), 4000);
      return;
    }
    clearShutdownTimer();
    void performShutdown(setShutdown);
  };

  // ── shared styles ──────────────────────────────────────────────────────────
  const rowStyle: React.CSSProperties = {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    gap: 10, marginBottom: 12,
  };
  const labelStyle: React.CSSProperties = {
    fontFamily: theme.fontMono, fontSize: 10, fontWeight: 600,
    letterSpacing: '0.06em', textTransform: 'uppercase', color: theme.text2,
    whiteSpace: 'nowrap',
  };
  const selectStyle: React.CSSProperties = {
    padding: '5px 8px',
    background: theme.bg1,
    color: theme.text,
    border: `1px solid ${theme.border}`,
    borderRadius: theme.radius,
    cursor: 'pointer',
    fontFamily: theme.fontMono, fontSize: 11, fontWeight: 600,
    letterSpacing: '0.02em',
    outline: 'none',
    minWidth: 110,
  };

  const labeledSelect = (
    label: string,
    node: React.ReactNode,
  ) => (
    <div style={rowStyle}>
      <span style={labelStyle}>{label}</span>
      {node}
    </div>
  );

  // Split-view 1/2/3 segmented control (desktop only).
  const layoutBtn = (n: 1 | 2 | 3) => (
    <button
      key={n}
      onClick={() => onLayoutChange(n)}
      title={n === 1 ? 'Single terminal' : `${n} terminals side by side`}
      aria-pressed={layoutCount === n}
      style={{
        padding: '5px 10px',
        background: layoutCount === n ? `${accent}1f` : 'transparent',
        color: layoutCount === n ? accent : theme.text3,
        border: `1px solid ${layoutCount === n ? accent : theme.border}`,
        // Join the three into one segmented strip.
        borderRadius: n === 1 ? `${theme.radius}px 0 0 ${theme.radius}px`
                    : n === 3 ? `0 ${theme.radius}px ${theme.radius}px 0`
                    : 0,
        marginLeft: n === 1 ? 0 : -1,
        cursor: 'pointer',
        fontFamily: theme.fontMono, fontSize: 9, fontWeight: 700,
        letterSpacing: '0.12em',
      }}>
      {'▮'.repeat(n)}
    </button>
  );

  return (
    <>
      <button
        ref={btnRef}
        onClick={() => setOpen(o => !o)}
        title="Settings"
        aria-label="Settings"
        aria-haspopup="dialog"
        aria-expanded={open}
        style={{
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          marginLeft: isMobile ? 2 : 4,
          padding: isMobile ? '5px 10px' : '6px 12px',
          background: open ? `${accent}14` : 'transparent',
          color: open ? accent : theme.text2,
          border: 'none',
          borderRight: `1px solid ${theme.border}`,
          cursor: 'pointer',
          fontFamily: theme.fontMono, fontSize: isMobile ? 13 : 14,
          lineHeight: 1,
          flexShrink: 0,
        }}
      >
        ⚙
      </button>

      {open && pos && createPortal(
        <div
          ref={panelRef}
          role="dialog"
          aria-label="Settings"
          style={{
            position: 'fixed',
            top: pos.top,
            right: pos.right,
            width: 264,
            zIndex: 1000,
            padding: 16,
            // Cap to the viewport so the (now taller) menu scrolls instead of
            // running off-screen on short windows. ThemeControls repositions its
            // color panel on any scroll, so the embedded swatch stays anchored.
            maxHeight: 'calc(100vh - 80px)',
            overflowY: 'auto',
            background: theme.bg2,
            border: `1px solid ${theme.borderHi}`,
            borderRadius: theme.radius * 2,
            boxShadow: '0 10px 30px rgba(0,0,0,0.45)',
            backdropFilter: 'blur(12px)',
            WebkitBackdropFilter: 'blur(12px)',
          }}
        >
          <div style={{
            fontFamily: theme.fontDisplay, fontSize: 12, fontWeight: 700,
            letterSpacing: '0.08em', textTransform: 'uppercase', color: theme.text,
            marginBottom: 14,
          }}>Settings</div>

          {labeledSelect('Launch', (
            <select
              value={bootstrapCmd}
              onChange={e => onBootstrapChange(e.target.value)}
              aria-label="Launch command for new sessions"
              style={selectStyle}
            >
              {bootstrapOptions.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          ))}

          {labeledSelect('Font', (
            <select
              value={termFont}
              onChange={e => onFontChange(e.target.value)}
              aria-label="Terminal font"
              style={selectStyle}
            >
              {fontOptions.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          ))}

          {labeledSelect('Font size', (
            <select
              value={termFontSize}
              onChange={e => onFontSizeChange(parseInt(e.target.value, 10))}
              aria-label="Terminal font size"
              style={selectStyle}
            >
              {fontSizeOptions.map(s => (
                <option key={s} value={s}>{s}px</option>
              ))}
            </select>
          ))}

          {/* Theme row — the embedded swatch button opens its own color panel. */}
          <div style={rowStyle}>
            <span style={labelStyle}>Theme</span>
            <ThemeControls
              settings={themeSettings}
              onChange={onThemeChange}
              theme={theme}
              accent={accent}
              isMobile={isMobile}
            />
          </div>

          {/* Split-view layout — desktop only (mobile always shows one pane). */}
          {!isMobile && (
            <div style={rowStyle}>
              <span style={labelStyle}>Split</span>
              <div style={{ display: 'flex' }}>
                {([1, 2, 3] as const).map(layoutBtn)}
              </div>
            </div>
          )}

          <div style={{ height: 1, background: theme.border, margin: '6px 0 14px' }} />

          {/* Restart Dashboard — exits the server (launcher relaunches it), then
              polls /api/health and reloads once the new instance answers. */}
          <button
            onClick={doRestart}
            disabled={restart === 'restarting'}
            title="Restart the dashboard server, then reload — picks up a rebuilt frontend or edited backend"
            style={{
              width: '100%',
              padding: '8px 0',
              background: restart === 'error' ? `${theme.red}1f` : `${accent}14`,
              color: restart === 'error' ? theme.red : accent,
              border: `1px solid ${restart === 'error' ? theme.red : accent}`,
              borderRadius: theme.radius,
              cursor: restart === 'restarting' ? 'default' : 'pointer',
              fontFamily: theme.fontMono, fontSize: 11, fontWeight: 700,
              letterSpacing: '0.06em', textTransform: 'uppercase',
              opacity: restart === 'restarting' ? 0.7 : 1,
            }}
          >
            {restart === 'restarting' ? 'Restarting…' : '⟳ Restart Dashboard'}
          </button>

          {restart === 'restarting' && (
            <div style={{
              marginTop: 8,
              fontFamily: theme.fontMono, fontSize: 9.5, lineHeight: 1.5,
              color: theme.text3, textAlign: 'center',
            }}>
              The page will reload automatically once the server is back.
            </div>
          )}
          {restart === 'error' && (
            <div style={{
              marginTop: 8,
              fontFamily: theme.fontMono, fontSize: 9.5, lineHeight: 1.5,
              color: theme.text2, textAlign: 'center',
            }}>
              Server didn’t come back. If you launched it manually, relaunch it —{' '}
              <button
                onClick={() => window.location.reload()}
                style={{
                  background: 'none', border: 'none', padding: 0,
                  color: accent, cursor: 'pointer', textDecoration: 'underline',
                  fontFamily: theme.fontMono, fontSize: 9.5,
                }}
              >reload now</button>.
            </div>
          )}

          {/* Daemon restarts — the PTY + agent managers are standalone processes
              that survive a dashboard restart, so they have their own recycle
              buttons. Both respawn lazily on the next API access; the page does
              NOT reload (unlike Restart Dashboard above). */}
          <div style={{ height: 1, background: theme.border, margin: '16px 0 12px' }} />
          <div style={{
            fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
            letterSpacing: '0.08em', textTransform: 'uppercase', color: theme.text2,
            marginBottom: 12,
          }}>Managers</div>

          <ManagerRow
            theme={theme}
            accent={accent}
            label="Session manager"
            note="Kills all terminal sessions (unrecoverable)."
            destructive
            run={() => restartTerminalManager()}
          />
          <ManagerRow
            theme={theme}
            accent={accent}
            label="Agent manager"
            note="Halts scheduling; running agents survive."
            run={() => restartAgentManager()}
          />

          {/* Danger Zone — the full-stop counterpart to Restart Dashboard above.
              Restart bounces the server so it relaunches; this stops the entire
              stack for good (both daemons + the server process) and durably
              disarms the agent watch loop. Destructive two-click confirm, and —
              unlike Restart — the page does NOT reload, because nothing comes
              back to reload into. */}
          <div style={{ height: 1, background: theme.border, margin: '16px 0 12px' }} />
          <div style={{
            fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
            letterSpacing: '0.08em', textTransform: 'uppercase', color: theme.red,
            marginBottom: 12,
          }}>Danger Zone</div>

          <button
            onClick={doShutdown}
            disabled={shutdown === 'shutting-down' || shutdown === 'done'}
            title="Stop both daemons, kill all terminal sessions, disarm the agent watch loop, and exit the server — the dashboard does not come back"
            style={{
              width: '100%',
              padding: '8px 0',
              background: `${theme.red}14`,
              color: theme.red,
              border: `1px solid ${theme.red}`,
              borderRadius: theme.radius,
              cursor: (shutdown === 'shutting-down' || shutdown === 'done') ? 'default' : 'pointer',
              fontFamily: theme.fontMono, fontSize: 11, fontWeight: 700,
              letterSpacing: '0.06em', textTransform: 'uppercase',
              opacity: (shutdown === 'shutting-down' || shutdown === 'done') ? 0.7 : 1,
            }}
          >
            {shutdown === 'confirm'        ? 'Click again to confirm'
             : shutdown === 'shutting-down' ? 'Shutting down…'
             : shutdown === 'done'          ? '✓ Dashboard stopped'
             : shutdown === 'error'         ? '⚠ Did not stop — retry'
             : '⏻ Exit & Shut Down Everything'}
          </button>

          <div style={{
            marginTop: 8,
            fontFamily: theme.fontMono, fontSize: 9.5, lineHeight: 1.5,
            color: shutdown === 'done'  ? theme.green
                 : shutdown === 'error' ? theme.red
                 : shutdown === 'confirm' ? theme.text2
                 : theme.text3,
            textAlign: 'center',
          }}>
            {shutdown === 'shutting-down' ? (
              'Stopping daemons and exiting… this window can be closed once it stops.'
            ) : shutdown === 'done' ? (
              'Dashboard stopped. The agent watch loop is disarmed; you can close this window.'
            ) : shutdown === 'error' ? (
              'Server did not exit. It may still be running — close the launcher window manually to stop it.'
            ) : shutdown === 'confirm' ? (
              <>
                <span style={{ color: theme.red, fontWeight: 700 }}>This kills every terminal session and exits.</span>{' '}
                Stops both daemons, kills all terminal sessions, disarms the agent watch loop, and exits the server. The launcher window will not relaunch.
              </>
            ) : (
              'Stops both daemons, kills all terminal sessions, disarms the agent watch loop, and exits the server. The launcher window will not relaunch.'
            )}
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

// One row in the Managers section: a labelled daemon with a Restart button and
// an inline status line. `destructive` rows (the session manager kills every
// terminal) require a second "Confirm?" click, auto-disarming after 4s. The
// async restart reports back its method ("stopped cleanly" / "force-stopped" /
// "was not running") on success, or the error detail on failure.
function ManagerRow({
  theme, accent, label, note, destructive, run,
}: {
  theme: Theme;
  accent: string;
  label: string;
  note: string;
  destructive?: boolean;
  run: () => Promise<{ ok: boolean; method: string; detail: string }>;
}) {
  const [state, setState] = useState<'idle' | 'confirm' | 'busy' | 'ok' | 'error'>('idle');
  const [msg, setMsg] = useState('');
  const timerRef = useRef<number | null>(null);
  const clearTimer = () => {
    if (timerRef.current !== null) { window.clearTimeout(timerRef.current); timerRef.current = null; }
  };
  useEffect(() => clearTimer, []);

  const onClick = () => {
    if (state === 'busy') return;
    // First click on a destructive row only arms the confirm.
    if (destructive && state !== 'confirm') {
      setState('confirm');
      clearTimer();
      timerRef.current = window.setTimeout(() => setState('idle'), 4000);
      return;
    }
    clearTimer();
    setState('busy');
    run().then(r => {
      setState(r.ok ? 'ok' : 'error');
      setMsg(r.ok ? (METHOD_LABEL[r.method] ?? 'restarted') : (r.detail || 'failed'));
      if (r.ok) timerRef.current = window.setTimeout(() => setState('idle'), 3000);
    }).catch(e => {
      setState('error');
      setMsg(e instanceof Error ? e.message : 'request failed');
    });
  };

  const danger = state === 'confirm';
  const btnLabel = state === 'busy' ? '…' : danger ? 'Confirm?' : 'Restart';

  const statusLine =
    state === 'ok'      ? `✓ ${msg} — respawns on next use`
    : state === 'error' ? `✕ ${msg}`
    : danger            ? 'Click Confirm to proceed.'
    : note;
  const statusColor =
    state === 'error' ? theme.red
    : state === 'ok'  ? theme.green
    : danger          ? theme.red
    : theme.text3;

  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10,
      }}>
        <span style={{
          fontFamily: theme.fontMono, fontSize: 10.5, fontWeight: 600, color: theme.text,
        }}>{label}</span>
        <button
          onClick={onClick}
          disabled={state === 'busy'}
          style={{
            padding: '4px 10px',
            background: danger ? `${theme.red}1f` : 'transparent',
            color: danger ? theme.red : accent,
            border: `1px solid ${danger ? theme.red : `${accent}88`}`,
            borderRadius: theme.radius,
            cursor: state === 'busy' ? 'default' : 'pointer',
            fontFamily: theme.fontMono, fontSize: 9.5, fontWeight: 700,
            letterSpacing: '0.06em', textTransform: 'uppercase',
            whiteSpace: 'nowrap', flexShrink: 0,
            opacity: state === 'busy' ? 0.7 : 1,
          }}
        >{btnLabel}</button>
      </div>
      <div style={{
        marginTop: 4,
        fontFamily: theme.fontMono, fontSize: 9, lineHeight: 1.5,
        color: statusColor,
      }}>{statusLine}</div>
    </div>
  );
}
