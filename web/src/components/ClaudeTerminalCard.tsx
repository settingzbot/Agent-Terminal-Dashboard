// Persistent in-browser terminal for the Claude tab.
//
// Each tab is a backend PowerShell PTY (see trident_terminal.py) that survives
// browser disconnects. Lets Nathan run `claude --resume <id>` to re-enter prior
// conversations — something the Remote Control launcher above can't do — and
// also doubles as a general shell (git, scripts, etc.) for one-tap phone access.
//
// Tab state model: backend owns truth (sessions list comes from the API). The
// frontend keeps an xterm.Terminal instance alive per session for as long as
// the card is mounted; switching tabs just toggles which container is visible.
// One-time .open(el) means we cannot move a terminal between containers, so
// each session gets its own div in the same host area, shown or hidden in
// place — never reparented.
//
// Desktop split view (Nathan, 2026-06-11): a ▮/▮▮/▮▮▮ toggle in the tab strip
// shows 1–3 sessions side by side. The host becomes a flex row; each visible
// session's div is ordered into its pane slot via CSS `order` and sized by
// flex — same DOM elements, style-only changes, so the no-reparenting rule
// holds. One shared tab strip drives all panes: the last-clicked pane is
// "focused" (accent outline) and clicking a tab loads that session into it;
// clicking a tab already on screen just focuses its pane. Mobile always
// renders exactly one full-width session (the pane state exists but only the
// focused pane shows).
//
// Mobile shape:
//   Tab strip flush at top (safe-area inset), terminal fills the middle,
//   chat box + tool row rest 30px above the bottom tab bar when the keyboard
//   is down, and sit flush on top of the keyboard when it's up. The keyboard
//   transition is driven by `useChatBottomOffset`, which reads from
//   `window.visualViewport` — the only signal iOS 26 Safari exposes (the
//   VirtualKeyboard API, the `interactive-widget` viewport directive, and
//   `env(keyboard-inset-*)` are all unimplemented on WebKit). Tapping the
//   chat box summons the iOS keyboard; tapping the terminal does nothing (so
//   long-press text selection works without triggering it). Send button is
//   dual-mode (text → push text without enter; empty → send `\r`).
//
// WS protocol mirrored from dashboard/routes_ws.py:ws_terminal:
//   server → <binary frame>            (output chunk: raw UTF-8 PTY bytes —
//                                       the server sends these because our WS
//                                       URL carries ?bin=1; replay included)
//   server → {type:'out', data}        (output chunk, legacy JSON framing —
//                                       only from a pre-binary dashboard that
//                                       ignores ?bin=1; kept for compat)
//   server → {type:'exit'}             (pty closed)
//   client → {type:'in', data}         (keystrokes from term.onData)
//   client → {type:'resize', rows, cols}
//   client → {type:'flow'}             (on open: we support ACK flow control)
//   client → {type:'ack', bytes}       (delta of payload bytes xterm processed)
//
// Binary output framing (Phase 3, 2026-06-10): mixed text/binary on one WS —
// the standard ttyd / VS Code pattern. Output rides binary frames (no JSON
// escaping/parse overhead per chunk); control messages stay JSON text in both
// directions. xterm's term.write accepts Uint8Array directly (decoded as
// UTF-8, statefully — split multibyte chars across frames reassemble fine).
//
// Flow control (Phase 2, 2026-06-10): every output chunk goes through
// term.write(chunk, callback); the callback fires once xterm has actually
// parsed the chunk. We ACK every ~100KB processed (plus a drain-flush when
// the write queue catches up), and the server pauses the PTY upstream when
// too many bytes are in flight — so a `yes`-style burst backpressures onto
// the child process instead of freezing the browser. Keystrokes are never
// gated; ^C interrupts instantly even mid-burst. ACK unit: raw UTF-8 byte
// length of the payload — byteLength for binary frames, TextEncoder bytes of
// the data string for legacy JSON frames; the server counts the identical
// quantity in both formats.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebglAddon } from '@xterm/addon-webgl';
import '@xterm/xterm/css/xterm.css';
import {
  createSession, killSession, listSessions, renameSession, terminalWsUrl, uploadClaudeFile,
  type TerminalSession,
} from '../api/terminal';
import { type Theme, type ThemeSettings } from '../theme';
import { useKeyboardHeightPx } from '../hooks/useKeyboardHeightPx';
import { useAgentPulse } from '../hooks/useAgentPulse';
import { AgentManagerPane } from './AgentManager/AgentManagerPane';
import { SettingsMenu } from './SettingsMenu';

// BOTTOM_TAB_BAR_HEIGHT_CSS inlined from BottomTabBar.tsx to avoid the dependency.
// Original: 'calc(52px + env(safe-area-inset-bottom, 0px))'
const BOTTOM_TAB_BAR_HEIGHT_CSS = 'calc(52px + env(safe-area-inset-bottom, 0px))';

// An agent-run session is a pty session the agent-manager created with an
// "agent:<run-id>" tag (trident_pty_manager #25). We surface live ones as
// `agent N` tabs beside the operator's own shells.
const isAgentSession = (s: { tag?: string | null }): boolean =>
  typeof s.tag === 'string' && s.tag.startsWith('agent:');

// Mobile chat group sizing — kept here so the spacer that reserves room above
// the keyboard tracks the actual chat height. Single tool row + single-line
// contenteditable + container padding ≈ 86px.
const CHAT_AREA_HEIGHT_PX = 86;

// Mobile terminal overdraw (Nathan, 2026-06-10): the grid extends past its
// visible box at the BOTTOM so the last row reaches ~1 row closer to the chat
// box. FitAddon measures the extended container, so the PTY simply gets the
// extra rows — no special-casing anywhere else.
//
// TOP overdraw retired (Nathan, 2026-06-13): it used to extend ~2 rows up
// behind the frosted tab strip so scrolling text slid out from behind it
// (and the smooth-scroll blank-strip artifact hid there). But Claude Code
// paints a top-anchored full-screen TUI, so its first line landed in that
// hidden band and was permanently obscured by the strip. Zeroing the top
// overdraw makes the terminal sit flush below the strip — every row, including
// the first, is fully visible. Tradeoff accepted: lose the slide-from-behind
// flourish; a fast fling may briefly show a blank sliver at the top edge.
const TERM_OVERDRAW_TOP_PX = 0;
const TERM_EXTEND_BOTTOM_PX = 14;

// Last measured iOS keyboard height, persisted so the chat box can PRE-LIFT
// to its above-keyboard position the instant it's focused — before the
// keyboard has animated up and been measured (rationale at the chatBottom
// computation below). 336px is the stock iPhone portrait QWERTY + QuickType
// bar; only used before the first real measurement ever lands.
const LAST_KBD_PX_KEY = 'claudeLastKbdPx';

// Desktop split-view layout (1, 2, or 3 panes), persisted across reloads.
const LAYOUT_KEY = 'claudeTermLayout';
type LayoutCount = 1 | 2 | 3;
const readLastKbdPx = (): number => {
  try {
    const v = parseInt(localStorage.getItem(LAST_KBD_PX_KEY) ?? '', 10);
    return Number.isFinite(v) && v > 100 ? v : 336;
  } catch {
    return 336;
  }
};

type Props = {
  theme: Theme;
  accent: string;
  // Current theme dials + setter, threaded down so the tab-strip ThemeControls
  // dropdown can recolor the whole app live (owned/persisted in App.tsx).
  themeSettings: ThemeSettings;
  onThemeChange: (next: ThemeSettings) => void;
  isMobile?: boolean;
  isActive?: boolean;
};

// Raw byte sequences for keys the iOS / Android software keyboard doesn't
// expose (plus "/" for one-tap slash commands). ESC interrupts Claude's
// prompts, "/" opens Claude Code's slash-command menu, and the arrows walk
// command history / move the cursor — all awkward or unreachable from a
// stock mobile keyboard. ^C used to live here too; its tool-row slot became
// the ↻ refresh button (Nathan, 2026-06-12) — ESC covers interrupting
// Claude, and garbled output was the more frequent phone-side pain.
const KEYS = {
  ESC:   '\x1b',
  SLASH: '/',
  UP:    '\x1b[A',
  DOWN:  '\x1b[B',
  LEFT:  '\x1b[D',
  RIGHT: '\x1b[C',
};

