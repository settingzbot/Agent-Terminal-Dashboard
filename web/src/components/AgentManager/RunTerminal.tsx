// Embedded xterm.js terminal that ATTACHES to an already-running pty-manager
// session, so an operator can watch -- and intervene in -- a live autonomous
// agent run. The run is hosted as a normal tagged terminal session, so this is
// the same xterm+WS plumbing as ClaudeTerminalCard, minus everything that card
// carries for the multi-pane Claude tab: no tab strip, no split panes, no
// WebGL renderer, no SgrBgRemapper chip-recolor, no mobile chat box, no
// momentum touch-scroll. One pane, one session, one socket.
//
// WS protocol (mirrored exactly from ClaudeTerminalCard / routes_ws.py):
//   server -> <binary frame>       output bytes (raw UTF-8 PTY bytes; we opt in
//                                  to binary framing via ?bin=1 in the URL --
//                                  terminalWsUrl appends it)
//   server -> {type:'out', data}   legacy JSON output framing (pre-binary
//                                  dashboard that ignored ?bin=1; still handled)
//   server -> {type:'exit'}        PTY closed -- mark ended, stop, don't reconnect
//   client -> {type:'in', data}    keystrokes from term.onData
//   client -> {type:'resize',rows,cols}
//   client -> {type:'flow'}        on open: we support ACK flow control
//   client -> {type:'ack', bytes}  delta of processed payload bytes
//
// Flow control: the server pauses the PTY upstream once too many output bytes
// are in flight and only resumes on our ACKs. Each output chunk goes through
// term.write(chunk, cb); the cb fires once xterm has parsed it. We ACK every
// ~100KB processed, plus a drain-flush when the write queue catches up -- that
// drain-flush is what guarantees the in-flight count converges to zero after a
// burst, so the server never stays paused forever. ACK unit is RAW UTF-8 byte
// length of the payload: byteLength for binary frames, TextEncoder bytes for
// legacy JSON 'out' frames -- the server counts the identical quantity in both.

import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { type Theme } from '../../theme';
import { terminalWsUrl } from '../../api/terminal';

// ACK granularity for terminal output. Above the server's low watermark so a
// single ack can drop the in-flight count below it, below the high watermark so
// acks fire well before the window fills. (Same constant as ClaudeTerminalCard.)
const FLOW_ACK_EVERY_BYTES = 100_000;
const utf8Encoder = new TextEncoder();

// Reconnect backoff bounds -- start fast, cap so we don't hammer a flapping
// backend.
const RECONNECT_START_MS = 500;
const RECONNECT_MAX_MS = 5_000;

type ConnStatus = 'connecting' | 'open' | 'closed';

type Props = {
  sessionId: string;   // pty-manager session id -- the run record's `session_id`
  theme: Theme;
  accent: string;      // hex like "#e3b363"
  alive: boolean;      // run still running? when false, show a hint + no reconnect
  isMobile?: boolean;
};

// Imperative handle so the parent's "Latest" button can snap the viewport to
// the bottom of the scrollback (xterm's native scrollToBottom).
export type RunTerminalHandle = { scrollToBottom: () => void };