// ── terminal font choices ───────────────────────────────────────────────────
// Selectable from the tab-strip dropdown, persisted in localStorage. JetBrains
// Mono already ships with the dashboard (loaded in dashboard.html); the other
// web fonts are fetched from Google Fonts on demand the first time they're
// picked, then browser-cached. Every family chain falls back to device
// monospace (Menlo on iOS/macOS, Consolas on Windows) so an offline pick still
// renders. 'system' skips the network entirely and uses the device font.
const FONT_CHOICES = [
  { value: 'jetbrains', label: 'JetBrains',   gfFamily: 'JetBrains Mono',  gfUrl: null },
  { value: 'cascadia',  label: 'Cascadia',    gfFamily: 'Cascadia Code',   gfUrl: 'https://fonts.googleapis.com/css2?family=Cascadia+Code:wght@400;700&display=swap' },
  { value: 'fira',      label: 'Fira Code',   gfFamily: 'Fira Code',       gfUrl: 'https://fonts.googleapis.com/css2?family=Fira+Code:wght@400;700&display=swap' },
  { value: 'plex',      label: 'Plex Mono',   gfFamily: 'IBM Plex Mono',   gfUrl: 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&display=swap' },
  { value: 'source',    label: 'Source Code', gfFamily: 'Source Code Pro', gfUrl: 'https://fonts.googleapis.com/css2?family=Source+Code+Pro:wght@400;700&display=swap' },
  { value: 'system',    label: 'System',      gfFamily: null,              gfUrl: null },
] as const;
type FontChoice = typeof FONT_CHOICES[number];

const findFontChoice = (value: string): FontChoice =>
  FONT_CHOICES.find(c => c.value === value) ?? FONT_CHOICES[0];

// ── terminal font sizes ──────────────────────────────────────────────────────
// Selectable from the tab-strip dropdown (desktop + mobile), persisted in
// localStorage. 13 is the long-standing desktop default; mobile defaults to 12
// (its split-view geometry is tuned for one full-width terminal) until changed.
const FONT_SIZE_CHOICES = [8, 9, 10, 11, 12, 13, 14, 15, 16, 18] as const;
const DEFAULT_FONT_SIZE = 13;
const clampFontSize = (n: number): number =>
  FONT_SIZE_CHOICES.includes(n as typeof FONT_SIZE_CHOICES[number]) ? n : DEFAULT_FONT_SIZE;

// The final font-family string handed to xterm for a choice.
const fontFamilyFor = (c: FontChoice): string =>
  c.gfFamily
    ? `"${c.gfFamily}", Menlo, Consolas, monospace`
    : 'Menlo, Consolas, "Courier New", monospace';

// Inject the choice's Google Fonts stylesheet (once per choice), then resolve
// when the face is actually rendered-ready — the signal that re-setting
// fontFamily on a term will measure against the real font, not a fallback.
// Resolves immediately for system fonts; resolves (not rejects) on network
// failure so callers can apply the fallback chain unconditionally.
const ensureFontLoaded = (c: FontChoice): Promise<void> => {
  if (!c.gfFamily || typeof document === 'undefined' || !document.fonts) {
    return Promise.resolve();
  }
  if (c.gfUrl && !document.getElementById(`term-font-${c.value}`)) {
    const link = document.createElement('link');
    link.id = `term-font-${c.value}`;
    link.rel = 'stylesheet';
    link.href = c.gfUrl;
    document.head.appendChild(link);
  }
  return document.fonts.load(`12px "${c.gfFamily}"`)
    .then(() => undefined)
    .catch(() => undefined);
};

// ── dark-gray background remap ──────────────────────────────────────────────
// Claude Code (v2.1.17x restyle) paints a hard-coded dark-gray truecolor
// background — SGR 48;2;55;55;55 — behind user-message echoes and the
// example-prompt chips. Designed for a solid black terminal; on our
// transparent frosted card it reads as black boxes (Nathan, 2026-06-10).
// xterm has no hook to theme truecolor, so we rewrite the SGR params in the
// output stream before they reach term.write: matching backgrounds become
// SGR 49 (default background = fully transparent) — Nathan picked removal
// over recoloring (2026-06-10). Scope is deliberately narrow: only truecolor
// *backgrounds* that are near-greyscale (channel spread ≤ 10) in the 30–80
// lightness band get stripped — that's the "subtle highlight on black"
// class. Diff colors (e.g. 2;40;0 green) are chromatic and pass through;
// explicit black (0;0;0) is left alone in case it's used as invisible
// filler. Known tradeoff: if Claude Code ever highlights a menu's selected
// row with a gray in this band, that highlight vanishes too — if that
// bites, narrow the band or recolor instead of stripping.
//
// Accent recolor (Nathan, 2026-06-11): the stripped gray is also the only
// marker that distinguishes user-message echoes from Claude's own output, so
// while a chip background is open we force the *foreground* to the dashboard
// accent — sent messages render in the theme color instead of plain text.
// Injecting the accent once at chip-open wasn't enough: Claude Code sets the
// echo text's color explicitly (its own truecolor/dim foreground sequences,
// emitted per styled segment after the bg opens), which overrode the
// injection — Nathan's "still just white" report, 2026-06-11. So the rewrite
// is stateful across sequences: from chip-open until the chip closes, every
// foreground set (truecolor 38;2, 256-color 38;5, basic 30–37/90–97, default
// 39) is replaced with the accent. The chip closes at SGR 49 (default bg —
// we chase it with SGR 39 so the accent never bleeds into Claude's replies)
// or at a full reset (which clears fg and bg on its own). The accent is read
// through a getter at rewrite time, so an accent switch in Settings applies
// to messages rendered from then on.
//
// Implementation notes:
// - Operates on raw UTF-8 bytes. ESC (0x1b) can never appear inside a UTF-8
//   multibyte character (all continuation bytes are ≥ 0x80), so scanning
//   bytes is safe without decoding.
// - A CSI sequence can be split across WS frames; bytes from an unterminated
//   sequence at the end of a chunk are carried into the next push(). flush()
//   releases the carry (used with a short timer so a dangling ESC at stream
//   idle never sticks).
// - ACK flow control counts WIRE bytes, which the caller snapshots before
//   the rewrite — the transformed length never enters the ACK math.
class SgrBgRemapper {
  private _carry: Uint8Array | null = null;
  // True between injecting the accent foreground (at a chip-background strip)
  // and emitting its close — tells the rewriter that the next bg-close /
  // reset needs an SGR 39 appended. Survives across push() calls because a
  // chip's open and close can land in different WS frames.
  private _fgInjected = false;

  // Returns the accent as [r, g, b], or null to skip the recolor
  // (unparsable accent) and only strip the background as before.
  private _getChipFg: () => [number, number, number] | null;

  constructor(getChipFg: () => [number, number, number] | null) {
    this._getChipFg = getChipFg;
  }

  push(chunk: Uint8Array): Uint8Array {
    let buf: Uint8Array;
    if (this._carry) {
      buf = new Uint8Array(this._carry.length + chunk.length);
      buf.set(this._carry, 0);
      buf.set(chunk, this._carry.length);
      this._carry = null;
    } else {
      buf = chunk;
    }
    const out: number[] = [];
    let i = 0;
    while (i < buf.length) {
      const b = buf[i];
      if (b !== 0x1b || i + 1 >= buf.length || buf[i + 1] !== 0x5b /* [ */) {
        // Lone ESC as the very last byte — carry it; it may start a CSI
        // sequence whose remainder is in the next frame.
        if (b === 0x1b && i + 1 === buf.length) {
          this._carry = buf.slice(i);
          break;
        }
        out.push(b);
        i++;
        continue;
      }
      // CSI sequence: ESC [ <params> <final byte 0x40–0x7e>
      let j = i + 2;
      while (j < buf.length && (buf[j] < 0x40 || buf[j] > 0x7e)) j++;
      if (j >= buf.length) {
        // Unterminated at chunk end. Carry it — unless it's implausibly
        // long for an SGR (garbage / DCS payload), then pass through raw.
        if (buf.length - i <= 64) {
          this._carry = buf.slice(i);
        } else {
          for (let k = i; k < buf.length; k++) out.push(buf[k]);
        }
        break;
      }
      if (buf[j] === 0x6d /* m */) {
        let params = '';
        for (let k = i + 2; k < j; k++) params += String.fromCharCode(buf[k]);
        const rewritten = this._rewriteSgr(params);
        if (rewritten !== params) {
          out.push(0x1b, 0x5b);
          for (let k = 0; k < rewritten.length; k++) out.push(rewritten.charCodeAt(k));
          out.push(0x6d);
          i = j + 1;
          continue;
        }
      }
      for (let k = i; k <= j; k++) out.push(buf[k]);
      i = j + 1;
    }
    return new Uint8Array(out);
  }

  flush(): Uint8Array | null {
    const c = this._carry;
    this._carry = null;
    return c;
  }

  private _rewriteSgr(params: string): string {
    // Sequences with no chip-open still matter while a foreground injection
    // is outstanding — the chip's text styling and CLOSE (49 / reset) arrive
    // as their own SGRs.
    if (!params.includes('48;2;') && !this._fgInjected) return params;
    const accent = this._getChipFg();
    const parts = params.split(';');
    const out: string[] = [];
    let changed = false;
    let i = 0;
    const pushAccentFg = () => {
      out.push('38', '2', String(accent![0]), String(accent![1]), String(accent![2]));
      changed = true;
    };
    while (i < parts.length) {
      const p = parts[i];
      // Truecolor intro consumes 5 params: (38|48|58);2;R;G;B — handled as a
      // unit so channel values are never misread as standalone codes.
      if ((p === '38' || p === '48' || p === '58') && parts[i + 1] === '2') {
        if (p === '48' && i + 4 < parts.length) {
          const r = Number(parts[i + 2]), g = Number(parts[i + 3]), b = Number(parts[i + 4]);
          const max = Math.max(r, g, b), min = Math.min(r, g, b);
          if (max >= 30 && max <= 80 && max - min <= 10) {
            // Chip opens: replace the background set with "default bg" and
            // start the accent foreground.
            out.push('49');
            changed = true;
            if (accent) {
              this._fgInjected = true;
              pushAccentFg();
            }
            i += 5;
            continue;
          }
        } else if (p === '38' && this._fgInjected && accent) {
          // Explicit truecolor foreground inside the chip — flatten to the
          // accent so the echo text doesn't render in its hard-coded color.
          pushAccentFg();
          i += 5;
          continue;
        }
        out.push(...parts.slice(i, i + 5));
        i += 5;
        continue;
      }
      // 256-color intro consumes 3 params: (38|48|58);5;N.
      if ((p === '38' || p === '48' || p === '58') && parts[i + 1] === '5') {
        if (p === '38' && this._fgInjected && accent) {
          pushAccentFg();
        } else {
          out.push(...parts.slice(i, i + 3));
        }
        i += 3;
        continue;
      }
      if (this._fgInjected) {
        // Chip close: SGR 49 (default bg) gets a default-fg chaser so the
        // accent never bleeds past the chip; a full reset ('0', or the empty
        // param a bare ESC[m means) clears the foreground on its own.
        if (p === '49') {
          out.push('49', '39');
          this._fgInjected = false;
          changed = true;
          i++;
          continue;
        }
        if (p === '0' || p === '') {
          this._fgInjected = false;
          out.push(p);
          i++;
          continue;
        }
        // Basic (30–37 / bright 90–97) or default (39) foreground inside the
        // chip — flatten to the accent like the truecolor case.
        const n = Number(p);
        if (accent && (p === '39' || (n >= 30 && n <= 37) || (n >= 90 && n <= 97))) {
          pushAccentFg();
          i++;
          continue;
        }
      }
      out.push(p);
      i++;
    }
    return changed ? out.join(';') : params;
  }
}

// Accent hex (#rrggbb) → [r, g, b] for the SGR foreground injection above.
// Null for anything else — the remapper then strips backgrounds without
// recoloring, the pre-2026-06-11 behavior.
const hexToRgb = (hex: string): [number, number, number] | null => {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
};

// ── output flow control ─────────────────────────────────────────────────────
// ACK granularity for terminal output. Must stay comfortably above the
// server's low watermark (50KB in routes_ws.py) so a single ack can drop the
// in-flight count below it, and below the high watermark (400KB) so acks fire
// well before the window fills. Counted in raw UTF-8 bytes of the output
// payload: binary frames count their byteLength (the server counts len() of
// the same bytes); legacy JSON frames count TextEncoder bytes of the data
// string (the server counts .encode('utf-8') of the identical string) — both
// ends agree exactly in both formats.
const FLOW_ACK_EVERY_BYTES = 100_000;
const utf8Encoder = new TextEncoder();

// ── live terminal title persistence ──────────────────────────────────────────
// How long a window title must hold steady before we persist it as the session's
// real name (renameSession). Claude Code rewrites the title every animation
// frame while it works (spinner glyph), so debouncing means only the SETTLED
// title — the conversation context it lands on, ✓ and all — is written to the
// backend; the rapid intermediate frames still render live in the tab.
const TITLE_PERSIST_DEBOUNCE_MS = 1500;

// Touch-primary device (phone/tablet), independent of which LAYOUT is
// rendered. A phone in landscape is wider than the mobile breakpoint and
// shows the DESKTOP layout by design (Nathan keeps it as an escape hatch,
// 2026-06-10) — but it still has no mouse and an on-screen keyboard, so
// input affordances (auto-focus, touch scrolling) key off this, not
// isMobile. Evaluated once: pointer capability doesn't change at runtime.
const IS_COARSE_POINTER =
  typeof window !== 'undefined' && window.matchMedia('(pointer: coarse)').matches;

// ── fling-trace capture ──────────────────────────────────────────────────────
// Always-on in-memory ring of touch-scroll telemetry (~400 short strings,
// negligible), inspectable as window.__flingTrace. Exists because momentum-
// scroll bugs only reproduce under a real finger on a real iPhone — the
// 2026-06-12 "fast fling stops dead" report took a synthetic-event simulation
// to even hypothesise about; this ring is the ground-truth channel for the
// next one. When the 'flingTrace' localStorage flag is set (open the dashboard
// once with ?flingtrace=1 to set it, ?flingtrace=0 to clear), each gesture
// also uploads the ring to .tmp/claude_uploads/ on the laptop through the
// existing file-upload endpoint, where a Claude session can read it.
const flingTrace: string[] = [];
const traceLine = (s: string) => {
  flingTrace.push(`${performance.now().toFixed(1)} ${s}`);
  if (flingTrace.length > 400) flingTrace.splice(0, flingTrace.length - 400);
};
if (typeof window !== 'undefined') {
  try {
    (window as unknown as Record<string, unknown>).__flingTrace = flingTrace;
    const q = new URLSearchParams(window.location.search).get('flingtrace');
    if (q === '1') window.localStorage.setItem('flingTrace', '1');
    else if (q === '0') window.localStorage.removeItem('flingTrace');
  } catch { /* private mode — ring stays memory-only */ }
}
let traceUploadTimer: number | null = null;
const maybeUploadTrace = () => {
  try {
    if (window.localStorage.getItem('flingTrace') !== '1') return;
  } catch { return; }
  // Debounced: a gesture's settle/momentum-end calls this once; rapid
  // consecutive flicks collapse into one upload carrying all of them.
  if (traceUploadTimer !== null) window.clearTimeout(traceUploadTimer);
  traceUploadTimer = window.setTimeout(() => {
    traceUploadTimer = null;
    const name = `fling-trace-${new Date().toISOString().replace(/[:.]/g, '-')}.txt`;
    uploadClaudeFile(new File([flingTrace.join('\n')], name, { type: 'text/plain' }))
      .catch(() => { /* debug channel only — never surface */ });
  }, 1500);
};

type WsStatus = 'connecting' | 'open' | 'closed';

type Bundle = {
  term: Terminal;
  fit: FitAddon;
  ws: WebSocket | null;
  status: WsStatus;
  // Session is alive on the backend (false once we receive {type:'exit'} or
  // a 4404 close on connect). Dead bundles stay in the map so the user can
  // see the prior output until they close the tab.
  alive: boolean;
  // Backoff for reconnect attempts after unexpected drops.
  reconnectMs: number;
  reconnectTimer: number | null;
  // Callback to nudge React to re-render when status changes.
  onStatusChange: () => void;
  // Cleanup for the touch-scroll handlers we attached to the host element.
  detachTouch: (() => void) | null;
  // GPU renderer addon, when active. Null means we're on the DOM renderer
  // (WebGL construction failed, or the GPU context was lost at runtime).
  webgl: WebglAddon | null;
  // Set by the ↻ refresh so the NEXT connect() claims the resize lock and
  // sizes the shared PTY to this device (dual-purpose refresh). One-shot:
  // connect() reads it then clears, so plain reconnects (network blips) don't
  // steal the lock — only an explicit ↻ does.
  pendingTakeover: boolean;
};

export function ClaudeTerminalCard({ theme, accent, themeSettings, onThemeChange, isMobile = false, isActive = true }: Props) {
  const [sessions, setSessions] = useState<TerminalSession[]>([]);
  // The Agent Manager tab (leftmost in the strip) is a non-terminal view; when
  // true the host area is hidden and AgentManagerPane is shown full-width. A
  // session-tab click clears it. Never persisted — AGENTS is the Claude tab's
  // home, so this defaults to true (Nathan, 2026-06-25).
  const [showAgents, setShowAgents] = useState(true);
  // "An agent is alive" treatment for the ≡ AGENTS tab (Nathan, 2026-06-24): a
  // steady accent glow while a run is in flight, plus a one-shot flash each time
  // the manager checks for work. `active` drives the glow; `pulseKey` advances on
  // a real scheduler tick. We poll only while this card is on screen (isActive).
  const { active: agentActive, pulseKey: agentPulseKey, online: agentOnline } = useAgentPulse(isActive);
  // True for ~720ms right after a tick advance, so the button swaps the one-shot
  // flash animation in then reverts to the breathing glow.
  const [agentPulsing, setAgentPulsing] = useState(false);
  useEffect(() => {
    if (agentPulseKey === 0) return;  // 0 = initial; no flash on mount
    setAgentPulsing(true);
    const t = window.setTimeout(() => setAgentPulsing(false), 720);
    return () => window.clearTimeout(t);
  }, [agentPulseKey]);
  // Glow vars + animation for the AGENTS tab. The three accent-with-alpha vars
  // feed the agentGlowBreath / agentTickPulse keyframes (dashboard.css). Empty
  // when idle so the tab keeps its plain look with zero overhead.
  const agentGlowStyle: React.CSSProperties = agentActive
    ? {
        ['--agent-glow-soft' as string]: `${accent}55`,
        ['--agent-glow' as string]: `${accent}aa`,
        ['--agent-glow-bright' as string]: accent,
        animation: agentPulsing
          ? 'agentTickPulse 0.7s ease-out'
          : 'agentGlowBreath 2.4s ease-in-out infinite',
        borderRadius: 3,
      } as React.CSSProperties
    : {};
  // Status dot for the AGENTS tab, mirroring the session tabs' connection pips:
  // green = manager reachable, amber = first pulse pending, red = unreachable
  // (down or mid-respawn). Pulse polling only runs while this card is on screen.
  const agentDotColor =
    agentOnline === false ? theme.red :
    agentOnline === true  ? theme.green :
    theme.amber;
  // Only the operator's own shells are surfaced in the Claude tab. Agent runs
  // are NOT shown as tabs here (design 2026-06-21): the running-agent COUNT
  // lives in the sidebar/bottom-bar badge (useAgentBadge), and the full run
  // detail + feeds live in the ≡ AGENTS manager pane. So agent-tagged pty
  // sessions are filtered out of the strip entirely.
  const normalSessions = useMemo(() => sessions.filter(s => !isAgentSession(s)), [sessions]);
  // ── pane model ────────────────────────────────────────────────────────────
  // paneIds[i] is the session shown in pane i (null = empty pane awaiting an
  // assignment); focusedPane is the slot tab-clicks load into. activeId — the
  // id every effect and input path keys off — is DERIVED: the focused pane's
  // session (falling back to any occupied pane so a focused-but-empty pane
  // never strands keyboard input). Mobile ignores the split and renders only
  // activeId; layoutCount is forced to 1 there at init.
  const [layoutCount, setLayoutCount] = useState<LayoutCount>(() => {
    if (isMobile) return 1;
    try {
      const v = parseInt(localStorage.getItem(LAYOUT_KEY) ?? '1', 10);
      return v === 2 || v === 3 ? v : 1;
    } catch { return 1; }
  });
  const [paneIds, setPaneIds] = useState<(string | null)[]>(
    () => Array(layoutCount).fill(null),
  );
  const [focusedPane, setFocusedPane] = useState(0);
  const activeId = paneIds[focusedPane] ?? paneIds.find(id => id !== null) ?? null;
  // The session ids actually on screen right now — what the refit effects
  // iterate. Memoized so effect deps only fire on real changes.
  const visibleIds = useMemo(
    () => (isMobile ? [activeId] : paneIds).filter((x): x is string => x !== null),
    [isMobile, activeId, paneIds],
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // In-place tab rename: which session id is being edited, and the current
  // input value. Null editingId means no rename is in progress.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editLabel, setEditLabel] = useState('');

  // ── live terminal title → tab label ─────────────────────────────────────────
  // Claude Code (and anything that sets the window title via an OSC 0/2 escape)
  // drives the PowerShell tab title to the live conversation context — a spinner
  // glyph while it works, a ✓ when it's done. xterm parses those OSC sequences
  // and fires term.onTitleChange (wired up in ensureBundle); we mirror the
  // latest title into the tab here so the dashboard shows the same live-updating
  // name as a native terminal. liveTitles[id] takes precedence over the stored
  // label for display; a settled title is debounce-persisted as the real session
  // name so it survives a reload (the OSC title is NOT part of the screen-state
  // replay, so without persistence an idle session would lose its name).
  const [liveTitles, setLiveTitles] = useState<Record<string, string>>({});
  // Sessions the operator manually renamed — auto-title stops overwriting them.
  // Page-lifetime only: a reload starts every session back in auto mode.
  const pinnedTitlesRef = useRef<Set<string>>(new Set());
  // Per-session debounce timers for persisting a settled title to the backend.
  const titlePersistTimersRef = useRef<Map<string, number>>(new Map());
  // Double-tap-to-close safety: first tap on × sets this to the session id and
  // arms a timeout; second tap on the same × (or any tap after timeout) actually
  // closes. Tapping a different tab's × resets to that tab. Clicking anywhere
  // else cancels. Prevents accidental tab loss from bumping the close button.
  const [pendingCloseId, setPendingCloseId] = useState<string | null>(null);
  const pendingCloseTimerRef = useRef<number | null>(null);
  // Ticks whenever a per-bundle status flips, so the active-tab status pip
  // re-renders without forcing the whole sessions list refresh.
  const [, forceRender] = useState(0);
  const bumpRender = useCallback(() => forceRender(n => n + 1), []);

  // Mobile chat-box state. The contenteditable div is the focus target that
  // summons the iOS keyboard; the terminal itself never gets focus on mobile.
  // We use contenteditable instead of <textarea> because iOS shows a
  // form-assistant bar (^/v/✓) above the keyboard whenever a textarea or
  // input has focus — that bar was eating ~50px of vertical real-estate and
  // pushing the chat group up off the keyboard. Contenteditable elements
  // aren't classified as form fields, so the bar stays hidden.
  const [chatText, setChatText] = useState('');
  const chatInputRef = useRef<HTMLDivElement>(null);
  // The portaled mobile chat group container (input + tool row). Reffed so a
  // native touchmove blocker can be attached — see the drag-pan effect below.
  const chatGroupRef = useRef<HTMLDivElement>(null);
  // Image-attach flow (mobile). Tap + → hidden file input opens iOS's photo
  // picker (camera roll + take-photo + choose-file). On pick we upload the
  // file, get back its absolute path on the laptop, and paste that path into
  // the chat box so the user can send it to Claude Code (which reads images
  // off disk via its Read tool — pasted bytes don't traverse the PTY cleanly
  // on Windows). uploadStatus drives a small toast above the chat box;
  // uploadError holds the most recent error text.
  const fileInputRef = useRef<HTMLInputElement>(null);
  type UploadStatus = 'idle' | 'uploading' | 'done' | 'error';
  const [uploadStatus, setUploadStatus] = useState<UploadStatus>('idle');
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Desktop drag-and-drop: true while a file is being dragged over the card.
  // dragDepthRef counts enter/leave pairs because dragleave fires every time
  // the pointer crosses a child element boundary — a bare boolean flickers.
  const [dragOver, setDragOver] = useState(false);
  const dragDepthRef = useRef(0);
  // Lets the term.onData closure (created once per terminal, never re-bound)
  // reach the CURRENT upload routine for Ctrl+V image paste without going
  // stale. Assigned after uploadFiles is defined below.
  const uploadFilesRef = useRef<(files: (File | Blob)[]) => void>(() => {});

  // Drives the chat group's `bottom` CSS. iOS 26 Safari exposes no CSS
  // mechanism for keyboard tracking; we read `window.visualViewport` and lift
  // fixed-bottom elements by the measured keyboard height ourselves. See
  // docs/claude/synth/2026-05-06_ios26-safari-keyboard-reference.md.
  //
  // Layout (bottom-up):
  //   - Keyboard down: chat sits 30px + safe-area-inset above the tab bar.
  //   - Keyboard up:   the BottomTabBar hides itself entirely (see
  //     useChatInputFocused) so the chat sits flush on top of the keyboard,
  //     no tab-bar gap to clear.
  //   - Tap-to-type detected but keyboard not yet measured: PRE-LIFT to the
  //     last-known keyboard height. This is load-bearing, not cosmetic —
  //     iOS 26 standalone pushes the whole app frame up when the focused
  //     element sits where the keyboard will land, then botches the frame
  //     restore on dismissal, leaving the layout viewport shrunk by the top
  //     safe-area inset and every fixed-bottom element floating (the
  //     "unpinned bottom bar" bug, 2026-06-10). Lifting the box ABOVE the
  //     keyboard's landing zone before the keyboard animates means iOS never
  //     needs to push the frame, so there is nothing to mis-restore. The
  //     tab-flip rebuild in DashboardApp remains as a backstop cure.
  //
  // preLift is armed ONLY by the pointerdown handler on the input (a real
  // finger tap that will summon the keyboard) — NOT by focus state. The
  // input also gets focused programmatically (post-send refocus, image-
  // attach append, the blur guard) and none of those raise a keyboard, so
  // keying the lift off focus floated the box mid-screen above nothing
  // (Nathan's ↵-with-keyboard-down report, 2026-06-10). Disarmed by the
  // keyboard actually arriving (kbdPx takes over), by blur, or by timeout.
  const kbdPx = useKeyboardHeightPx();
  const [preLift, setPreLift] = useState(false);
  useEffect(() => {
    if (kbdPx > 100) {
      try { localStorage.setItem(LAST_KBD_PX_KEY, String(Math.round(kbdPx))); }
      catch { /* private mode — pre-lift falls back to the default guess */ }
    }
    if (kbdPx > 0) setPreLift(false);
  }, [kbdPx]);
  const chatBottom = kbdPx > 0
    ? `${kbdPx}px`
    // +16 cushion matches the synchronous pointerdown pre-lift below — the
    // two must agree or this re-render would drop the box back into the
    // keyboard's landing zone mid-animation, re-triggering the frame push.
    : preLift
    ? `${readLastKbdPx() + 16}px`
    : `calc(${BOTTOM_TAB_BAR_HEIGHT_CSS} + 30px + env(safe-area-inset-bottom, 0px))`;

  // The mobile chat group is portaled only while the tab is active, so the
  // stuck-frame tab-flip detour in DashboardApp unmounts and remounts it —
  // destroying the contenteditable's DOM text while chatText state survives
  // (this card itself stays mounted, just display:none). Rehydrate the div
  // from state on remount so a half-typed draft never vanishes. chatText is
  // deliberately NOT a dependency: while the user is typing, DOM and state
  // are already in sync, and rewriting textContent on every keystroke would
  // throw the caret back to the start.
  useEffect(() => {
    if (!isMobile || !isActive) return;
    const el = chatInputRef.current as unknown as HTMLDivElement | null;
    if (el && (el.textContent ?? '') !== chatText) el.textContent = chatText;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile, isActive]);

  // Drag-pan blocker. While the keyboard is up, iOS 26 standalone lets a
  // drag on any non-scrolling surface grab the pushed-up app frame and pan
  // it — and even a 1px pan re-breaks the frame's restore on dismissal
  // (Nathan, 2026-06-10). The chat group is exactly such a surface, and the
  // one your finger is nearest while typing. Kill pans that start on it.
  // Native listener because React's synthetic touch handlers are passive —
  // preventDefault would be ignored. One carve-out: a long multi-line draft
  // makes the contenteditable itself scrollable; let those drags through so
  // the draft can still be scrolled.
  useEffect(() => {
    if (!isMobile || !isActive) return;
    const group = chatGroupRef.current;
    if (!group) return;
    const onTouchMove = (ev: TouchEvent) => {
      const input = chatInputRef.current as unknown as HTMLDivElement | null;
      if (
        input &&
        ev.target instanceof Node &&
        input.contains(ev.target) &&
        input.scrollHeight > input.clientHeight + 1
      ) return;
      ev.preventDefault();
    };
    group.addEventListener('touchmove', onTouchMove, { passive: false });
    return () => group.removeEventListener('touchmove', onTouchMove);
  }, [isMobile, isActive]);

  const bundlesRef = useRef<Map<string, Bundle>>(new Map());
  const hostRef = useRef<HTMLDivElement>(null);
  // Sessions in this set get the selected launch command auto-typed on
  // their first ws.onopen (no trailing \r — the user presses Enter to run
  // it or backspaces to type something else). Add a session id when we
  // create it (handleNewTab + the initial empty-state auto-create); remove
  // it once the bootstrap fires. Reconnects skip it because the id is no
  // longer in the set.
  const pendingBootstrapRef = useRef<Set<string>>(new Set());

  // Which launch command gets auto-typed when a new session connects.
  // Persisted in localStorage so the choice survives page reloads.
  const BOOTSTRAP_COMMANDS = [
    { value: 'claude',       label: 'Claude' },
    { value: 'claude-ds',    label: 'DeepSeek' },
    { value: 'claude-go',    label: 'Go' },
    { value: 'claude-ds-go', label: 'DS Go' },
  ] as const;
  const [bootstrapCmd, setBootstrapCmd] = useState<string>(() => {
    try { return localStorage.getItem('claudeBootstrapCmd') || 'claude-ds'; }
    catch { return 'claude-ds'; }
  });
  // Ref mirror so the ws.onopen closure always reads the latest value
  // without retriggering ensureBundle (which is keyed by session id and
  // should only run once).
  const bootstrapCmdRef = useRef(bootstrapCmd);
  bootstrapCmdRef.current = bootstrapCmd;

  // Which font the terminal text renders in. Persisted like the launch
  // command; ref-mirrored for the same reason (ensureBundle reads it at
  // construction time without being re-keyed on it).
  const [termFont, setTermFont] = useState<string>(() => {
    try { return localStorage.getItem('claudeTermFont') || 'jetbrains'; }
    catch { return 'jetbrains'; }
  });
  const termFontRef = useRef(termFont);
  termFontRef.current = termFont;

  // Terminal font size. Persisted and ref-mirrored like the font family;
  // ensureBundle reads the ref at construction time without being re-keyed on
  // it. Shared by desktop and mobile (the dropdown is exposed on both). When
  // nothing has been chosen, mobile starts at its tuned 12px and desktop at 13.
  const [termFontSize, setTermFontSize] = useState<number>(() => {
    try {
      const stored = localStorage.getItem('claudeTermFontSize');
      if (stored) return clampFontSize(parseInt(stored, 10));
    } catch { /* private mode */ }
    return isMobile ? 12 : DEFAULT_FONT_SIZE;
  });
  const termFontSizeRef = useRef(termFontSize);
  termFontSizeRef.current = termFontSize;

  // Accent as RGB for the SgrBgRemapper's foreground injection (sent-message
  // recolor). Ref-mirrored so the per-connection remapper — constructed
  // inside ensureBundle's connect closure, which is never re-keyed — reads
  // the CURRENT accent: switch accents in Settings and messages echoed from
  // then on pick up the new color (existing scrollback keeps the old one
  // until a reconnect replays it).
  const accentRgbRef = useRef<[number, number, number] | null>(hexToRgb(accent));
  accentRgbRef.current = hexToRgb(accent);

  // ── theme tokens for xterm ─────────────────────────────────────────────────
  // Set at construction AND pushed into live terminals when the dashboard
  // theme changes (see the options.theme effect below) — cells store ANSI
  // color indices, so a repaint recolors existing scrollback too. Without the
  // live push, flipping Dark→Light left cream terminal text on a cream page.
  const isLight = theme.name === 'Light';
  const xtermTheme = useMemo(() => ({
    // Transparent so the card bg shows through — but with theme.bg0's RGB
    // channels, NOT rgba(0,0,0,0). The WebGL renderer draws a background
    // rectangle for any cell whose packed bg attributes are non-zero, and
    // DIM lives in those bits — so dim text (Claude Code's input-bar hint,
    // suggestion lists) gets a rectangle painted in the theme background's
    // RGB with alpha FORCED to 1 (RectangleRenderer._updateRectangle).
    // Transparent black therefore rendered as opaque black boxes behind all
    // dim text (Nathan, 2026-06-10). With bg0's channels the buggy rectangle
    // comes out as the page's own base color — invisible on both themes —
    // while the alpha-0 viewport clear keeps the rest see-through.
    background: `${theme.bg0}00`,
    foreground: theme.text,
    cursor: accent,
    cursorAccent: theme.bg0,
    selectionBackground: `${accent}55`,
    black: '#1f1d1a',
    red: theme.red,
    green: theme.green,
    yellow: theme.amber,
    blue: theme.blue,
    // Magenta/cyan have no dashboard token — pastels tuned for the dark bg,
    // deepened variants for Light so they keep contrast on cream.
    magenta: isLight ? '#8a4ea8' : '#c79bd9',
    cyan: isLight ? '#2c7f99' : '#86c5d9',
    white: theme.text,
    brightBlack: theme.text3,
    brightRed: theme.red,
    brightGreen: theme.green,
    brightYellow: theme.amber,
    brightBlue: theme.blue,
    brightMagenta: isLight ? '#9a62b8' : '#d9b3e6',
    brightCyan: isLight ? '#3a92ad' : '#a3d9e6',
    brightWhite: theme.text,
  }), [theme, accent, isLight]);

  // Live-restyle every open terminal when the theme (or accent) changes.
  // options.theme is xterm's documented runtime setter — it repaints in
  // place, so existing scrollback flips palette along with new output. The
  // one exception is literal-RGB text injected by the SgrBgRemapper (sent
  // messages): those cells carry baked RGB and keep their color until a
  // reconnect replays them — same accepted behavior as accent switching.
  useEffect(() => {
    bundlesRef.current.forEach(b => {
      try {
        b.term.options.theme = xtermTheme;
        b.term.refresh(0, b.term.rows - 1);
      } catch { /* term may already be disposed */ }
    });
  }, [xtermTheme]);

  // ── load sessions on mount; auto-create Session 1 if none exist ────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await listSessions();
        if (cancelled) return;
        // Auto-create / pane-fill key off the operator's OWN shells — agent-run
        // sessions (tagged) shouldn't suppress the default shell or claim a pane
        // on load.
        // Agent-run sessions are never surfaced here (see normalSessions note),
        // so only the operator's own shells go into state.
        const ownShells = list.filter(s => !isAgentSession(s));
        if (ownShells.length === 0) {
          const s = await createSession();
          if (cancelled) return;
          // Brand-new session — auto-`claude-ds` once connected (no Enter).
          pendingBootstrapRef.current.add(s.id);
          setSessions([s]);
          setPaneIds(prev => prev.map((_, i) => (i === 0 ? s.id : null)));
        } else {
          // Existing sessions are reattaches — don't bootstrap them; the
          // user may already be mid-conversation. Fill panes from the operator's
          // own shells in order (a restored 2/3-pane layout comes back populated).
          setSessions(ownShells);
          setPaneIds(prev => prev.map((_, i) => ownShells[i]?.id ?? null));
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => { cancelled = true; };
  }, []);


  // Ref mirrors so the title-persist debounce (which fires outside render) reads
  // the CURRENT label / edit state without the callbacks being re-created.
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const editingIdRef = useRef(editingId);
  editingIdRef.current = editingId;

  // Write a settled window title back as the session's real name. Skips pinned
  // (manually-renamed) tabs, a tab being actively edited, and no-op renames.
  const persistTitle = useCallback((id: string, title: string) => {
    if (pinnedTitlesRef.current.has(id)) return;
    if (editingIdRef.current === id) return;
    const cur = sessionsRef.current.find(s => s.id === id);
    if (!cur || cur.label === title) return;
    setSessions(prev => prev.map(s => s.id === id ? { ...s, label: title } : s));
    // Non-fatal: the live title still shows even if the backend write fails.
    renameSession(id, title).catch(() => { /* ignore — display already updated */ });
  }, []);

  // term.onTitleChange handler: show the new title live, then debounce-persist
  // it once it stops changing. The backend caps labels at 64 chars; mirror that
  // here so a runaway title can't desync the optimistic label from what sticks.
  const handleTermTitle = useCallback((id: string, rawTitle: string) => {
    if (pinnedTitlesRef.current.has(id)) return;
    const title = rawTitle.trim().slice(0, 64);
    if (!title) return;
    setLiveTitles(prev => (prev[id] === title ? prev : { ...prev, [id]: title }));
    const timers = titlePersistTimersRef.current;
    const existing = timers.get(id);
    if (existing) window.clearTimeout(existing);
    timers.set(id, window.setTimeout(() => {
      timers.delete(id);
      persistTitle(id, title);
    }, TITLE_PERSIST_DEBOUNCE_MS));
  }, [persistTitle]);

  // ── xterm + WS lifecycle: ensure a bundle exists for each session ──────────
  // Called via ref callback when each per-session container element mounts.
  // Idempotent — once a bundle is created for a session id, never recreate it
  // until the user explicitly closes the tab.
  const ensureBundle = useCallback((id: string, el: HTMLDivElement | null) => {
    if (!el) return;
    if (bundlesRef.current.has(id)) return;

    // The user-selected terminal font, read at construction time. On mobile
    // we list Menlo (the iOS native monospace, consistent block-glyph
    // metrics) ahead of the web font so that the initial glyph-width
    // measurement — which xterm does once, immediately — happens against a
    // font that's already on the device; web fonts are fetched async and may
    // not be ready yet. Once the real font loads, we re-set fontFamily to
    // trigger a re-measurement (see the ensureFontLoaded handler below).
    const fontChoice = findFontChoice(termFontRef.current);
    const finalFontFamily = fontFamilyFor(fontChoice);

    const term = new Terminal({
      fontFamily: isMobile && fontChoice.gfFamily
        ? `Menlo, ${finalFontFamily}`
        : finalFontFamily,
      fontSize: termFontSizeRef.current,
      // Block-character art (Claude Code's logo, box-drawing UIs) needs
      // tight line-height to stack flush. 1.2 leaves a visible gap between
      // rows that mangles the logo on mobile; 1.0 stacks blocks correctly
      // and looks identical for normal text.
      lineHeight: isMobile ? 1.0 : 1.2,
      cursorBlink: true,
      cursorStyle: 'block',
      // 5000 lines is fine even on mobile: both the WebGL renderer and the
      // DOM fallback paint only the visible rows per frame, so scrollback
      // depth costs memory (circular buffer), not render time. Reconsidered
      // and kept during the Phase 3 polish wave (2026-06-10).
      scrollback: 5000,
      allowProposedApi: false,
      // allowTransparency lets xterm's renderer skip its solid background
      // fill, so theme.bg1Chart + the constellation behind the card show
      // through — same look as the bot-log terminal.
      allowTransparency: true,
      theme: xtermTheme,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    try { fit.fit(); } catch { /* container not sized yet — first ResizeObserver tick will fix */ }

    // Input guards on xterm's hidden textarea — mobile keyboards otherwise
    // autocapitalize/autocorrect keystrokes routed through it, silently
    // rewriting shell input (`Git` for `git`, curly quotes, etc.). Harmless
    // no-ops on desktop. term.textarea is only set once open() has run.
    const ta = term.textarea;
    if (ta) {
      ta.setAttribute('autocapitalize', 'off');
      ta.setAttribute('autocorrect', 'off');
      ta.setAttribute('autocomplete', 'off');
      ta.setAttribute('spellcheck', 'false');
    }

    // Pull xterm's hidden helper textarea out of iOS's "next form field"
    // navigation AND out of layout entirely. Without this, iOS sees a real
    // <textarea> in the DOM and pins the QuickType form-assistant bar (^/v/✓)
    // to the top of the keyboard whenever the chat input is focused — even
    // though the chat input itself is contenteditable, not a form field.
    // On mobile we never type into xterm directly (the chat box is the input
    // source, sent over WS), so display:none is safe; tabIndex=-1 + aria-hidden
    // are belt-and-braces in case any iOS version still scans hidden inputs.
    if (isMobile) {
      const helperTa = el.querySelector('.xterm-helper-textarea') as HTMLTextAreaElement | null;
      if (helperTa) {
        helperTa.tabIndex = -1;
        helperTa.setAttribute('aria-hidden', 'true');
        helperTa.style.display = 'none';
      }
    }

    // The Claude Code banner is block-drawing art (▀▄█▌) — it only stacks
    // correctly when xterm measures glyphs against the real selected font.
    // xterm measures once at construction; if the web font hasn't downloaded
    // yet, all measurements use the fallback font's metrics and the logo
    // stays mangled even after the font finally swaps in.
    //
    // ensureFontLoaded resolves once the face is actually rendered-ready
    // (document.fonts.load — more reliable on iOS than fonts.ready, which
    // can settle before web fonts have been fetched). Re-setting fontFamily
    // on a value that differs from the current one is xterm's documented
    // re-measurement trigger; for the system choice it's a no-op.
    ensureFontLoaded(fontChoice).then(() => {
      try {
        term.options.fontFamily = finalFontFamily;
        fit.fit();
        term.refresh(0, term.rows - 1);
      } catch { /* term may already be disposed */ }
    });

    const bundle: Bundle = {
      term,
      fit,
      ws: null,
      status: 'connecting',
      alive: true,
      reconnectMs: 500,
      reconnectTimer: null,
      onStatusChange: bumpRender,
      detachTouch: null,
      webgl: null,
      pendingTakeover: false,
    };
    bundlesRef.current.set(id, bundle);

    // ── WebGL renderer ───────────────────────────────────────────────────────
    // Without a renderer addon, xterm 6 uses its DOM renderer — the slowest
    // path and the suspected source of janky scrolling (and possibly the iOS
    // glyph jumbling; WebGL draws from its own texture atlas, a completely
    // different code path from DOM glyph measurement). This is VS Code's
    // production renderer. If the GPU context can't be created (blocklisted
    // driver, headless, ancient device) we just stay on the DOM renderer —
    // identical output, slower paint.
    try {
      const webgl = new WebglAddon();
      webgl.onContextLoss(() => {
        // GPU contexts are a finite browser resource — iOS Safari reclaims
        // them under memory pressure. Dispose the addon so xterm falls back
        // to the DOM renderer instead of leaving a frozen/blank canvas.
        console.warn('xterm WebGL context lost — falling back to DOM renderer');
        try { webgl.dispose(); } catch { /* already disposed */ }
        bundle.webgl = null;
      });
      term.loadAddon(webgl);
      bundle.webgl = webgl;
    } catch (e) {
      console.warn('xterm WebGL renderer unavailable — using DOM renderer', e);
    }

    // Pipe user keystrokes to the WS.
    // Desktop: intercept Ctrl+V (\x16) and paste clipboard text instead.
    // xterm forwards the raw byte to the shell — nothing happens. Right-click
    // paste works because the browser handles that at the OS level. We catch
    // it here and inject clipboard contents through the same WS path.
    // Newlines are sent as \n (no \r) so multi-line text lands in the input
    // buffer without auto-submitting — user reviews then presses Enter.
    term.onData(data => {
      if (!isMobile && data === '\x16') {
        // Prefer a screenshot/image on the clipboard: upload it and type its
        // path (via uploadFilesRef — the live routine, this closure is bound
        // once per terminal). clipboard.read() prompts Chrome's clipboard
        // permission the first time, then remembers. Text clipboards fall
        // through to the original readText() paste.
        void (async () => {
          try {
            const items = await navigator.clipboard.read();
            for (const item of items) {
              const imgType = item.types.find(t => t.startsWith('image/'));
              if (imgType) {
                const blob = await item.getType(imgType);
                uploadFilesRef.current([blob]);
                return;
              }
            }
          } catch { /* rich clipboard read unavailable — try plain text */ }
          try {
            const text = await navigator.clipboard.readText();
            const ws = bundle.ws;
            if (ws && ws.readyState === WebSocket.OPEN && text) {
              ws.send(JSON.stringify({ type: 'in', data: text }));
            }
          } catch { /* clipboard empty or permission denied — silent noop */ }
        })();
        return;
      }
      const ws = bundle.ws;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'in', data }));
      }
    });

    // Pipe local resizes to the WS so the shell knows new geometry.
    term.onResize(({ rows, cols }) => {
      const ws = bundle.ws;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', rows, cols }));
      }
    });

    // Mirror the live window title (OSC 0/2 — what Claude Code drives to the
    // conversation context + status glyph) into this session's tab label.
    term.onTitleChange(title => handleTermTitle(id, title));

    // ── momentum touch-scroll ────────────────────────────────────────────────
    // xterm's default touch handling intercepts swipes for selection and the
    // viewport's native scrolling doesn't kick in because .xterm-screen sits
    // above .xterm-viewport in the stacking order — so a touch on the visible
    // terminal area never reaches the viewport's native scroll handler.
    //
    // Custom handler: track touchstart Y, scroll the buffer by whole cell
    // rows via term.scrollLines() (the public API — xterm 6 rounds every
    // scroll position to a row internally, so rows are the finest the BUFFER
    // can move), and carry the sub-row pixel remainder as a translateY on
    // .xterm-screen. The transform is what makes content track the finger
    // pixel-for-pixel instead of stepping a row (~12px) at a time — the
    // "not smooth on iOS" complaint (Nathan, 2026-06-10). preventDefault
    // stops xterm's selection logic and any native scroll the browser might
    // try. On touchend a requestAnimationFrame loop decays the last velocity
    // along iOS's own deceleration curve for native-feeling momentum, then
    // settles the fraction back onto a row boundary.
    //
    // Still deliberate (Phase 3 review, 2026-06-10):
    //   - xterm's smoothScrollDuration stays 0: it animates scrollLines()
    //     calls, and the drag/momentum paths already emit one scroll per
    //     frame — easing on top would queue overlapping animations.
    //   - Follow-tail discipline (re-anchor to bottom only when already at
    //     bottom) is xterm's own buffer behavior and scrollLines() is the
    //     public API, so the handler doesn't fight it. The transform is
    //     zeroed whenever scrolling rests, so tail-following output never
    //     renders half-shifted.
    // Attached for the mobile layout AND for touch devices showing the
    // desktop layout (phone in landscape) — without it, swipes there fall
    // into xterm's selection logic and the terminal doesn't scroll at all.
    if (isMobile || IS_COARSE_POINTER) {
      // Disable the browser's own touch handling on the host element so our
      // capture-mode preventDefault() truly wins. Without this, in some iOS
      // builds the browser starts a native pan animation on touchstart which
      // races our handler and produces no visible scroll.
      el.style.touchAction = 'none';

      let lastY = 0;
      let momentumRaf: number | null = null;
      let touchStartY = 0;
      let touchMoved = false;
      // Release-velocity samples: (y, t) pairs from the last ~120ms of the
      // gesture, UIKit-style. Two generations of estimator bugs live here:
      //   1. The original EMA started at zero and needed 4–5 touchmoves to
      //      converge — a violent flick delivers only 2–3, so hard throws
      //      released at a fraction of their real speed and stopped dead
      //      (Nathan, 2026-06-10). Replaced with the windowed average.
      //   2. The windowed average alone STILL died on hard throws: WebKit
      //      coalesces/starves touchmove delivery exactly when the main
      //      thread is busiest — and a fast fling scrolling several rows of
      //      WebGL terminal per event is peak busy — so the samples covered
      //      only the slow start of the gesture (or none of it) and the
      //      computed velocity landed under the momentum cutoff: instant
      //      stop on release (Nathan, 2026-06-12). Three-part fix: sample
      //      at EVENT time (e.timeStamp — handler-run time drifts under
      //      load); append the touchend's changedTouches position as a
      //      final sample so travel the move stream never delivered still
      //      counts; and blend in the last-segment velocity (see
      //      onTouchEnd) so a short jab isn't diluted by its own slow
      //      first samples.
      let samples: { y: number; t: number }[] = [];
      const VELOCITY_WINDOW_MS = 120;
      // Event-time reader. e.timeStamp shares performance.now()'s timebase
      // in every engine we target; the fallback covers synthetic events
      // that carry timeStamp 0.
      const evTime = (e: TouchEvent) => e.timeStamp || performance.now();
      // Drag deltas accumulate here and apply once per animation frame.
      // Keeping the touchmove handler itself near-free is part of the fix
      // for estimator bug #2 above: heavy per-event work is what made
      // WebKit starve the stream in the first place. (The screen only
      // paints once per frame regardless, so coalescing costs nothing
      // visually.)
      let pendingDragPx = 0;
      let dragRaf: number | null = null;
      // Visual scroll offset in px that hasn't crossed a whole cell row yet.
      // Rendered as translateY(-pxAccum) on the screen element; always
      // brought back to 0 when scrolling comes to rest (see settle()).
      let pxAccum = 0;
      let settleTimer: number | null = null;
      let settleRaf: number | null = null;

      // The element the renderer paints into (WebGL canvas / DOM rows plus
      // selection + cursor overlays — they all shift together). Present from
      // term.open(); lives as long as the terminal does.
      const screenEl = el.querySelector('.xterm-screen') as HTMLElement | null;

      // Cell height in CSS px. The renderer sizes .xterm-screen to exactly
      // rows × cellHeight, making it the reliable measure under xterm 6
      // (where .xterm-viewport became a custom scrollable element — kept
      // only as a fallback probe). Public API: term.rows + clientHeight.
      const getCellHeight = (): number => {
        const probe = screenEl ?? (el.querySelector('.xterm-viewport') as HTMLElement | null);
        if (probe && term.rows > 0) {
          const ch = probe.clientHeight / term.rows;
          if (ch > 0 && Number.isFinite(ch)) return ch;
        }
        return 14;  // safe fallback ≈ 12px font × 1.0 lineHeight + slack
      };

      const applyFrac = () => {
        if (!screenEl) return;
        screenEl.style.transform = pxAccum !== 0 ? `translateY(${-pxAccum}px)` : '';
      };

      // All steady-state transform updates go through here, NOT applyFrac
      // directly. xterm repaints its grid asynchronously (its render service
      // schedules on its own requestAnimationFrame), so a synchronous
      // transform reset at a row crossing painted before the grid shifted —
      // popping the content a full row down and back up at every crossing
      // (Nathan's "lines popping up and down" report, 2026-06-10).
      //
      // Two-tier sync:
      //   - No row crossing (pure sub-row drag): rAF-coalesced apply. The
      //     grid isn't moving, so the only constraint is one update per frame.
      //   - Row crossing (scrollLines fired): hold the transform until
      //     term.onRender says the shifted grid has actually been drawn —
      //     ground truth, immune to assumptions about xterm's internal
      //     scheduling ("still kinda stuttery" report, 2026-06-10). A 50ms
      //     timeout backstops the rare crossing that produces no render.
      let fracRaf: number | null = null;
      let crossingPending = false;
      let crossingFallback: number | null = null;
      const scheduleFrac = () => {
        // A pending crossing owns the next transform write (onRender applies
        // the latest pxAccum); writing earlier would paint ahead of the grid.
        if (fracRaf !== null || crossingPending || !screenEl) return;
        fracRaf = requestAnimationFrame(() => {
          fracRaf = null;
          if (!crossingPending) applyFrac();
        });
      };
      const releaseCrossing = () => {
        if (!crossingPending) return;
        crossingPending = false;
        if (crossingFallback !== null) {
          window.clearTimeout(crossingFallback);
          crossingFallback = null;
        }
        applyFrac();
      };
      const renderSub = term.onRender(releaseCrossing);
      const noteCrossing = () => {
        crossingPending = true;
        if (crossingFallback === null) {
          crossingFallback = window.setTimeout(() => {
            crossingFallback = null;
            releaseCrossing();
          }, 50);
        }
      };

      // ── touch-scroll for a fullscreen TUI (Claude Code, vim, htop…) ─────────
      // When the running app "owns" the scroll (see appOwnsScroll) there's no
      // scrollback for scrollByPx to slide — the app holds its history in its
      // own memory and repaints in response to scroll INPUT. So we translate the
      // finger drag into wheel notches and let xterm encode them per the app's
      // negotiated mouse protocol — the SAME path a real mouse wheel uses on the
      // desktop, which is why the encoding is guaranteed to match. The momentum
      // loop below drives this for free (it just keeps calling scrollByPx).
      // Gated by TUI_WHEEL_FORWARDING so it can be disabled in one line if a
      // future xterm/Claude Code change regresses it; off = the prior
      // do-nothing-when-the-app-owns-scroll behaviour, exactly.
      // Fallback if synthetic wheel ever stops being honored: a direct SGR-1006
      // wheel write to the PTY — ws.send {type:'in', data:`\x1b[<${up?64:65};1;1M`}.
      const TUI_WHEEL_FORWARDING = true;
      // Detect "the app owns the scroll" so we forward wheel instead of sliding
      // the local buffer. Two signals, OR'd:
      //   - alternate screen buffer (vim/htop), OR
      //   - mouse tracking active — Claude Code's fullscreen renderer turns this
      //     ON (that's what CLAUDE_CODE_DISABLE_MOUSE disables).
      // History/why mouse-tracking and not alt-buffer alone: an alt-buffer-only
      // gate never fired — through this PTY pipeline Claude Code's fullscreen
      // repaints into the NORMAL buffer (its old frames pile up as scrollback),
      // so scrollByPx fell through to the classic path and revealed that garbled
      // repaint history instead of driving the app's own scroll (Nathan,
      // 2026-06-22). Mouse-tracking is the reliable signal; it also fires
      // regardless of which buffer is active.
      const appOwnsScroll = (): boolean => {
        try {
          if (term.buffer.active.type === 'alternate') return true;
          const m = term.modes?.mouseTrackingMode;
          return !!m && m !== 'none';
        } catch { return false; }
      };
      // Accumulates sub-row drag px until a whole wheel notch's worth has built
      // up, then fires that many notches. One notch ≈ one cell row of drag, so
      // the scroll distance tracks the finger at roughly 1:1 (Claude Code's own
      // CLAUDE_CODE_SCROLL_SPEED then multiplies how far each notch moves).
      let wheelAccumPx = 0;
      const emitWheelDrag = (px: number): void => {
        const ch = getCellHeight();
        wheelAccumPx += px;
        const notches = Math.trunc(wheelAccumPx / ch);
        if (notches === 0) return;
        wheelAccumPx -= notches * ch;
        // Sign only — in mouse mode xterm sends one button press per wheel
        // event regardless of magnitude. Positive px = drag toward newer output
        // = wheel DOWN (deltaY > 0). Cap the per-frame burst so a violent fling
        // can't dump 40 notches into one frame and overshoot the conversation.
        const deltaY = notches > 0 ? 120 : -120;
        const count = Math.min(Math.abs(notches), 8);
        const target = screenEl ?? el;
        for (let i = 0; i < count; i++) {
          target.dispatchEvent(new WheelEvent('wheel', {
            deltaY, deltaMode: 0, bubbles: true, cancelable: true,
          }));
        }
      };

      // Scroll by a pixel delta: whole rows go to the buffer, the remainder
      // becomes the visual fraction. Positive px = scroll the buffer down
      // (toward newer output) — "swipe up reveals more recent content".
      // Returns false when nothing moved (buffer pinned at an end, fraction
      // clamped) so the momentum loop knows to stop instead of spinning.
      const scrollByPx = (px: number): boolean => {
        if (TUI_WHEEL_FORWARDING && appOwnsScroll()) {
          // Fullscreen TUI: drive the app's own scroll via wheel forwarding.
          // Always report movement — we can't observe the app's scroll limits
          // (it just stops repainting at the top), and returning false here
          // would make the momentum loop misread "no scrollback" as "hit the
          // edge" and die on frame 1. Over-scrolling past the top is harmless:
          // the app ignores the extra notches.
          emitWheelDrag(px);
          return true;
        }
        const buf = term.buffer.active;
        const startY = buf.viewportY;
        const startFrac = pxAccum;
        pxAccum += px;
        const ch = getCellHeight();
        const lines = Math.trunc(pxAccum / ch);
        if (lines !== 0) {
          term.scrollLines(lines);
          pxAccum -= lines * ch;
          const actual = buf.viewportY - startY;
          // Asked for N rows but the buffer clamped at an end — drop the
          // fraction too so the grid never floats past the first/last line.
          if (actual !== lines) pxAccum = 0;
          // Grid actually moved → hold the transform for onRender. A fully
          // clamped call moved nothing, so no render is coming — fall
          // through to the plain rAF path instead of the 50ms backstop.
          if (actual !== 0) noteCrossing();
        }
        // Same clamp for sub-row pulls at either end. Also covers the
        // alternate buffer (viewportY === baseY === 0 — no scrollback), so
        // full-screen apps never get a floating half-row offset.
        if ((buf.viewportY <= 0 && pxAccum < 0) ||
            (buf.viewportY >= buf.baseY && pxAccum > 0)) {
          pxAccum = 0;
        }
        scheduleFrac();
        return buf.viewportY !== startY || pxAccum !== startFrac;
      };

      // Bring the resting offset back onto a row boundary: hop the buffer
      // one row if the fraction is past halfway, then glide the few
      // remaining px to zero with a short transform transition. Frame
      // choreography matters here for the same one-frame-pop reason as
      // scheduleFrac: the hop's grid repaint and the re-synced transform
      // must land in the same paint (first rAF, registered after the
      // scrollLines call), and only THEN is the transition armed (second
      // rAF) so the glide animates just the final ≤½-row.
      const settle = () => {
        if (pxAccum === 0) return;
        const ch = getCellHeight();
        if (Math.abs(pxAccum) >= ch / 2) {
          const dir = pxAccum > 0 ? 1 : -1;
          const buf = term.buffer.active;
          const y0 = buf.viewportY;
          term.scrollLines(dir);
          if (buf.viewportY !== y0) {
            pxAccum -= dir * ch;
            noteCrossing();  // transform re-sync rides the hop's render
          } else {
            pxAccum = 0;
          }
        }
        if (!screenEl) { pxAccum = 0; return; }
        settleRaf = requestAnimationFrame(() => {
          if (!crossingPending) applyFrac();
          settleRaf = requestAnimationFrame(() => {
            settleRaf = null;
            screenEl.style.transition = 'transform 90ms ease-out';
            screenEl.style.transform = '';
            pxAccum = 0;
            if (settleTimer !== null) window.clearTimeout(settleTimer);
            settleTimer = window.setTimeout(() => {
              screenEl.style.transition = '';
              settleTimer = null;
            }, 120);
          });
        });
      };

      // Abort a settle that hasn't finished (rAF chain and/or transition
      // cleanup timer) — called when a new touch grabs the content so the
      // stale callbacks can't zero pxAccum or re-arm a transition mid-drag.
      const cancelSettle = () => {
        if (settleRaf !== null) { cancelAnimationFrame(settleRaf); settleRaf = null; }
        if (settleTimer !== null) { window.clearTimeout(settleTimer); settleTimer = null; }
        if (screenEl) screenEl.style.transition = '';
      };

      const stopMomentum = () => {
        if (momentumRaf !== null) {
          cancelAnimationFrame(momentumRaf);
          momentumRaf = null;
        }
      };

      const cancelDrag = () => {
        if (dragRaf !== null) { cancelAnimationFrame(dragRaf); dragRaf = null; }
        pendingDragPx = 0;
      };
      // Apply whatever drag delta is still waiting on its frame — touchend
      // calls this so momentum hands off from where the finger actually
      // left the content, not a frame behind it.
      const flushDrag = () => {
        if (dragRaf !== null) { cancelAnimationFrame(dragRaf); dragRaf = null; }
        if (pendingDragPx !== 0) {
          const px = pendingDragPx;
          pendingDragPx = 0;
          scrollByPx(px);
        }
      };
      const scheduleDrag = () => {
        if (dragRaf !== null) return;
        dragRaf = requestAnimationFrame(() => {
          dragRaf = null;
          if (pendingDragPx !== 0) {
            const px = pendingDragPx;
            pendingDragPx = 0;
            scrollByPx(px);
          }
        });
      };

      const onTouchStart = (e: TouchEvent) => {
        if (e.touches.length !== 1) {
          traceLine(`ts! n=${e.touches.length}`);
          return;
        }
        traceLine(`ts y=${e.touches[0].clientY.toFixed(0)} mom=${momentumRaf !== null ? 1 : 0}`);
        stopMomentum();
        cancelDrag();
        // Grab the content where it currently sits: cancel any in-flight
        // settle glide but KEEP pxAccum (don't reset it like the old handler
        // did), so consecutive drags stay visually continuous.
        cancelSettle();
        if (!crossingPending) applyFrac();
        const y = e.touches[0].clientY;
        lastY = y;
        touchStartY = y;
        touchMoved = false;
        samples = [{ y, t: evTime(e) }];
      };

      const onTouchMove = (e: TouchEvent) => {
        if (e.touches.length !== 1) {
          traceLine(`tm! n=${e.touches.length}`);
          return;
        }
        const y = e.touches[0].clientY;
        const dy = lastY - y;  // swipe up → positive → scroll buffer down
        if (Math.abs(dy) > 0) {
          // Override xterm's selection behavior + native scroll.
          try { e.preventDefault(); } catch { /* passive listener — fine */ }
          pendingDragPx += dy;
          scheduleDrag();
          touchMoved = touchMoved || Math.abs(y - touchStartY) > 6;
        }
        const t = evTime(e);
        traceLine(`tm y=${y.toFixed(0)} dy=${dy.toFixed(1)} lag=${(performance.now() - t).toFixed(1)}`);
        samples.push({ y, t });
        while (samples.length > 2 && samples[0].t < t - VELOCITY_WINDOW_MS) {
          samples.shift();
        }
        lastY = y;
      };

      const onTouchEnd = (e: TouchEvent) => {
        // Land the drag delta still waiting on its frame, so momentum picks
        // up exactly where the finger left the content.
        flushDrag();
        // The lift position is the last word on displacement: when WebKit
        // starved the move stream (violent jab), most of the travel exists
        // ONLY in this event's changedTouches. Recording it counts that
        // travel, and lets a fling with zero delivered moves still register
        // touchMoved.
        const lift = e.changedTouches && e.changedTouches[0];
        const tEnd = evTime(e);
        if (lift) {
          samples.push({ y: lift.clientY, t: tEnd });
          touchMoved = touchMoved || Math.abs(lift.clientY - touchStartY) > 6;
        }
        while (samples.length > 2 && samples[0].t < tEnd - VELOCITY_WINDOW_MS) {
          samples.shift();
        }
        // Hybrid release velocity (px/ms, positive = finger moved up =
        // scroll buffer down), from two estimators:
        //   - window average: displacement over the whole sample window.
        //     Stable, but a short violent jab gets diluted by its own slow
        //     first samples.
        //   - last segment: the final two samples. Tracks the release
        //     instant; noisy on tiny dt, so dt is floored at 8ms.
        // Same sign → larger magnitude wins (jabs keep their real speed; a
        // deliberate pin-then-lift reads ~0 from BOTH, so it still stops).
        // Opposite signs → the segment wins: the user reversed direction at
        // the end, and recent intent beats the stale window. Capped at
        // ±6 px/ms — beyond that is sampling noise, not a finger.
        let vWin = 0;
        if (samples.length >= 2) {
          const first = samples[0];
          const last = samples[samples.length - 1];
          const dt = last.t - first.t;
          if (dt > 0) vWin = (first.y - last.y) / dt;
        }
        let vSeg = 0;
        if (samples.length >= 2) {
          const a = samples[samples.length - 2];
          const b = samples[samples.length - 1];
          vSeg = (a.y - b.y) / Math.max(b.t - a.t, 8);
        }
        let velocity =
          vWin === 0 ? vSeg
          : vSeg !== 0 && Math.sign(vSeg) !== Math.sign(vWin) ? vSeg
          : Math.abs(vSeg) > Math.abs(vWin) ? vSeg
          : vWin;
        velocity = Math.max(-6, Math.min(6, velocity));
        // Diagnostics: sample count + window span (ms) prove the dilution
        // case — a 2-sample window spanning a long dwell is the fast-flick
        // failure mode (WebKit starved every touchmove; vWin/vSeg collapse
        // onto one diluted number). See 2026-06-13 momentum diagnosis.
        const nSamp = samples.length;
        const span = nSamp >= 2 ? (samples[nSamp - 1].t - samples[0].t) : 0;
        // No fling: either a tap (settle is a no-op, fraction is 0) or a
        // slow drag that stopped — ease the leftover fraction onto a row.
        // 0.02 px/ms ≈ 20px/s; below that momentum wouldn't visibly move.
        if (!touchMoved || Math.abs(velocity) < 0.02) {
          traceLine(`te vWin=${vWin.toFixed(3)} vSeg=${vSeg.toFixed(3)} v=${velocity.toFixed(3)} n=${nSamp} span=${span.toFixed(0)} moved=${touchMoved ? 1 : 0} -> settle`);
          maybeUploadTrace();
          settle();
          return;
        }
        traceLine(`te vWin=${vWin.toFixed(3)} vSeg=${vSeg.toFixed(3)} v=${velocity.toFixed(3)} n=${nSamp} span=${span.toFixed(0)} -> momentum`);
        // Time-based momentum decay, integrated over the real frame delta so
        // 60Hz and 120Hz ProMotion behave identically. The decay constant is
        // UIScrollView's own: native iOS scrolling retains 0.998 of its
        // velocity per millisecond, which carries a fling noticeably farther
        // than the old 0.95-per-frame factor — that early die-off read as
        // "not smooth" next to native scrolling on the same screen.
        let v = velocity;            // px per ms — keep the gesture's units
        let glidePx = 0;
        let dt0 = -1;                // first-frame dt; 0 ⇒ the dt=0 edge-stop bug
        const tLaunch = performance.now();
        let prev = tLaunch;
        const step = (now: number) => {
          // Clamp dt both ways: a janked/backgrounded frame can't teleport
          // the viewport when rAF resumes (max 64), and a first-frame rAF
          // timestamp that lands BEFORE the touchend handler ran — rAF
          // stamps at vsync — can't scroll backwards (min 0).
          const dt = Math.min(Math.max(now - prev, 0), 64);
          // dt=0 ⇒ this frame's vsync stamp preceded the touchend handler —
          // the common case for the FIRST momentum frame. Running scrollByPx(0)
          // moves nothing and returns false, which the edge check below
          // misreads as "hit the end of scrollback" and kills the fling on
          // frame 1. That was the real, speed-INDEPENDENT cause of the ~50/50
          // "stops dead" report — trace 2026-06-13 showed every death as
          // `mo edge 0ms glide=0px dt0=0` and every survivor as dt0=1. Skip to
          // the next frame (don't advance prev, so its dt spans from launch).
          if (dt === 0) {
            momentumRaf = requestAnimationFrame(step);
            return;
          }
          if (dt0 < 0) dt0 = dt;
          prev = now;
          if (!scrollByPx(v * dt)) {
            // Hit the top/bottom of the scrollback — stop dead instead of
            // burning frames decaying a velocity that moves nothing.
            momentumRaf = null;
            traceLine(`mo edge after ${(now - tLaunch).toFixed(0)}ms glide=${glidePx.toFixed(0)}px dt0=${dt0.toFixed(1)}`);
            maybeUploadTrace();
            settle();
            return;
          }
          glidePx += v * dt;
          v *= Math.pow(0.998, dt);
          // ~0.3px per 60Hz frame — below this the motion is invisible.
          if (Math.abs(v) < 0.02) {
            momentumRaf = null;
            traceLine(`mo decay after ${(now - tLaunch).toFixed(0)}ms glide=${glidePx.toFixed(0)}px dt0=${dt0.toFixed(1)}`);
            maybeUploadTrace();
            settle();
            return;
          }
          momentumRaf = requestAnimationFrame(step);
        };
        momentumRaf = requestAnimationFrame(step);
      };

      // capture: true so we run before xterm's own touch handlers; passive
      // false so preventDefault works to override its selection / native scroll.
      const opts: AddEventListenerOptions = { capture: true, passive: false };
      el.addEventListener('touchstart', onTouchStart, opts);
      el.addEventListener('touchmove', onTouchMove, opts);
      el.addEventListener('touchend', onTouchEnd, opts);
      el.addEventListener('touchcancel', onTouchEnd, opts);
      bundle.detachTouch = () => {
        stopMomentum();
        cancelDrag();
        cancelSettle();
        if (fracRaf !== null) { cancelAnimationFrame(fracRaf); fracRaf = null; }
        crossingPending = false;
        if (crossingFallback !== null) { window.clearTimeout(crossingFallback); crossingFallback = null; }
        try { renderSub.dispose(); } catch { /* term already disposed */ }
        if (screenEl) screenEl.style.transform = '';
        el.removeEventListener('touchstart', onTouchStart, opts);
        el.removeEventListener('touchmove', onTouchMove, opts);
        el.removeEventListener('touchend', onTouchEnd, opts);
        el.removeEventListener('touchcancel', onTouchEnd, opts);
      };
    }

    const connect = () => {
      bundle.status = 'connecting';
      bundle.onStatusChange();
      // One-shot takeover (set by the ↻ refresh): claim the resize lock and
      // size the PTY to this device. fit.fit() already ran in the refresh
      // handler, so term.rows/cols are this device's current dimensions.
      const takeover = bundle.pendingTakeover;
      bundle.pendingTakeover = false;
      const ws = new WebSocket(
        terminalWsUrl(id, takeover ? { rows: bundle.term.rows, cols: bundle.term.cols } : undefined),
      );
      // Output arrives as binary WS frames (raw UTF-8 PTY bytes — we opt in
      // via ?bin=1 in the URL). arraybuffer (not the default 'blob') so
      // ev.data is synchronously readable in onmessage; Blob would force an
      // async read that reorders chunks relative to text control frames.
      ws.binaryType = 'arraybuffer';
      bundle.ws = ws;
      // ── flow-control counters — per CONNECTION, not per bundle ────────────
      // Local to this connect() closure so a fresh attach resets the ACK
      // window automatically; the server's in-flight counter is likewise
      // per-WS. Stale write callbacks from a dead connection can't pollute a
      // new one: they close over the old `ws`, and the readyState guard below
      // drops their acks.
      let receivedBytes = 0;   // payload bytes handed to term.write()
      let processedBytes = 0;  // payload bytes xterm has finished processing
      let unackedBytes = 0;    // processed but not yet ACKed to the server
      // Strips Claude Code's hard-coded dark-gray chip backgrounds before
      // they hit the screen (see SgrBgRemapper). Per-connection like the
      // flow counters, so a reconnect starts with an empty carry. The flush
      // timer releases a carried partial escape sequence if the stream goes
      // idle mid-sequence (in practice only a pathological lone ESC at
      // burst end).
      const remapper = new SgrBgRemapper(() => accentRgbRef.current);
      let carryFlushTimer: number | null = null;
      // Fires once xterm has actually parsed a chunk — the "processed"
      // signal the ACK window is built on. Counted in WIRE bytes (the
      // server's unit), never the remapper's output length.
      const noteProcessed = (chunkBytes: number) => {
        processedBytes += chunkBytes;
        unackedBytes += chunkBytes;
        // ACK every ~100KB, PLUS a drain-flush whenever the write
        // queue catches up (processed === received). The drain-flush
        // is what guarantees the server's in-flight count converges
        // to zero after a burst — without it, a residual smaller than
        // the ACK threshold could sit above the server's low
        // watermark forever and deadlock a paused stream.
        if (
          unackedBytes > 0 &&
          (unackedBytes >= FLOW_ACK_EVERY_BYTES || processedBytes === receivedBytes) &&
          ws.readyState === WebSocket.OPEN
        ) {
          ws.send(JSON.stringify({ type: 'ack', bytes: unackedBytes }));
          unackedBytes = 0;
        }
      };
      // Shared output path for both wire formats (binary frame / legacy JSON
      // 'out'). chunkBytes is the raw UTF-8 byte length of the payload — the
      // ACK unit the server's in-flight window is counted in.
      const writeChunk = (chunk: string | Uint8Array, chunkBytes: number) => {
        receivedBytes += chunkBytes;
        const raw = typeof chunk === 'string' ? utf8Encoder.encode(chunk) : chunk;
        const filtered = remapper.push(raw);
        if (carryFlushTimer !== null) window.clearTimeout(carryFlushTimer);
        carryFlushTimer = window.setTimeout(() => {
          const tail = remapper.flush();
          if (tail && tail.length > 0) {
            try { term.write(tail); } catch { /* term disposed */ }
          }
        }, 120);
        // A chunk swallowed whole into the carry produces zero output;
        // term.write callbacks aren't guaranteed for empty writes, so do
        // the ACK bookkeeping synchronously to keep the window draining.
        if (filtered.length === 0) {
          noteProcessed(chunkBytes);
          return;
        }
        term.write(filtered, () => noteProcessed(chunkBytes));
      };
      // Reset xterm before each connect so the server's screen-state replay
      // doesn't pile on top of the prior visible state and visually duplicate.
      term.reset();
      ws.onopen = () => {
        bundle.status = 'open';
        bundle.reconnectMs = 500;
        bundle.onStatusChange();
        // Send current dimensions so the shell prompt wraps correctly from the start.
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
        // Declare ACK flow-control support BEFORE any output arrives. The
        // server only arms its pause logic after this hello, so an old
        // routes_ws.py simply ignores it (unknown types are dropped) and
        // an old client bundle (which never sends it) keeps legacy behavior.
        ws.send(JSON.stringify({ type: 'flow' }));
        // Auto-fill the selected launch command at the PowerShell prompt
        // without hitting Enter, so Nathan can press Enter to run it or
        // backspace to type something else. The 700ms delay gives the
        // prompt time to print before we type. Reconnects skip this —
        // the id is removed from the set after the first fire.
        if (pendingBootstrapRef.current.has(id)) {
          pendingBootstrapRef.current.delete(id);
          window.setTimeout(() => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'in', data: bootstrapCmdRef.current }));
            }
          }, 700);
        }
      };
      ws.onmessage = (ev) => {
        // Binary frame = terminal output, raw UTF-8 bytes — straight into
        // xterm (it decodes Uint8Array as UTF-8, statefully across writes).
        // The byte length on the wire is exactly what the server counted
        // into its in-flight window, so it's also our ACK unit.
        if (ev.data instanceof ArrayBuffer) {
          const bytes = new Uint8Array(ev.data);
          writeChunk(bytes, bytes.byteLength);
          return;
        }
        // Text frame = JSON control message (exit/ping/meta) — or a legacy
        // {type:'out'} output chunk from a pre-binary dashboard that ignored
        // our ?bin=1 opt-in.
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'out' && typeof msg.data === 'string') {
            writeChunk(msg.data, utf8Encoder.encode(msg.data).length);
          } else if (msg.type === 'exit') {
            bundle.alive = false;
            term.write('\r\n\x1b[2;37m[session exited]\x1b[0m\r\n');
            bundle.onStatusChange();
            // Close our side too. The server now closes after sending
            // 'exit', but an old (pre-fix) dashboard doesn't — without this
            // the socket idles open forever (alive=false suppresses the
            // reconnect in onclose, so closing here is terminal-state safe).
            try { ws.close(); } catch { /* already closing */ }
          }
        } catch { /* malformed — ignore */ }
      };
      ws.onerror = () => { /* onclose follows; reconnect there */ };
      ws.onclose = (ev) => {
        bundle.ws = null;
        bundle.status = 'closed';
        bundle.onStatusChange();
        // 4404 = "session not found / dead" from our handler. Don't reconnect;
        // the session is gone on the backend.
        if (ev.code === 4404) {
          bundle.alive = false;
          term.write('\r\n\x1b[2;37m[session no longer available]\x1b[0m\r\n');
          return;
        }
        // 4001 = "Service Restart" — the dashboard is restarting intentionally.
        // The session is alive; the new dashboard instance will accept reconnects.
        // Show a transient message (term.reset() in connect() clears it on success).
        if (ev.code === 4001) {
          term.write('\r\n\x1b[2;37m[dashboard restarting — reconnecting…]\x1b[0m\r\n');
        }
        if (!bundle.alive) return;  // explicit exit; user must close the tab
        // Backoff up to 10s. The session is still alive on the backend so
        // reattach should always succeed.
        bundle.reconnectTimer = window.setTimeout(connect, bundle.reconnectMs);
        bundle.reconnectMs = Math.min(bundle.reconnectMs * 2, 10_000);
      };
    };
    connect();
  }, [xtermTheme, isMobile, bumpRender, handleTermTitle]);

  // ── ResizeObserver on the host: refit every visible terminal whenever the
  // card area changes (window resize, mobile rotation, tab-switch back) ─────
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const ro = new ResizeObserver(() => {
      visibleIds.forEach(id => {
        const b = bundlesRef.current.get(id);
        if (!b) return;
        try { b.fit.fit(); } catch { /* container hidden mid-tween — next tick will retry */ }
      });
    });
    ro.observe(host);
    return () => ro.disconnect();
  }, [visibleIds]);

  // When the set of visible panes changes (tab switch, layout toggle, pane
  // assignment) OR when the keyboard opens/closes (which changes the visible
  // host height on mobile), refit every visible term. The host's OWN box
  // doesn't change on a layout toggle — only the pane widths do — so the
  // ResizeObserver above never fires for those; this effect covers them.
  useEffect(() => {
    // requestAnimationFrame so the display:block flip / keyboard transition
    // has painted before fit().
    const raf = requestAnimationFrame(() => {
      visibleIds.forEach(id => {
        const b = bundlesRef.current.get(id);
        if (!b) return;
        try { b.fit.fit(); } catch { /* noop */ }
      });
      // On mobile we never focus the terminal — the chat box is the input.
      // Touch devices on the DESKTOP layout (phone in landscape) must not
      // auto-focus either: focusing xterm's textarea summons the iOS
      // keyboard uninvited, and the keyboard push left the page's tap
      // targets offset from the visuals — Nathan's "tabs not selectable,
      // had to tap around" report (2026-06-10). Tapping the terminal still
      // focuses it explicitly when typing there is actually wanted.
      if (!isMobile && !IS_COARSE_POINTER && activeId) {
        const b = bundlesRef.current.get(activeId);
        b?.term.focus();
      }
    });
    return () => cancelAnimationFrame(raf);
  }, [visibleIds, activeId, isMobile]);

  // iOS keyboard transitions don't change the host element's CSS box, so the
  // ResizeObserver above never fires for them — but the chat group moves on
  // top of the terminal output (when keyboard up), so the visible-row count
  // changes and the active term should re-measure. Subscribe to visualViewport
  // resize + scroll on mobile only and refit the active bundle on each tick.
  useEffect(() => {
    if (!isMobile) return;
    const vv = window.visualViewport;
    if (!vv) return;
    const refit = () => {
      const id = activeId;
      if (!id) return;
      const b = bundlesRef.current.get(id);
      if (!b) return;
      // RAF lets the keyboard transition paint before we measure.
      requestAnimationFrame(() => {
        try { b.fit.fit(); } catch { /* noop */ }
      });
    };
    vv.addEventListener('resize', refit);
    vv.addEventListener('scroll', refit);
    return () => {
      vv.removeEventListener('resize', refit);
      vv.removeEventListener('scroll', refit);
    };
  }, [isMobile, activeId]);

  // ── recover from backgrounding: rebuild the WebGL texture atlas + refit ───
  // iOS reclaims GPU resources from backgrounded tabs. The WebGL renderer's
  // onContextLoss handler (above) only fires for a true context loss; a more
  // common case is the texture atlas surviving but its contents getting
  // scrambled, which reads as garbled/misplaced glyphs once the tab is
  // foregrounded again — previously only fixed by an orientation flip
  // (which forces a resize → fit → redraw). clearTextureAtlas() is xterm's
  // documented hook for exactly this: it wipes the atlas and redraws from
  // scratch. fit() + refresh() cover the DOM-renderer fallback and any
  // viewport-size drift that happened while backgrounded.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      requestAnimationFrame(() => {
        bundlesRef.current.forEach(b => {
          try { b.webgl?.clearTextureAtlas(); } catch { /* context lost — DOM fallback below */ }
          try { b.fit.fit(); } catch { /* container hidden — next tick will retry */ }
          try { b.term.refresh(0, b.term.rows - 1); } catch { /* term disposed */ }
        });
      });
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, []);

  // ── teardown on card unmount: close all WSes, dispose xterms ───────────────
  // We do NOT kill the sessions on the backend — persistence is the point.
  useEffect(() => {
    const bundles = bundlesRef.current;
    const titleTimers = titlePersistTimersRef.current;
    return () => {
      bundles.forEach(b => {
        if (b.reconnectTimer) window.clearTimeout(b.reconnectTimer);
        if (b.detachTouch) b.detachTouch();
        b.alive = false;  // suppress reconnect from onclose
        b.ws?.close();
        // Release the GPU context before the terminal goes (term.dispose()
        // would also do it, but explicit beats implicit for a finite resource).
        try { b.webgl?.dispose(); } catch { /* already disposed on context loss */ }
        b.term.dispose();
      });
      bundles.clear();
      titleTimers.forEach(t => window.clearTimeout(t));
      titleTimers.clear();
    };
  }, []);

  // ── actions ────────────────────────────────────────────────────────────────
  const sendInput = useCallback((data: string): boolean => {
    if (!activeId) return false;
    const b = bundlesRef.current.get(activeId);
    if (!b || !b.ws || b.ws.readyState !== WebSocket.OPEN) return false;
    b.ws.send(JSON.stringify({ type: 'in', data }));
    return true;
  }, [activeId]);

  // ↻ tool-row button: DUAL-PURPOSE — recover a garbled terminal AND take over
  // resize control for this device. Hitting it does two things:
  //   1. Recovery: rebuilds the GPU glyph atlas (iOS reclaims texture memory in
  //      the background) and re-pulls the full screen from the server. Garbled
  //      buffer CONTENT (bytes mangled on a flaky reconnect, or a replay that
  //      was fit to another device's width) can't be fixed by a client redraw;
  //      the authoritative copy is the pty-manager's server-side pyte screen,
  //      and the reattach rebuilds from it: connect() does term.reset() then the
  //      server replays the serialized grid + scrollback. So: deliberately close
  //      the WS and let the standing onclose → reconnect machinery re-pull —
  //      same recovery as a dashboard restart, on demand.
  //   2. Takeover (Nathan, 2026-06-15): the reconnect carries takeover=1 + this
  //      device's rows/cols, so the server claims the resize lock for it and
  //      sizes the shared PTY to THIS display BEFORE serializing the replay.
  //      Work on a wide PC, then open the phone and hit ↻: history reflows to
  //      the phone instead of running off the right edge (it was sized for the
  //      PC, which may still be connected). One ConPTY = one size, so the last
  //      device to take over wins. See trident_pty_manager.py:_cmd_attach,
  //      [[footguns#150]], [[decisions/2026-06-15_terminal-replay-screen-model]].
  //
  // The size jiggle below — shrink one row, then fit back — is belt-and-braces:
  // it forces ConPTY to repaint the live screen and rebuilds the GPU glyph
  // atlas, the same repaint a phone rotation triggers. The 120ms gap keeps the
  // two resizes from coalescing into a no-op at the backend.
  const handleRefreshTerminal = useCallback(() => {
    if (!activeId) return;
    const id = activeId;
    const b = bundlesRef.current.get(id);
    if (!b || !b.alive) return;
    // Arm the takeover so the imminent reconnect claims the lock + sizes the
    // PTY to this device. fit.fit() below refreshes term dims first.
    b.pendingTakeover = true;
    try { b.webgl?.clearTextureAtlas(); } catch { /* context lost — replay repaints anyway */ }
    try { b.fit.fit(); } catch { /* container hidden — reconnect's resize uses last-good dims */ }
    // Only close an OPEN socket. CONNECTING would race its own onopen, and
    // null/CLOSED means a reconnect timer is already pending — either way
    // the wait loop below picks up whichever connection lands.
    if (b.ws && b.ws.readyState === WebSocket.OPEN) {
      try { b.ws.close(); } catch { /* already closing */ }
    }
    // Wait for the reattach (reconnect backoff starts at 500ms), then jiggle.
    // Interval-poll instead of hooking onopen: connect() lives inside
    // ensureBundle's closure and re-wires bundle.ws on every reconnect, so
    // polling the bundle is the non-invasive way to see the new socket.
    const t0 = Date.now();
    const waitOpen = window.setInterval(() => {
      const cur = bundlesRef.current.get(id);
      if (!cur || !cur.alive || Date.now() - t0 > 8000) {
        window.clearInterval(waitOpen);
        return;
      }
      if (!cur.ws || cur.ws.readyState !== WebSocket.OPEN) return;
      window.clearInterval(waitOpen);
      const { term, fit } = cur;
      // Recover EVERY visible pane (desktop split view), not just the target.
      // All same-font/size panes share xterm's per-config WebGL texture atlas;
      // the takeover's clearTextureAtlas + replay REPACKED that shared atlas,
      // leaving the other panes' renderers drawing from cached glyph slots that
      // no longer hold the right characters — the "alien writing" Nathan
      // reported (2026-06-16). A plain refresh() wasn't enough: on xterm 6 it
      // redraws from the already-stale glyph cache without rebuilding it. The
      // fix is the SAME recovery the visibilitychange handler does (which
      // Nathan confirmed clears the garble): clearTextureAtlas() wipes the
      // shared atlas + each renderer's glyph cache, fit() re-measures, and
      // refresh() repaints every cell from scratch. Because the atlas is
      // shared, clearing it on one pane clears it for all — so EVERY visible
      // pane must then repaint, or the un-refreshed ones go blank/garbled.
      const recoverAllPanes = () => {
        visibleIds.forEach(vid => {
          const vb = bundlesRef.current.get(vid);
          if (!vb) return;
          try { vb.webgl?.clearTextureAtlas(); } catch { /* DOM fallback */ }
          try { vb.fit.fit(); } catch { /* hidden mid-layout */ }
          try { vb.term.refresh(0, vb.term.rows - 1); } catch { /* disposed */ }
        });
      };
      try {
        if (term.rows > 2) {
          term.resize(term.cols, term.rows - 1);
          window.setTimeout(() => {
            try { fit.fit(); } catch { /* term disposed mid-refresh */ }
            // First recovery pass once the resize jiggle has applied.
            recoverAllPanes();
            // Second pass: the takeover's server replay streams in just after
            // reconnect and re-rasterizes glyphs into the freshly-cleared
            // atlas. A single early pass can run mid-replay and get partially
            // undone; a deferred pass after the replay has settled guarantees
            // every pane ends consistent.
            window.setTimeout(recoverAllPanes, 600);
          }, 120);
        } else {
          // Degenerate geometry (container mid-layout) — repaint only.
          recoverAllPanes();
          window.setTimeout(recoverAllPanes, 600);
        }
      } catch { /* term disposed */ }
    }, 150);
  }, [activeId, visibleIds]);

  // Mobile chat box: dual-mode send. Text in box → push text (no enter).
  // Empty box → send `\r`. Box clears after every send.
  const handleSend = useCallback(() => {
    if (chatText.length > 0) {
      sendInput(chatText);
      setChatText('');
      // contenteditable content isn't managed by React — clear it manually.
      const el = chatInputRef.current as unknown as HTMLDivElement | null;
      if (el) el.textContent = '';
    } else {
      sendInput('\r');
    }
    // Keep the keyboard up — refocus after the next render so iOS doesn't
    // dismiss it. Also reset the cursor inside the now-empty contenteditable.
    // ONLY when the input was focused at send time (keyboard up): tapping ↵
    // with the keyboard down (send a bare enter to the terminal) must not
    // grab focus — programmatic focus raises no keyboard, and a focused
    // input with no keyboard hides the bottom tab bar for nothing.
    const wasFocused = document.activeElement === chatInputRef.current;
    if (wasFocused) {
      requestAnimationFrame(() => {
        const el = chatInputRef.current as unknown as HTMLDivElement | null;
        el?.focus();
      });
    }
  }, [chatText, sendInput]);

  // Font picker: persist the choice, fetch the face if it's a web font, then
  // restyle every live terminal in place (xterm rebuilds its glyph atlas on
  // fontFamily change; fit() re-derives cols/rows for the new cell metrics
  // and the onResize hook tells each PTY about the new geometry).
  const handleFontChange = useCallback((value: string) => {
    setTermFont(value);
    try { localStorage.setItem('claudeTermFont', value); } catch { /* private mode */ }
    const c = findFontChoice(value);
    void ensureFontLoaded(c).then(() => {
      const fam = fontFamilyFor(c);
      bundlesRef.current.forEach(b => {
        try {
          b.term.options.fontFamily = fam;
          b.fit.fit();
          b.term.refresh(0, b.term.rows - 1);
        } catch { /* term disposed mid-switch */ }
      });
    });
  }, []);

  // Font-size picker (desktop): persist the choice, then restyle every live
  // terminal in place. xterm re-derives cell metrics on fontSize change;
  // fit() recomputes cols/rows and the onResize hook tells each PTY.
  const handleFontSizeChange = useCallback((size: number) => {
    const px = clampFontSize(size);
    setTermFontSize(px);
    try { localStorage.setItem('claudeTermFontSize', String(px)); } catch { /* private mode */ }
    bundlesRef.current.forEach(b => {
      try {
        b.term.options.fontSize = px;
        b.fit.fit();
        b.term.refresh(0, b.term.rows - 1);
      } catch { /* term disposed mid-switch */ }
    });
  }, []);

  // Tab click: if the session is already on screen, just focus its pane;
  // otherwise load it into the focused pane.
  const handleTabClick = useCallback((id: string) => {
    const idx = paneIds.indexOf(id);
    if (idx >= 0) {
      setFocusedPane(idx);
      return;
    }
    setPaneIds(prev => {
      const next = [...prev];
      next[focusedPane] = id;
      return next;
    });
  }, [paneIds, focusedPane]);

  // Layout toggle: grow panes by filling new slots with sessions not already
  // on screen (in session order); shrink by dropping the rightmost panes.
  // Their sessions stay alive in the tab strip — only visibility changes.
  const handleLayoutChange = useCallback((n: LayoutCount) => {
    setLayoutCount(n);
    try { localStorage.setItem(LAYOUT_KEY, String(n)); } catch { /* private mode */ }
    setPaneIds(prev => {
      const next = prev.slice(0, n);
      while (next.length < n) {
        const candidate = sessions.find(s => !next.includes(s.id));
        next.push(candidate ? candidate.id : null);
      }
      return next;
    });
    setFocusedPane(f => Math.min(f, n - 1));
  }, [sessions]);

  const clearPendingClose = useCallback(() => {
    setPendingCloseId(null);
    if (pendingCloseTimerRef.current !== null) {
      window.clearTimeout(pendingCloseTimerRef.current);
      pendingCloseTimerRef.current = null;
    }
  }, []);

  const handleNewTab = useCallback(async () => {
    clearPendingClose();
    setBusy(true);
    setError(null);
    try {
      const s = await createSession();
      pendingBootstrapRef.current.add(s.id);
      setSessions(prev => [...prev, s]);
      // Prefer an empty pane; otherwise the new session takes the focused
      // pane. Either way it ends up focused (matching the old behavior of
      // a new tab becoming active).
      const empty = paneIds.indexOf(null);
      const slot = empty >= 0 ? empty : focusedPane;
      setPaneIds(prev => {
        const next = [...prev];
        next[slot] = s.id;
        return next;
      });
      setFocusedPane(slot);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [paneIds, focusedPane, clearPendingClose]);

  const handleCloseTab = useCallback((id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    // Double-tap safety: first tap arms the confirm state; second tap on the
    // same × (within 2.5s) actually closes. Tapping a different tab's × resets
    // to that tab. Prevents accidental tab loss from bumping the close button.
    if (pendingCloseId !== id) {
      clearPendingClose();
      setPendingCloseId(id);
      pendingCloseTimerRef.current = window.setTimeout(() => {
        setPendingCloseId(null);
        pendingCloseTimerRef.current = null;
      }, 2500);
      return;
    }
    // Second tap — actually close.
    clearPendingClose();
    void killSession(id).catch(() => { /* will surface as 404 next refresh */ });
    const b = bundlesRef.current.get(id);
    if (b) {
      if (b.reconnectTimer) window.clearTimeout(b.reconnectTimer);
      if (b.detachTouch) b.detachTouch();
      b.alive = false;
      b.ws?.close();
      try { b.webgl?.dispose(); } catch { /* already disposed on context loss */ }
      b.term.dispose();
      bundlesRef.current.delete(id);
    }
    // Drop the closed session's live-title state so a future id reuse starts clean.
    const titleTimer = titlePersistTimersRef.current.get(id);
    if (titleTimer) { window.clearTimeout(titleTimer); titlePersistTimersRef.current.delete(id); }
    pinnedTitlesRef.current.delete(id);
    setLiveTitles(prev => {
      if (!(id in prev)) return prev;
      const { [id]: _drop, ...rest } = prev;
      return rest;
    });
    const remaining = sessions.filter(s => s.id !== id);
    setSessions(remaining);
    // Backfill the freed pane with the first session not already on screen;
    // empty otherwise. Panes showing other sessions are untouched.
    setPaneIds(prev => prev.map(pid => {
      if (pid !== id) return pid;
      // Backfill with the operator's next own shell not already on screen
      // (agent-run sessions never auto-fill a freed pane).
      const replacement = remaining.find(s => !isAgentSession(s) && !prev.includes(s.id));
      return replacement ? replacement.id : null;
    }));
    if (remaining.filter(s => !isAgentSession(s)).length === 0) {
      createSession()
        .then(s => {
          pendingBootstrapRef.current.add(s.id);
          // Preserve any live agent-run sessions alongside the fresh shell.
          setSessions(prev => [...prev.filter(isAgentSession), s]);
          setPaneIds(prev => prev.map((_, i) => (i === 0 ? s.id : null)));
          setFocusedPane(0);
        })
        .catch(err => setError(err instanceof Error ? err.message : String(err)));
    }
  }, [sessions, pendingCloseId, clearPendingClose]);

  // ── tab rename ──────────────────────────────────────────────────────────

  const handleRenameStart = useCallback((id: string, currentLabel: string, e: React.MouseEvent) => {
    // Double-click on a tab label enters rename mode. Stop propagation so
    // the tab-switch click doesn't fire.
    e.stopPropagation();
    setEditingId(id);
    setEditLabel(currentLabel);
  }, []);

  const handleRenameCommit = useCallback((id: string) => {
    const trimmed = editLabel.trim();
    if (trimmed) {
      // A manual rename pins the tab: auto-title stops overwriting it, any live
      // title now showing is dropped, and a pending title-persist is cancelled.
      pinnedTitlesRef.current.add(id);
      const titleTimer = titlePersistTimersRef.current.get(id);
      if (titleTimer) { window.clearTimeout(titleTimer); titlePersistTimersRef.current.delete(id); }
      setLiveTitles(prev => {
        if (!(id in prev)) return prev;
        const { [id]: _drop, ...rest } = prev;
        return rest;
      });
      if (trimmed !== sessions.find(s => s.id === id)?.label) {
        // Optimistic update: apply the label locally immediately, then fire
        // the API call. If the API fails, revert.
        const prevLabel = sessions.find(s => s.id === id)?.label ?? '';
        setSessions(prev => prev.map(s => s.id === id ? { ...s, label: trimmed } : s));
        renameSession(id, trimmed).catch(err => {
          // Revert on failure
          setSessions(prev => prev.map(s => s.id === id ? { ...s, label: prevLabel } : s));
          setError(err instanceof Error ? err.message : String(err));
        });
      }
    }
    setEditingId(null);
    setEditLabel('');
  }, [editLabel, sessions]);

  const handleRenameCancel = useCallback(() => {
    setEditingId(null);
    setEditLabel('');
  }, []);

  // Append text into the contenteditable chat input AND keep React state in
  // sync. Used by the image-attach flow to paste the uploaded file's absolute
  // path. We mutate the DOM directly because the input is contenteditable
  // (uncontrolled by React); setState afterward keeps the data-empty flag,
  // send-mode glyph, and any future read-side state coherent.
  const appendToChatInput = useCallback((text: string) => {
    if (!text) return;
    const el = chatInputRef.current as unknown as HTMLDivElement | null;
    const current = el?.textContent ?? '';
    // Add a space separator if there's already text and it doesn't end in
    // whitespace. Keeps multiple paths / typed context tidy.
    const sep = current.length > 0 && !/\s$/.test(current) ? ' ' : '';
    const next = current + sep + text;
    if (el) {
      el.textContent = next;
      // Move caret to the end so the next keystroke / send picks up cleanly.
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);
    }
    setChatText(next);
  }, []);

  // The attach button just clicks the hidden file input. On mobile, iOS
  // surfaces its native photo-picker (Photo Library / Take Photo or Video /
  // Choose File) thanks to accept="image/*" — no `capture` attribute, because
  // pinning capture to the camera removes the camera-roll option which Nathan
  // needs. On desktop the input is a separate element with a broader accept
  // list (zip/text/markdown/images) and `multiple`.
  const handleAttachImage = useCallback(() => {
    if (uploadStatus === 'uploading') return;
    fileInputRef.current?.click();
  }, [uploadStatus]);

  // Shared upload pipeline for every entry point: file picker (mobile +
  // desktop), desktop drag-and-drop, desktop Ctrl+V screenshot paste.
  // Each file is uploaded to the dashboard (saved under .tmp/claude_uploads/)
  // and its absolute path — double-quoted so spaces/backslashes survive the
  // PTY — is delivered where the user is typing: the chat box on mobile,
  // straight into the terminal prompt on desktop. Claude Code then reads the
  // file off disk (or unzips it) via its own tools.
  const uploadFiles = useCallback(async (files: (File | Blob)[]) => {
    if (files.length === 0) return;
    setUploadError(null);
    setUploadStatus('uploading');
    try {
      for (const f of files) {
        const result = await uploadClaudeFile(f);
        const quoted = `"${result.path}"`;
        if (isMobile) {
          appendToChatInput(quoted);
        } else if (!sendInput(quoted + ' ')) {
          // No live PTY to type into — surface the path in the error toast
          // so the upload isn't silently lost.
          throw new Error(`no live terminal session — uploaded to ${result.path}`);
        }
      }
      setUploadStatus('done');
      if (isMobile) {
        // Refocus the input so the keyboard stays up + caret sits at the end
        // ready for the user to type any context before hitting send.
        requestAnimationFrame(() => {
          const el = chatInputRef.current as unknown as HTMLDivElement | null;
          el?.focus();
        });
      }
      // Clear the "done" badge after a beat so it doesn't linger.
      window.setTimeout(() => {
        setUploadStatus(prev => (prev === 'done' ? 'idle' : prev));
      }, 1800);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : String(err));
      setUploadStatus('error');
      window.setTimeout(() => {
        setUploadStatus(prev => (prev === 'error' ? 'idle' : prev));
      }, 5000);
    }
  }, [appendToChatInput, isMobile, sendInput]);
  uploadFilesRef.current = uploadFiles;

  const handleFileChosen = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    // Reset the input so picking the same file twice in a row still triggers
    // a change event.
    e.target.value = '';
    if (files.length > 0) void uploadFiles(files);
  }, [uploadFiles]);

  // Desktop drag-and-drop onto the card. dragDepthRef counts enter/leave
  // pairs (dragleave fires on every child-boundary crossing).
  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepthRef.current += 1;
    setDragOver(true);
  }, []);
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();  // required, or the browser navigates to the file
  }, []);
  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepthRef.current -= 1;
    if (dragDepthRef.current <= 0) {
      dragDepthRef.current = 0;
      setDragOver(false);
    }
  }, []);
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepthRef.current = 0;
    setDragOver(false);
    const files = Array.from(e.dataTransfer?.files ?? []);
    if (files.length > 0) void uploadFiles(files);
  }, [uploadFiles]);

  // ── render ─────────────────────────────────────────────────────────────────
  return (
    <div style={{
      flex: 1,
      display: 'flex', flexDirection: 'column',
      width: '100%',
      minHeight: 0,
    }}>
      {/* Override xterm.js's bundled CSS:
          - line 95 of xterm.css hardcodes .xterm-viewport { background-color: #000 }
            which overrides theme.background='rgba(0,0,0,0)' — wipe it so the card's
            frosted-glass bg shows through.
          - iOS Safari needs `-webkit-overflow-scrolling: touch` + `touch-action: pan-y`
            on the scroll container before swipes scroll the buffer instead of the page.
          - Hide xterm's scrollbar everywhere — on desktop the browser default renders
            as a bright white track over the frosted-glass card; wheel-scroll replaces
            it. On mobile, momentum touch-scroll replaces it (Nathan, 2026-06-28). */}
      <style>{`
        .xterm, .xterm .xterm-viewport, .xterm .xterm-screen {
          background-color: transparent !important;
        }
        .xterm .xterm-viewport {
          -webkit-overflow-scrolling: touch;
          touch-action: pan-y;
          scrollbar-width: none;
        }
        /* display:none (not width:0) — newer Chrome on Windows otherwise still
           draws a gray fluent overlay scrollbar on hover. */
        .xterm .xterm-viewport::-webkit-scrollbar { display: none; }
        /* xterm also renders its OWN VSCode-style overlay scrollbar — a .slider
           div (cream at 20% alpha, reads as gray) inside .xterm-scrollable-element
           that fades in on hover. It's a real element, not a native scrollbar, so
           the rules above don't touch it. Hide it too; wheel-scroll still works. */
        .xterm .xterm-scrollable-element > .scrollbar { display: none !important; }
        .claude-chat-input { scrollbar-width: none; }
        .claude-chat-input::-webkit-scrollbar { display: none; }
        ${isMobile ? `
          /* Contenteditable placeholder — mimics input::placeholder. The
             [data-empty="true"] flag is flipped from React on every input
             event so the placeholder hides as soon as the user types. */
          .claude-chat-input[data-empty="true"]::before {
            content: attr(data-placeholder);
            color: ${theme.text2};
            opacity: 0.45;
            font-size: 13px;
            pointer-events: none;
          }
        ` : ''}
      `}</style>
      <div
        // Desktop: dropping a file anywhere on the card uploads it for Claude.
        onDragEnter={!isMobile ? handleDragEnter : undefined}
        onDragOver={!isMobile ? handleDragOver : undefined}
        onDragLeave={!isMobile ? handleDragLeave : undefined}
        onDrop={!isMobile ? handleDrop : undefined}
        style={{
        flex: 1,
        display: 'flex', flexDirection: 'column',
        // Mobile: a flex-shrink:0 spacer further down reserves room for the
        // chat group (which is portaled and position:fixed); the terminal
        // gets whatever's left between the tab strip and that spacer.
        minHeight: 0,
        position: 'relative',
        background: 'transparent',
        overflow: 'hidden',
        // Mobile + Agent Manager: the dashboard root is a scrolling document on
        // mobile (no fixed height), so a flex:1 card grows with its content and
        // the agent feed pushes the WHOLE page up/down instead of scrolling
        // internally. Cap the card to the visible viewport (top of screen down
        // to the fixed bottom tab bar) so the agent pane below becomes a real
        // bounded internal-scroll box — header stays put, only the feed scrolls
        // (Nathan, 2026-06-21). Terminal sessions keep the document-flow model.
        ...(isMobile && showAgents
          ? { flex: 'none' as const, height: `calc(100dvh - ${BOTTOM_TAB_BAR_HEIGHT_CSS})` }
          : {}),
      }}>
        {/* Tab strip. On mobile this is the topmost element — pad with the
            iOS status-bar safe-area inset so the tab labels don't slip behind
            the notch / dynamic island. */}
        <div style={{
          display: 'flex', alignItems: 'stretch',
          borderBottom: `1px solid ${theme.border}`,
          // Mobile: frosted + z-raised above the terminal containers. The top
          // overdraw that tucked rows behind this strip was retired 2026-06-13,
          // but the z-index:2 stacking trap still keeps xterm's internal layers
          // from escaping over the strip, and the frost is kept for depth.
          // Desktop keeps the airy 20% wash.
          background: isMobile ? `${theme.bg2}99` : `${theme.bg2}33`,
          ...(isMobile ? {
            position: 'relative' as const,
            zIndex: 2,
            backdropFilter: 'blur(10px)',
            WebkitBackdropFilter: 'blur(10px)',
          } : {}),
          overflowX: 'auto',
          scrollbarWidth: 'thin',
          flexShrink: 0,
          paddingTop: isMobile ? 'env(safe-area-inset-top, 0px)' : 0,
          // The AGENTS tab's "agent alive" box-shadow glow bleeds a few px past
          // its left edge. overflowX:auto clips child shadows at the strip's
          // edges, so as the leftmost tab its left halo was being cut off while
          // the right side (bleeding into the neighbor) showed — an asymmetric
          // glow (Nathan, 2026-06-25). This inset gives the left halo room to
          // render before the clip boundary; the frosted strip bg fills it, so
          // there's no visible gap.
          paddingLeft: 12,
        }}>
          {/* Agent Manager — the leftmost selectable tab and the Claude tab's
              home view (Nathan, 2026-06-25). Opens the non-terminal management
              view (agent recipes, runs, feeds). */}
          <div
            onClick={() => { clearPendingClose(); setShowAgents(true); }}
            title={agentActive
              ? 'Agent Manager — an agent is active'
              : 'Agent Manager — create agents, watch runs'}
            style={{
              display: 'flex', alignItems: 'center', gap: 6,
              padding: isMobile ? '7px 12px' : '8px 14px',
              cursor: 'pointer',
              borderRight: `1px solid ${theme.border}`,
              background: showAgents ? `${accent}14` : (agentActive ? `${accent}0d` : 'transparent'),
              borderBottom: showAgents ? `2px solid ${accent}` : '2px solid transparent',
              fontFamily: theme.fontMono, fontSize: 10,
              // While an agent is alive the label glows in the accent even when
              // the pane isn't open, so the strip reads "active" at a glance.
              color: (showAgents || agentActive) ? accent : theme.text2,
              fontWeight: (showAgents || agentActive) ? 700 : 500,
              letterSpacing: '0.06em',
              // Two-layer text glow so the ≡ AGENTS label itself reads as lit,
              // not just outlined: a tight bright core + a softer wider halo.
              textShadow: agentActive
                ? `0 0 4px ${accent}cc, 0 0 11px ${accent}88`
                : undefined,
              whiteSpace: 'nowrap', flexShrink: 0, userSelect: 'none',
              touchAction: 'manipulation',
              ...agentGlowStyle,
            }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: agentDotColor, flexShrink: 0,
            }}/>
            <span>AGENTS</span>
          </div>
          {/* Tabs: the operator's own shells. Agent runs are not tabs here —
              the running-agent count is the sidebar/bottom-bar badge, and run
              detail lives in the ≡ AGENTS manager tab to their left. */}
          {normalSessions.map(s => {
            // No session tab reads "active" while the Agent Manager is open.
            const on = !showAgents && s.id === activeId;
            // Visible in a non-focused pane of a desktop split — gets a
            // muted version of the active treatment so the strip shows at a
            // glance which sessions are on screen.
            const inPane = !isMobile && !showAgents && layoutCount > 1 && !on && paneIds.includes(s.id);
            const b = bundlesRef.current.get(s.id);
            const dotColor =
              !b || !b.alive       ? theme.red :
              b.status === 'open'  ? theme.green :
              b.status === 'connecting' ? theme.amber :
              theme.text3;
            // Live window title (Claude Code's conversation context + status
            // glyph) wins over the stored label; falls back to it before any
            // title arrives and for manually-renamed (pinned) tabs.
            const displayName = liveTitles[s.id] ?? s.label;
            return (
              <div key={s.id}
                onClick={() => { clearPendingClose(); setShowAgents(false); handleTabClick(s.id); }}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: isMobile ? '7px 10px' : '8px 14px',
                  cursor: 'pointer',
                  borderRight: `1px solid ${theme.border}`,
                  background: on ? `${accent}14` : inPane ? `${accent}0a` : 'transparent',
                  borderBottom: on ? `2px solid ${accent}`
                              : inPane ? `2px solid ${accent}55`
                              : '2px solid transparent',
                  fontFamily: theme.fontMono, fontSize: 10,
                  color: on ? accent : inPane ? theme.text : theme.text2,
                  fontWeight: on ? 700 : inPane ? 600 : 500,
                  letterSpacing: '0.06em',
                  whiteSpace: 'nowrap',
                  flexShrink: 0,
                  userSelect: 'none',
                  // No 350ms double-tap-zoom arbitration on touch — taps
                  // select the tab immediately (dblclick rename still works).
                  touchAction: 'manipulation',
                }}>
                <span style={{
                  width: 6, height: 6, borderRadius: '50%',
                  background: dotColor, flexShrink: 0,
                }}/>
                {(editingId === s.id) ? (
                  <input
                    value={editLabel}
                    onChange={e => setEditLabel(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') handleRenameCommit(s.id);
                      else if (e.key === 'Escape') handleRenameCancel();
                    }}
                    onBlur={() => handleRenameCommit(s.id)}
                    autoFocus
                    onClick={e => e.stopPropagation()}
                    style={{
                      fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
                      letterSpacing: '0.06em',
                      color: accent,
                      background: `${theme.bg0}cc`,
                      border: `1px solid ${accent}`,
                      borderRadius: 3,
                      padding: '2px 6px',
                      width: Math.max(60, (editLabel.length + 2) * 8),
                      outline: 'none',
                    }}
                  />
                ) : (
                  <span
                    onDoubleClick={(e) => handleRenameStart(s.id, displayName, e)}
                    title={`${displayName} — double-click to rename`}
                    style={{
                      maxWidth: isMobile ? 130 : 220,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >{displayName}</span>
                )}
                <span
                  onClick={(e) => handleCloseTab(s.id, e)}
                  title={pendingCloseId === s.id ? 'Tap again to close' : undefined}
                  style={{
                    marginLeft: 4, padding: '0 4px',
                    color: pendingCloseId === s.id ? theme.bg0 : theme.text3,
                    background: pendingCloseId === s.id ? theme.red : 'transparent',
                    cursor: 'pointer',
                    borderRadius: 2,
                    fontWeight: pendingCloseId === s.id ? 700 : 400,
                    transition: 'background 0.15s, color 0.15s',
                  }}
                  onMouseEnter={(e) => {
                    const el = e.currentTarget as HTMLElement;
                    if (pendingCloseId !== s.id) el.style.color = theme.red;
                  }}
                  onMouseLeave={(e) => {
                    const el = e.currentTarget as HTMLElement;
                    if (pendingCloseId !== s.id) el.style.color = theme.text3;
                  }}
                >{pendingCloseId === s.id ? '×?' : '×'}</span>
              </div>
            );
          })}
          <button
            onClick={handleNewTab}
            disabled={busy}
            style={{
              padding: isMobile ? '7px 12px' : '8px 16px',
              background: 'transparent',
              color: theme.text2,
              border: 'none',
              borderRight: `1px solid ${theme.border}`,
              cursor: busy ? 'default' : 'pointer',
              fontFamily: theme.fontMono, fontSize: 13, fontWeight: 700,
              flexShrink: 0,
              opacity: busy ? 0.4 : 1,
            }}>
            +
          </button>
          {/* Desktop refresh — same dual-purpose handler as the mobile tool-row
              ↻. Closes the WS so the standing reconnect re-pulls the focused
              terminal's full history (the server's serialized pyte screen +
              scrollback) AND takes over resize control, sizing the shared PTY to
              this device so history reflows here instead of running off the
              edge. The cure for garbled / out-of-order / wrong-width text. Acts
              on the focused pane (activeId). */}
          {!isMobile && (
            <button
              onClick={handleRefreshTerminal}
              title="Refresh + take control: re-pull history and resize the terminal to this screen"
              aria-label="Refresh terminal and take resize control"
              style={{
                marginLeft: 4,
                padding: '6px 12px',
                background: 'transparent',
                color: theme.text2,
                border: 'none',
                borderRight: `1px solid ${theme.border}`,
                cursor: 'pointer',
                fontFamily: theme.fontMono, fontSize: 14, fontWeight: 700,
                flexShrink: 0,
              }}>
              ↻
            </button>
          )}
          {/* Desktop attach — opens the Windows file picker. Uploads land in
              .tmp/claude_uploads/ and the path is typed into the terminal.
              Drag-and-drop onto the card and Ctrl+V (screenshot) feed the
              same pipeline. */}
          {!isMobile && (
            <button
              onClick={handleAttachImage}
              disabled={uploadStatus === 'uploading'}
              title="Attach a file for Claude — or drag-and-drop onto the terminal, or Ctrl+V a screenshot"
              aria-label="Attach file"
              style={{
                marginLeft: 4,
                padding: '6px 12px',
                background: 'transparent',
                color: theme.text2,
                border: 'none',
                borderRight: `1px solid ${theme.border}`,
                cursor: uploadStatus === 'uploading' ? 'default' : 'pointer',
                fontFamily: theme.fontMono, fontSize: 10, fontWeight: 600,
                letterSpacing: '0.04em',
                flexShrink: 0,
                opacity: uploadStatus === 'uploading' ? 0.4 : 1,
              }}>
              📎 ATTACH
            </button>
          )}
          {/* Desktop upload status — lives inline in the strip (the mobile
              toast is anchored to the portaled chat group, which doesn't
              exist on desktop). */}
          {!isMobile && uploadStatus !== 'idle' && (
            <span style={{
              alignSelf: 'center',
              marginLeft: 8,
              fontFamily: theme.fontMono, fontSize: 10,
              letterSpacing: '0.04em',
              color: uploadStatus === 'error' ? theme.red
                   : uploadStatus === 'done'  ? theme.green
                   : theme.text2,
              whiteSpace: 'nowrap', flexShrink: 0,
            }}>
              {uploadStatus === 'uploading' && 'uploading…'}
              {uploadStatus === 'done' && '✓ path typed into terminal'}
              {uploadStatus === 'error' && (uploadError || 'upload failed')}
            </span>
          )}
          {/* Desktop hidden file input. Shares fileInputRef with the mobile
              input — only one of the two is ever mounted (the mobile one
              lives inside the isMobile-gated portal). Broader accept than
              mobile (which stays image/* to keep the iOS photo picker), and
              `multiple` so a batch drag-select works from the picker too. */}
          {!isMobile && (
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".png,.jpg,.jpeg,.webp,.gif,.zip,.txt,.md,.markdown,.json,.csv,.tsv,.log,.yaml,.yml,.toml,.html,.pdf,image/*"
              onChange={handleFileChosen}
              style={{ display: 'none' }}
            />
          )}
          {/* Settings — far-right gear. Absorbs the launch / font / size / theme
              / split controls that used to sit loose in the strip, plus the
              Restart Dashboard action. Rendered on both desktop and mobile. */}
          <SettingsMenu
            theme={theme}
            accent={accent}
            isMobile={isMobile}
            bootstrapCmd={bootstrapCmd}
            bootstrapOptions={BOOTSTRAP_COMMANDS}
            onBootstrapChange={(v) => {
              setBootstrapCmd(v);
              try { localStorage.setItem('claudeBootstrapCmd', v); } catch { /* quota / private mode */ }
            }}
            termFont={termFont}
            fontOptions={FONT_CHOICES}
            onFontChange={handleFontChange}
            termFontSize={termFontSize}
            fontSizeOptions={FONT_SIZE_CHOICES}
            onFontSizeChange={handleFontSizeChange}
            themeSettings={themeSettings}
            onThemeChange={onThemeChange}
            layoutCount={layoutCount}
            onLayoutChange={handleLayoutChange}
          />
        </div>

        {/* Terminal host — every session gets its own container; xterm cannot
            re-open onto a different element so we never move terminals, only
            show/hide them in place.
            Mobile: position:relative box, containers absolutely positioned
            with overdraw (edge-to-edge, padding 0), exactly one visible.
            Desktop: flex row — visible containers become equal-width flex
            children ordered into their pane slot; hidden ones display:none.
            On mobile we also reserve room above the keyboard for the chat
            box + tool row so they don't sit on top of the terminal output. */}
        <div
          ref={hostRef}
          style={{
            flex: 1,
            position: 'relative',
            width: '100%',
            padding: isMobile ? 0 : 8,
            minHeight: isMobile ? 360 : 0,
            ...(isMobile ? {} : { display: 'flex' as const, flexDirection: 'row' as const, gap: 8 }),
            // Agent Manager open → hide the terminal host (panes stay mounted so
            // sessions survive) and show AgentManagerPane below instead.
            ...(showAgents ? { display: 'none' as const } : {}),
            // No tap-to-toggle-keyboard handler here on purpose: the chat box
            // is the only path to the keyboard now, so a stray tap on the
            // terminal output stays a no-op (and lets iOS long-press select).
            // The chat group floats over the bottom CHAT_AREA_HEIGHT_PX of
            // this host; the spacer below shrinks the available area to match
            // so terminal output never sits under the chat box.
          }}>
          {normalSessions.map(s => {
            const paneIdx = isMobile ? (s.id === activeId ? 0 : -1) : paneIds.indexOf(s.id);
            const visible = paneIdx >= 0;
            const focusedHere = !isMobile && layoutCount > 1 && visible && paneIdx === focusedPane;
            return (
              <div
                key={s.id}
                // Clicking anywhere in a pane focuses it (capture phase so
                // xterm's own mousedown handling still runs afterwards).
                onMouseDownCapture={!isMobile && visible ? () => setFocusedPane(paneIdx) : undefined}
                style={isMobile ? {
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  // Mobile: sits flush below the tab strip (top overdraw
                  // retired 2026-06-13 — it hid Claude Code's first line); the
                  // bottom edge still overdraws ~1 row closer to the chat box.
                  top: -TERM_OVERDRAW_TOP_PX,
                  bottom: -TERM_EXTEND_BOTTOM_PX,
                  display: visible ? 'block' : 'none',
                  // Clip the smooth-scroll translateY on .xterm-screen at the
                  // box edge.
                  overflow: 'hidden',
                  // zIndex 0 (vs the default auto) creates a stacking context,
                  // trapping xterm's internal z-indexes INSIDE this box. xterm
                  // ships invisible layers at z-index 5–11 (helpers, message
                  // overlay, scrollbar slider, canvas layers); with `auto`
                  // those escaped into the page's stacking context and beat
                  // the tab strip's zIndex 2 in the overdraw band — so taps on
                  // the tabs hit-tested into the terminal and selected text
                  // behind the strip (Nathan, 2026-06-10).
                  zIndex: 0,
                } : {
                  // Desktop pane: equal share of the row, slotted into pane
                  // order. minWidth 0 lets the flex item shrink below the
                  // terminal's intrinsic width so fit() controls the columns.
                  flex: '1 1 0%',
                  minWidth: 0,
                  position: 'relative',
                  order: visible ? paneIdx : 0,
                  display: visible ? 'block' : 'none',
                  overflow: 'hidden',
                  zIndex: 0,  // same stacking-context trap as mobile
                  borderRadius: 4,
                  // Focused-pane ring (split view only) — outline, not
                  // border, so it never shifts the terminal's box.
                  outline: focusedHere ? `1px solid ${accent}66` : 'none',
                  outlineOffset: -1,
                }}
                ref={el => ensureBundle(s.id, el)}
              />
            );
          })}
          {/* Empty desktop panes (split view with fewer sessions than slots):
              click to focus, then pick a tab or + to fill it. */}
          {!isMobile && sessions.length > 0 && paneIds.map((pid, i) => (
            pid === null ? (
              <div
                key={`empty-pane-${i}`}
                onMouseDown={() => setFocusedPane(i)}
                style={{
                  flex: '1 1 0%',
                  minWidth: 0,
                  order: i,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  border: `1px dashed ${i === focusedPane ? `${accent}88` : theme.border}`,
                  borderRadius: 4,
                  color: theme.text3,
                  fontFamily: theme.fontMono, fontSize: 10,
                  letterSpacing: '0.08em',
                  textAlign: 'center',
                  cursor: 'pointer',
                  userSelect: 'none',
                }}>
                {i === focusedPane
                  ? 'click a tab or + to open a session here'
                  : 'empty pane — click to focus'}
              </div>
            ) : null
          ))}
          {sessions.length === 0 && (
            <div style={{
              position: 'absolute', inset: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: theme.text3, fontFamily: theme.fontMono, fontSize: 11,
              letterSpacing: '0.08em',
            }}>
              {error ? `error: ${error}` : 'starting session…'}
            </div>
          )}
        </div>

        {/* Agent Manager view — shown in place of the terminal host when the
            AGENTS tab is selected. Kept mounted (display toggle) so its
            selection/scroll survive flipping back to a terminal; polling pauses
            via isActive when it's hidden. */}
        <div style={{
          flex: 1,
          // The card is now height-bounded whenever this pane is shown (see the
          // card's mobile+showAgents cap above, and the desktop app-shell), so
          // flex:1 + minHeight:0 makes this a proper bounded box and the pane's
          // own overflow:auto regions scroll internally instead of the document.
          minHeight: 0,
          overflow: 'hidden',
          display: showAgents ? 'flex' : 'none',
          flexDirection: 'column',
        }}>
          <AgentManagerPane
            theme={theme} accent={accent}
            isMobile={isMobile}
            isActive={isActive && showAgents}
          />
        </div>

        {/* Mobile spacer — reserves vertical room at the bottom of the flex
            column for the chat group. The chat is portaled with
            `bottom: BOTTOM_TAB_BAR_HEIGHT + 30`, so the spacer clears that
            plus the chat height plus a 4px buffer. The terminal containers
            then deliberately reach TERM_EXTEND_BOTTOM_PX past the host's
            bottom edge (Nathan wanted the text zone one row closer to the
            chat box, 2026-06-10), so the last row noses ~10px into the chat
            group's top padding. */}
        {isMobile && !showAgents && (
          <div style={{
            flexShrink: 0,
            height: `calc(${BOTTOM_TAB_BAR_HEIGHT_CSS} + 30px + ${CHAT_AREA_HEIGHT_PX}px + 4px)`,
          }}/>
        )}

        {/* Mobile chat box + tool row.
            Rendered into document.body via React portal so its `position: fixed`
            isn't trapped by the card's backdrop-filter (a known WebKit bug:
            backdrop-filter ancestors create a containing block for fixed
            descendants, so `bottom: X` ends up relative to the card's rendered
            bottom rather than the viewport). Portaling lifts the chat out of
            that ancestor entirely.

            Always mounted AND always visible on mobile so the input + tools
            are reachable without first summoning the keyboard. Tapping the
            input is what opens the keyboard.
            Key implementation details:
            • The input is a CONTENTEDITABLE div, not a textarea — older iOS
              versions suppressed the form-assistant bar for contenteditable.
            • Rests 30px + safe-area-inset-bottom above the bottom tab bar
              when the keyboard is down; sits flush above the keyboard when
              it's up. `bottom` is JS-driven via `useChatBottomOffset` from
              `window.visualViewport` (resize + scroll). iOS 26 Safari has
              no CSS-only path for this; see synth note dated 2026-05-06.
            • inputMode="text" hints iOS to render the standard QWERTY. */}
        {isMobile && isActive && !showAgents && createPortal(
          <div
            ref={chatGroupRef}
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'fixed',
              left: 8, right: 8,
              bottom: chatBottom,
              zIndex: 30,
              // Translucent rounded container with strong backdrop blur — the
              // terminal output is what shows through.
              background: `${theme.bg2}33`,
              border: `1px solid ${theme.border}`,
              borderRadius: 18,
              backdropFilter: 'blur(28px) saturate(1.4)',
              WebkitBackdropFilter: 'blur(28px) saturate(1.4)',
              padding: '8px 10px',
              display: 'flex', flexDirection: 'column', gap: 4,
              boxShadow: '0 -2px 24px rgba(0,0,0,0.35)',
            }}>
            {/* Contenteditable input. fontSize ≥ 16 prevents iOS auto-zoom
                on focus (contenteditable is NOT exempt — Safari zooms it
                the same as <input>/<textarea>). Empty-state placeholder is
                rendered via the ::before rule in the global <style> block
                above (data-empty="true"). */}
            <div
              ref={chatInputRef as unknown as React.RefObject<HTMLDivElement>}
              contentEditable
              suppressContentEditableWarning
              role="textbox"
              aria-multiline="true"
              aria-label="Chat with Claude"
              data-empty={chatText.length === 0 ? 'true' : 'false'}
              data-placeholder="Chat with Claude"
              inputMode="text"
              autoCapitalize="sentences"
              spellCheck={true}
              onBlur={() => setPreLift(false)}
              onPointerDown={(e) => {
                // PRE-LIFT, synchronous edition. Letting the chatFocused
                // re-render lift the box is too late — iOS samples the
                // focused element's position when focus lands, and if the
                // box still sits in the keyboard's landing zone it pushes
                // the whole app frame up (the push that, on dismissal, iOS
                // mis-restores into the shrunk-frame bug). So when the tap
                // arrives on an unfocused input: block the native focus,
                // move the container above the keyboard's future position
                // directly in the DOM (same event, zero frames later, +16px
                // cushion in case the remembered height is a little short),
                // and only then focus — preventScroll stops WebKit's own
                // scroll-into-view on top. When already focused, default
                // behavior stands so taps keep placing the caret normally.
                const el = e.currentTarget as HTMLDivElement;
                if (document.activeElement === el) return;
                e.preventDefault();
                const container = el.parentElement;
                const oldRect = container?.getBoundingClientRect();
                if (container) {
                  container.style.bottom = `${readLastKbdPx() + 16}px`;
                  void container.getBoundingClientRect();
                }
                // Keep React's rendered position in agreement with the manual
                // lift above (and disarm via timeout if no keyboard ever
                // shows — e.g. a hardware keyboard is attached).
                setPreLift(true);
                window.setTimeout(() => setPreLift(false), 2500);
                el.focus({ preventScroll: true });
                // On release, a SHORT tap (under ~2s — past that iOS skips
                // the synthesis, which is why press-and-hold behaved) fires
                // a family of ghost events at the point the finger left:
                // touchend, then synthesized mousedown/mouseup/click. The
                // box has already moved away from there, so they'd hit the
                // terminal underneath, which grabs focus on mousedown and
                // drops the keyboard as fast as it rose. Hit-testing is the
                // clean choke point: park an invisible shield over the
                // box's OLD rect so every release-path event lands on it
                // and dies. Removed on a timer well past any synthesis.
                if (oldRect) {
                  const shield = document.createElement('div');
                  shield.setAttribute('aria-hidden', 'true');
                  shield.style.cssText =
                    `position:fixed;left:${oldRect.left}px;top:${oldRect.top}px;` +
                    `width:${oldRect.width}px;height:${oldRect.height}px;` +
                    'z-index:9999;background:transparent;';
                  const swallow = (ev: Event) => {
                    ev.preventDefault();
                    ev.stopPropagation();
                  };
                  for (const type of [
                    'touchstart', 'touchmove', 'touchend',
                    'pointerdown', 'pointerup',
                    'mousedown', 'mouseup', 'click',
                  ]) shield.addEventListener(type, swallow, { passive: false });
                  document.body.appendChild(shield);
                  window.setTimeout(() => shield.remove(), 800);
                }
                // Backstop: if some ghost event slips past the shield and
                // steals focus anyway, take it back — once. The window is
                // too short for an intentional blur to be plausible.
                let refocused = false;
                const onBlurGuard = () => {
                  if (refocused) return;
                  refocused = true;
                  window.setTimeout(() => el.focus({ preventScroll: true }), 0);
                };
                el.addEventListener('focusout', onBlurGuard);
                window.setTimeout(
                  () => el.removeEventListener('focusout', onBlurGuard),
                  900,
                );
                // Caret to the end of any existing draft (preventDefault
                // suppressed the native caret placement).
                const sel = window.getSelection();
                if (sel) {
                  const range = document.createRange();
                  range.selectNodeContents(el);
                  range.collapse(false);
                  sel.removeAllRanges();
                  sel.addRange(range);
                }
              }}
              onInput={(e) => {
                const txt = (e.currentTarget as HTMLDivElement).textContent ?? '';
                setChatText(txt);
              }}
              onKeyDown={(e) => {
                // Enter → send, Shift+Enter → newline.
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                  return;
                }
                // Backspace overflow: when the chat box is empty, the next
                // backspace goes to the terminal so a user can erase text in
                // the running shell / Claude prompt without re-focusing the
                // terminal first. \x7f is the DEL byte xterm.js itself sends
                // for the Backspace key, so the receiving PTY treats it
                // identically to a "real" terminal backspace. We read the
                // live DOM (not chatText state) to dodge any stale-closure
                // race when keystrokes come in faster than React batches.
                if (e.key === 'Backspace') {
                  const live = (e.currentTarget as HTMLDivElement).textContent ?? '';
                  if (live.length === 0) {
                    e.preventDefault();
                    sendInput('\x7f');
                  }
                }
              }}
              className="claude-chat-input"
              style={{
                width: '100%',
                minHeight: 22,
                maxHeight: 96,
                overflowY: 'auto',
                padding: '4px 4px 2px',
                fontFamily: theme.fontMono, fontSize: 16,
                lineHeight: 1.3,
                color: theme.text,
                background: 'transparent',
                border: 'none',
                outline: 'none',
                WebkitAppearance: 'none',
                boxSizing: 'border-box',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                touchAction: 'manipulation',
              }}
            />

            {/* Tools + send row — every button is flex:1 so the row fills the
                available width without overflow regardless of viewport. No
                spacer between tools and send: the send is just the rightmost
                slot, sized identically to its neighbors. */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
              <ToolBtn glyph="ESC" onClick={() => sendInput(KEYS.ESC)}   theme={theme}/>
              <ToolBtn glyph="/"   onClick={() => sendInput(KEYS.SLASH)} theme={theme}/>
              <ToolBtn glyph="↻"   onClick={handleRefreshTerminal}       theme={theme}/>
              <ToolBtn glyph="↑"   onClick={() => sendInput(KEYS.UP)}    theme={theme}/>
              <ToolBtn glyph="↓"   onClick={() => sendInput(KEYS.DOWN)}  theme={theme}/>
              <ToolBtn glyph="←"   onClick={() => sendInput(KEYS.LEFT)}  theme={theme}/>
              <ToolBtn glyph="→"   onClick={() => sendInput(KEYS.RIGHT)} theme={theme}/>
              <ToolBtn
                glyph="+"
                onClick={handleAttachImage}
                theme={theme}
                disabled={uploadStatus === 'uploading'}
              />
              <button
                onClick={handleSend}
                onMouseDown={e => e.preventDefault()}
                onTouchStart={e => e.stopPropagation()}
                aria-label={chatText.length > 0 ? 'send text' : 'send enter'}
                style={{
                  flex: 1, minWidth: 0,
                  height: 40,
                  borderRadius: 10,
                  background: 'transparent',
                  color: theme.text,
                  border: 'none',
                  // Heavier + larger than the tool glyphs so the send action
                  // reads as the distinct primary control of the row.
                  fontSize: 28, fontWeight: 900,
                  lineHeight: 1,
                  cursor: 'pointer',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  WebkitTapHighlightColor: 'transparent',
                  opacity: 1,
                }}>
                {chatText.length > 0 ? '↑' : '⏎'}
              </button>
            </div>

            {/* Hidden file input — anchored inside the chat container so it
                participates in the same iOS focus/security context as the
                contenteditable. Tapping the + button calls .click() on this
                element to summon the photo picker. */}
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              onChange={handleFileChosen}
              style={{
                position: 'absolute',
                width: 1, height: 1,
                opacity: 0, pointerEvents: 'none',
                left: -9999, top: -9999,
              }}
            />

            {/* Upload toast — same slot as the old "coming soon" notice. Color
                + copy depend on uploadStatus so a single block covers all
                three transient states. The 'idle' branch renders nothing. */}
            {uploadStatus !== 'idle' && (
              <div style={{
                position: 'absolute',
                left: 8, right: 8, bottom: '100%',
                marginBottom: 6,
                padding: '6px 10px',
                background: theme.bg2,
                border: `1px solid ${
                  uploadStatus === 'error' ? theme.red + '88' : theme.border
                }`,
                borderRadius: 6,
                fontFamily: theme.fontMono, fontSize: 10,
                color: uploadStatus === 'error' ? theme.red : theme.text2,
                textAlign: 'center',
                pointerEvents: 'none',
              }}>
                {uploadStatus === 'uploading' && 'uploading…'}
                {uploadStatus === 'done' && 'attached — path pasted into chat'}
                {uploadStatus === 'error' && (uploadError || 'upload failed')}
              </div>
            )}
          </div>,
          document.body
        )}

        {/* Desktop drag-and-drop overlay — pointerEvents:none so the drop
            event lands on the container underneath, not the overlay. */}
        {!isMobile && dragOver && (
          <div style={{
            position: 'absolute', inset: 10, zIndex: 40,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: `${theme.bg0}b3`,
            border: `2px dashed ${accent}`,
            borderRadius: 10,
            pointerEvents: 'none',
            fontFamily: theme.fontMono, fontSize: 12, fontWeight: 600,
            letterSpacing: '0.08em',
            color: accent,
          }}>
            drop to upload for Claude
          </div>
        )}

        {error && sessions.length > 0 && (
          <div style={{
            padding: '8px 14px',
            background: `${theme.red}14`,
            borderTop: `1px solid ${theme.red}55`,
            fontFamily: theme.fontMono, fontSize: 10, color: theme.red,
            letterSpacing: '0.04em',
          }}>
            {error}
          </div>
        )}
      </div>
    </div>
  );
}

// White-symbol tool button used in the mobile unified chat group's tools row.
// Borderless, transparent-background — sized via flex:1 so all 9 buttons in
// the row (8 tools + send) share the available width equally and the row
// never overflows the chat container regardless of phone width.
function ToolBtn({ glyph, onClick, theme, danger, disabled }: {
  glyph: string; onClick: () => void; theme: Theme; danger?: boolean; disabled?: boolean;
}) {
  const color = danger ? theme.red : theme.text;
  // Multi-character labels (ESC, TAB, ^C) use a smaller font so they don't
  // blow out the box; single glyphs (arrows, +) read large and bold. The
  // unicode left/right arrows (← →) render visually thinner than ↑ ↓ in
  // most monospace fonts, so we render the same ↑ glyph rotated ±90° for
  // left/right — all four arrows then share weight and shape exactly.
  const isMulti = glyph.length > 1;
  const isPlus = glyph === '+';
  // "/" is a full-height ascender-to-descender stroke — at the 26px single-
  // glyph size it towers over the 16px ESC/^C labels, so it gets its own size.
  const fontSize = isMulti ? 16 : isPlus ? 38 : glyph === '/' ? 18 : 26;
  const rotate = glyph === '←' ? -90 : glyph === '→' ? 90 : 0;
  const renderedGlyph = rotate !== 0 ? '↑' : glyph;
  return (
    <button
      onClick={onClick}
      onMouseDown={e => e.preventDefault()}
      onTouchStart={e => e.stopPropagation()}
      aria-label={glyph}
      disabled={disabled}
      style={{
        flex: 1, minWidth: 0,
        height: 40,
        padding: 0,
        fontFamily: theme.fontMono,
        fontSize, fontWeight: 700,
        background: 'transparent',
        color,
        border: 'none',
        borderRadius: 10,
        cursor: disabled ? 'default' : 'pointer',
        whiteSpace: 'nowrap',
        WebkitTapHighlightColor: 'transparent',
        touchAction: 'manipulation',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        opacity: disabled ? 0.3 : 0.6,
      }}>
      <span style={{
        display: 'inline-block',
        transform: rotate ? `rotate(${rotate}deg)` : undefined,
        lineHeight: 1,
      }}>{renderedGlyph}</span>
    </button>
  );
}