export const RunTerminal = forwardRef<RunTerminalHandle, Props>(function RunTerminal(
  { sessionId, theme, accent, alive, isMobile = false }, ref,
) {
  // Status drives the little dot + label in the header. It's the only piece of
  // terminal state React needs to render; everything else lives in refs so a
  // re-render never recreates the Terminal / FitAddon / WebSocket.
  const [status, setStatus] = useState<ConnStatus>('connecting');

  // Long-lived instances, created once per mount. Kept in refs (not state) so
  // React re-renders are pure UI -- the imperative xterm/WS objects survive
  // untouched. xterm's one-time .open(el) means a Terminal cannot be moved
  // between containers, so it must outlive every render.
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const roRef = useRef<ResizeObserver | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectMsRef = useRef<number>(RECONNECT_START_MS);
  // `alive` and `accent`/`theme` can change across renders, but the connect
  // closure is built once (inside the mount effect). Mirror the latest values
  // into refs so that closure always reads current truth instead of a stale
  // capture -- same pattern ClaudeTerminalCard uses for its bootstrap command.
  const aliveRef = useRef(alive);
  // `disposed` guards every async callback (write callbacks, ws handlers,
  // reconnect timer) so a late event after unmount can't touch a torn-down
  // term or call setState on a gone component.
  const disposedRef = useRef(false);

  useEffect(() => { aliveRef.current = alive; }, [alive]);

  // Snap the viewport to the newest output. Safe to call any time -- a no-op if
  // the terminal is mid-build or already disposed.
  useImperativeHandle(ref, () => ({
    scrollToBottom() {
      try { termRef.current?.scrollToBottom(); } catch { /* not ready / disposed */ }
    },
  }), []);

  // -- live re-theme: repaint open terminal when theme/accent changes ---------
  // options.theme is xterm's documented runtime setter -- it repaints in place,
  // so existing scrollback flips palette along with new output.
  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    try {
      term.options.theme = buildXtermTheme(theme, accent);
      term.refresh(0, term.rows - 1);
    } catch { /* term may already be disposed */ }
  }, [theme, accent]);

  // -- mount: build the terminal + open the socket exactly once ---------------
  useEffect(() => {
    const el = hostRef.current;
    if (!el) return;
    disposedRef.current = false;

    const term = new Terminal({
      fontFamily: '"JetBrains Mono", Menlo, Consolas, monospace',
      fontSize: isMobile ? 12 : 13,
      // Tight line-height on mobile so block-character art (Claude Code's
      // banner, box-drawing UIs) stacks flush; 1.2 leaves a gap that mangles it.
      lineHeight: isMobile ? 1.0 : 1.2,
      cursorBlink: true,
      cursorStyle: 'block',
      // Visible rows are painted per frame regardless of depth, so scrollback
      // costs memory (circular buffer), not render time.
      scrollback: 5000,
      allowProposedApi: false,
      // Let xterm's renderer skip its solid background fill so the card bg /
      // constellation shows through (paired with the transparent-bg <style>).
      allowTransparency: true,
      theme: buildXtermTheme(theme, accent),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(el);
    try { fit.fit(); } catch { /* container not sized yet -- ResizeObserver fixes */ }
    termRef.current = term;
    fitRef.current = fit;

    // Input guards on xterm's hidden textarea -- mobile keyboards otherwise
    // autocapitalize/autocorrect keystrokes routed through it, silently
    // rewriting shell input (`Git` for `git`, curly quotes). No-ops on desktop.
    // term.textarea is only set once open() has run.
    const ta = term.textarea;
    if (ta) {
      ta.setAttribute('autocapitalize', 'off');
      ta.setAttribute('autocorrect', 'off');
      ta.setAttribute('autocomplete', 'off');
      ta.setAttribute('spellcheck', 'false');
    }

    // Pipe keystrokes and local resizes to whatever socket is currently open.
    // Bound once on the Terminal (which outlives reconnects); they read the
    // live socket off the ref each time, so a reconnect transparently
    // re-targets them at the new ws.
    term.onData((data) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'in', data }));
      }
    });
    term.onResize(({ rows, cols }) => {
      const ws = wsRef.current;
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', rows, cols }));
      }
    });

    const setConnStatus = (s: ConnStatus) => {
      if (!disposedRef.current) setStatus(s);
    };

    // -- one connection attempt; reattaches reset their own flow window -------
    const connect = () => {
      if (disposedRef.current) return;
      setConnStatus('connecting');

      const ws = new WebSocket(terminalWsUrl(sessionId));
      // Output arrives as binary WS frames (raw UTF-8 PTY bytes via ?bin=1).
      // arraybuffer (not the default 'blob') so ev.data is synchronously
      // readable in onmessage -- Blob forces an async read that would reorder
      // chunks relative to text control frames.
      ws.binaryType = 'arraybuffer';
      wsRef.current = ws;

      // Flow-control counters are local to this connect() closure so a fresh
      // attach resets the ACK window automatically (the server's in-flight
      // counter is likewise per-WS). Stale write callbacks from a dead
      // connection close over the OLD ws and are dropped by the readyState
      // guard below, so they can't pollute a new connection.
      let receivedBytes = 0;   // payload bytes handed to term.write()
      let processedBytes = 0;  // payload bytes xterm has finished processing
      let unackedBytes = 0;    // processed but not yet ACKed to the server

      // Fires once xterm has actually parsed a chunk -- the "processed" signal
      // the ACK window is built on. Counted in WIRE bytes (the server's unit).
      const noteProcessed = (chunkBytes: number) => {
        processedBytes += chunkBytes;
        unackedBytes += chunkBytes;
        // ACK every ~100KB, PLUS a drain-flush whenever the write queue catches
        // up (processed === received). The drain-flush guarantees the server's
        // in-flight count converges to zero after a burst -- without it a
        // residual smaller than the ACK threshold could sit above the server's
        // low watermark forever and deadlock a paused stream.
        if (
          unackedBytes > 0 &&
          (unackedBytes >= FLOW_ACK_EVERY_BYTES || processedBytes === receivedBytes) &&
          ws.readyState === WebSocket.OPEN
        ) {
          ws.send(JSON.stringify({ type: 'ack', bytes: unackedBytes }));
          unackedBytes = 0;
        }
      };

      // Shared output path for both wire formats. chunkBytes is the raw UTF-8
      // byte length of the payload -- the ACK unit the server counts in.
      const writeChunk = (chunk: string | Uint8Array, chunkBytes: number) => {
        receivedBytes += chunkBytes;
        term.write(chunk, () => noteProcessed(chunkBytes));
      };

      // Reset xterm before each (re)connect so the server's screen-state replay
      // doesn't pile on top of the prior visible state and visually duplicate.
      term.reset();

      ws.onopen = () => {
        if (disposedRef.current) return;
        setConnStatus('open');
        reconnectMsRef.current = RECONNECT_START_MS;
        // Send current dimensions so the shell prompt wraps correctly from the
        // start, then declare ACK flow-control support BEFORE any output
        // arrives -- the server only arms its pause logic after this hello (an
        // old routes_ws.py simply ignores the unknown type).
        ws.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
        ws.send(JSON.stringify({ type: 'flow' }));
      };

      ws.onmessage = (ev: MessageEvent<unknown>) => {
        const data = ev.data;
        // Binary frame = terminal output, raw UTF-8 bytes -- straight into
        // xterm (it decodes Uint8Array as UTF-8, statefully across writes). The
        // wire byte length is exactly what the server counted, so it's our ACK
        // unit too.
        if (data instanceof ArrayBuffer) {
          const bytes = new Uint8Array(data);
          writeChunk(bytes, bytes.byteLength);
          return;
        }
        // Defensive: some configs/proxies deliver binary as Blob despite
        // binaryType='arraybuffer'. Read it async and write once resolved.
        if (typeof Blob !== 'undefined' && data instanceof Blob) {
          void data.arrayBuffer().then((buf) => {
            if (disposedRef.current || ws.readyState === WebSocket.CLOSED) return;
            const bytes = new Uint8Array(buf);
            writeChunk(bytes, bytes.byteLength);
          });
          return;
        }
        // Text frame = JSON control message -- or a legacy {type:'out'} output
        // chunk from a pre-binary dashboard that ignored our ?bin=1 opt-in.
        if (typeof data !== 'string') return;
        try {
          const msg: unknown = JSON.parse(data);
          if (!isRecord(msg)) return;
          if (msg.type === 'out' && typeof msg.data === 'string') {
            writeChunk(msg.data, utf8Encoder.encode(msg.data).length);
          } else if (msg.type === 'exit') {
            // PTY closed -- terminal state, no reconnect.
            aliveRef.current = false;
            term.write('\r\n\x1b[2;37m[session exited]\x1b[0m\r\n');
            try { ws.close(); } catch { /* already closing */ }
          }
        } catch { /* malformed -- ignore */ }
      };

      ws.onerror = () => { /* onclose follows; reconnect decision lives there */ };

      ws.onclose = (ev: CloseEvent) => {
        if (wsRef.current === ws) wsRef.current = null;
        setConnStatus('closed');
        if (disposedRef.current) return;
        // 4404 = "session not found / dead" from our handler -- the session is
        // gone on the backend, so don't reconnect.
        if (ev.code === 4404) {
          aliveRef.current = false;
          term.write('\r\n\x1b[2;37m[session no longer available]\x1b[0m\r\n');
          return;
        }
        // A run that's no longer running (or that emitted {type:'exit'}) is
        // terminal -- leave the last frame on screen, don't reattach.
        if (!aliveRef.current) return;
        // Unexpected drop on a live run -> reconnect with backoff.
        reconnectTimerRef.current = window.setTimeout(connect, reconnectMsRef.current);
        reconnectMsRef.current = Math.min(reconnectMsRef.current * 2, RECONNECT_MAX_MS);
      };
    };

    // Only auto-connect a live run. A dead run still builds the terminal (so
    // any future replay could paint) but waits -- the header shows the ended
    // hint instead of a spinner.
    if (aliveRef.current) {
      connect();
    } else {
      setConnStatus('closed');
    }

    // Refit on any host-size change (window resize, mobile rotation, the
    // surrounding card growing/shrinking). fit.fit() can throw if the element
    // is momentarily 0-sized -- swallow it; the next tick recovers.
    const ro = new ResizeObserver(() => {
      try { fit.fit(); } catch { /* not sized yet */ }
    });
    ro.observe(el);
    roRef.current = ro;

    // -- teardown: no leaks. Order matters -- null ws handlers FIRST so a late
    // close fired by ws.close() can't run reconnect/setState against a
    // torn-down component. Each step is guarded so one failure can't skip the
    // rest. ----------------------------------------------------------------
    return () => {
      disposedRef.current = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      try { roRef.current?.disconnect(); } catch { /* never observed */ }
      roRef.current = null;
      const ws = wsRef.current;
      if (ws) {
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        try { ws.close(); } catch { /* already closing */ }
      }
      wsRef.current = null;
      try { term.dispose(); } catch { /* already disposed */ }
      termRef.current = null;
      fitRef.current = null;
    };
    // Build-once: the socket/terminal are created on mount and live for the
    // life of the session id. sessionId changing is effectively a different run
    // and should remount (the parent keys on it), but include it so a stale
    // closure can't attach to the wrong session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, isMobile]);

  const dot = status === 'open' ? theme.green : status === 'connecting' ? theme.amber : theme.text3;
  const label = !alive
    ? 'SESSION ENDED'
    : status === 'open'
      ? 'LIVE'
      : status === 'connecting'
        ? 'CONNECTING...'
        : 'SESSION ENDED';

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* Override xterm.js's bundled CSS -- line 95 of xterm.css hardcodes
          .xterm-viewport { background-color: #000 }, which would paint over our
          transparent theme background and hide the card's frosted-glass bg. */}
      <style>{`
        .xterm, .xterm .xterm-viewport, .xterm .xterm-screen {
          background-color: transparent !important;
        }
      `}</style>

      {/* Thin status row: colored dot + mono label. */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 7,
          fontFamily: theme.fontMono,
          fontSize: 10,
          letterSpacing: '0.04em',
          color: theme.text2,
          padding: '6px 10px',
          borderBottom: `1px solid ${theme.border}`,
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            background: dot,
            flexShrink: 0,
          }}
        />
        <span>{label}</span>
      </div>

      {/* Terminal host fills the rest. */}
      <div
        ref={hostRef}
        style={{ flex: 1, minHeight: 0, position: 'relative', overflow: 'hidden' }}
      />
    </div>
  );
});

// Narrowing helper so JSON.parse output (typed `unknown`) can be inspected
// without `any` -- strict mode rejects bare property access on unknown.
function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null;
}

// Build an xterm theme object from the dashboard theme + accent. A subset of
// ClaudeTerminalCard's xtermTheme -- same transparency trick (bg0's RGB channels
// with alpha 00 rather than rgba(0,0,0,0), so xterm's renderer painting an
// opaque background rectangle for dim cells comes out as the page's own base
// color instead of a black box), semantic colors mapped to dashboard tokens.
function buildXtermTheme(theme: Theme, accent: string) {
  return {
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
    white: theme.text,
    brightBlack: theme.text3,
    brightRed: theme.red,
    brightGreen: theme.green,
    brightYellow: theme.amber,
    brightBlue: theme.blue,
    brightWhite: theme.text,
  };
}
