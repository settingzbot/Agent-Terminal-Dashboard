"""
WebSocket routes for the Claude Code Dashboard.

Endpoints
---------
/agents            — agent-panel live push
/terminal/{session_id} — bidirectional PTY pipe for xterm.js
"""

import asyncio
import contextlib
import json
import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from server.state import ws_mgr
from daemons import agent_client
from daemons import terminal_client

# ── Paths ────────────────────────────────────────────────────────────────────
BOT_DIR = Path(__file__).resolve().parent.parent

# ── Router ───────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/ws", tags=["websocket"])

# ── Terminal output flow control (Phase 2, 2026-06-10) ──────────────────────
# The browser ACKs terminal output as xterm.js actually PROCESSES it (every
# ~100KB through term.write callbacks, plus a drain-flush when its write queue
# catches up). We track bytes-in-flight (sent minus ACKed). The unit is the
# raw UTF-8 byte length of the output payload, identical on both ends in both
# wire formats:
#   binary frames (?bin=1) — the frame IS the payload bytes; server counts
#     len(frame), client counts ArrayBuffer.byteLength of the same frame.
#   JSON text frames (legacy) — server counts len(text.encode('utf-8')) of
#     the "data" string, client counts TextEncoder bytes of the identical
#     string (JSON round-trips it exactly).
# The two formats also agree with EACH OTHER: queue items arrive as
# str.encode('utf-8', errors='replace') output from trident_terminal, so the
# legacy path's decode→re-encode is lossless and len(item) == len(text
# .encode('utf-8')). Above the high watermark we stop forwarding and tell the
# pty-manager to pause ConPTY reads; below the low watermark we resume. This
# keeps a `yes`-style burst from freezing the browser, while input (keystrokes,
# Ctrl-C) flows through the receive loop completely ungated.
FLOW_HIGH_WATER = 400_000   # bytes in flight → pause upstream
FLOW_LOW_WATER = 50_000     # bytes in flight → resume upstream


# ── Endpoints ────────────────────────────────────────────────────────────────

# ── /ws/agents — agent-panel live push (#151) ────────────────────────────────
# On connect (and on every change-epoch advance / backstop), push the snapshot
# in TWO frames: the CHEAP half first (runs, pulse, watch_gate, supervised —
# in-memory, microseconds) so the clickable agent cards render instantly, then
# the GH half (pill_counts + loop_health — ~8 `gh` calls) a beat later. The
# frontend hook merges per-section, so cheap+gh reconstructs the full snapshot
# with no duplicated keys. Before this split the panel waited 3-5s (the cold gh
# reads) before ANYTHING rendered (#151 perf follow-up).
#
# The manager keeps the gh half warm while the panel is open (every gh-bearing
# request arms its snapshot-warm loop), so the gh frame is usually sub-second
# too — the cheap-first split is the cold-start / first-frame guarantee.
#
# Epoch watch: a cheap in-memory counter bumped on every agent-visible mutation
# (no GitHub calls). We push when it advances, plus a slow backstop so a missed
# change or a reconnected stream self-heals. Manager blips are tolerated:
# last-known state is held and pushing resumes once the manager recovers.

_AGENT_EPOCH_POLL_S = 1.0       # how often we check the change-epoch
_AGENT_BACKSTOP_S = 20          # push at least this often regardless of epoch


def _snap_epoch(snap: dict) -> int:
    """Read the change-epoch out of a snapshot's pulse section (0 if absent).
    Only the cheap frame carries ``pulse``; the gh frame has no epoch."""
    return int((snap.get("pulse") or {}).get("epoch") or 0)


@router.websocket("/agents")
async def ws_agents(ws: WebSocket):
    await ws_mgr.accept_agent(ws)
    # ── per-connection state ──────────────────────────────────────────────
    last_epoch: int = -1          # -1 ensures the first poll sends a snapshot
    last_push_time: float = 0.0   # monotonic seconds of the last successful push

    async def _push_half(sections: str) -> "dict | None":
        """Fetch one snapshot half and send it as a frame. Returns the snapshot
        dict on success, or None if the manager was unreachable / returned an
        empty snapshot (so the caller holds last-known state). A send failure
        (client disconnected) propagates so the outer loop tears down cleanly."""
        try:
            snap = await agent_client.manager.get_snapshot(sections=sections)
        except Exception:
            return None  # manager blip — skip this frame, keep last-known
        if not snap:
            return None
        await ws.send_text(json.dumps(snap))  # raises on client disconnect
        return snap

    async def _push_snapshot(now_s: float) -> None:
        """Push the cheap half (instant cards) then the gh half (pills/loop).
        Advances last_epoch / last_push_time off the cheap frame's pulse."""
        nonlocal last_epoch, last_push_time
        cheap = await _push_half("cheap")
        if cheap is not None:
            last_push_time = now_s
            # Re-read the epoch from the snapshot's pulse so last_epoch stays in
            # sync even across manager restarts (epoch resets to 0 on respawn).
            last_epoch = max(last_epoch, _snap_epoch(cheap))
        await _push_half("gh")

    try:
        # ── connect: cheap half first so the cards never sit empty ──────────
        await _push_snapshot(asyncio.get_event_loop().time())

        while True:
            now_s = asyncio.get_event_loop().time()
            push = False

            # ── 1. check change-epoch (cheap — pulse_status is gh-free) ───
            try:
                pulse = await agent_client.manager.pulse_status()
            except Exception:
                # Manager blip — skip this tick. The backstop gate below still
                # fires once enough time has passed since the LAST successful
                # push, so the client gets a fresh snapshot on recovery.
                pulse = None

            if pulse is not None:
                epoch = int(pulse.get("epoch") or 0)
                if epoch > last_epoch:
                    push = True

            # ── 2. backstop — push at least every _AGENT_BACKSTOP_S ───────
            if not push and (now_s - last_push_time >= _AGENT_BACKSTOP_S):
                push = True

            # ── 3. push the snapshot (cheap frame then gh frame) ───────────
            if push:
                await _push_snapshot(now_s)

            await asyncio.sleep(_AGENT_EPOCH_POLL_S)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        ws_mgr.drop(ws)


@router.websocket("/terminal/{session_id}")
async def ws_terminal(ws: WebSocket, session_id: str):
    """Bidirectional pipe between an xterm.js client and a PowerShell PTY.
    Protocol:
      Server -> client:
        <binary frame>                         - raw UTF-8 PTY output bytes
                                                 (only when the client opted in
                                                 via ?bin=1 on the WS URL)
        {"type": "out",  "data": "<utf-8 chunk>"} - PTY output, legacy JSON
                                                 framing (clients w/o ?bin=1)
        {"type": "exit"}                       - pty closed
      Client -> server (always JSON text frames):
        {"type": "in",     "data": "<keystrokes>"}
        {"type": "resize", "rows": 30, "cols": 100}
        {"type": "flow"}                       - client supports ACK flow control
        {"type": "ack",    "bytes": N}         - N more payload bytes processed
                                                 through term.write() since the
                                                 last ack (delta, raw UTF-8 bytes)
    Output framing (Phase 3, 2026-06-10): mixed text/binary on one WS — the
    standard ttyd / VS Code pattern. Output data goes as binary frames (no
    JSON escaping overhead); control messages (meta, exit, ping) stay JSON
    text. The capability is negotiated via the `?bin=1` query param rather
    than a hello message because it must be known BEFORE the pump sends its
    first frame — the screen-state replay is usually already queued when the
    receive loop would process a hello, and replay is the biggest burst the
    connection ever sees. A stale cached bundle (whose URL lacks ?bin=1)
    keeps the legacy JSON-out framing; an old dashboard ignores the query
    param and the new bundle still understands JSON 'out' frames.
    Replay (the serialized pyte screen — grid + scrollback + cursor) is sent as
    chunked initial output frames on attach; replay bytes count toward the same
    in-flight window as live output.
    Flow control is ARMED only after the client sends {"type":"flow"} — an old
    cached bundle that never ACKs gets the legacy unbounded-push behavior
    instead of a stream frozen at the high watermark. All flow-control state is
    per-WebSocket-connection, so a fresh attach resets the window on both ends.
    """
    # Binary-output capability — must be read from the URL (known at accept
    # time) so the very first pump frame already uses the right format. See
    # docstring above for why a hello message can't carry this.
    bin_out = ws.query_params.get("bin") == "1"
    # Takeover attach (dual-purpose ↻): claim the resize lock + size the PTY to
    # this device before the replay is serialized. Read from the URL — like
    # ?bin=1 — because the manager attach fires at accept time, before the
    # client sends any message. rows/cols only honored when takeover=1.
    takeover = ws.query_params.get("takeover") == "1"

    def _q_int(name: str) -> int | None:
        try:
            return int(ws.query_params.get(name, ""))
        except (TypeError, ValueError):
            return None

    to_rows = _q_int("rows") if takeover else None
    to_cols = _q_int("cols") if takeover else None

    session = await terminal_client.manager.get(session_id)
    if session is None or not session.is_alive():
        await ws.close(code=4404)  # custom: session not found / dead
        return
    await ws.accept()
    ws_mgr.add_terminal(ws)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
    sub_id = await session.attach(loop, queue, takeover=takeover, rows=to_rows, cols=to_cols)

    # ── flow-control state (per connection — resets on every fresh attach) ──
    # Mutated only from this event loop (pump_out task + receive loop below),
    # so a dict + Event is race-free without locks. `armed` flips on the
    # client's {"type":"flow"} hello; until then we never pause (old-bundle
    # compat). resume_evt set = pump may send; cleared while paused.
    flow = {"sent": 0, "acked": 0, "paused": False, "armed": False}
    resume_evt = asyncio.Event()
    resume_evt.set()

    async def pump_out():
        # Drain queue -> send messages until sentinel (None) or socket error.
        # The reader thread pushes None when the pty exits, which lets us
        # notify the client and exit cleanly.
        # Queue items are either:
        #   bytes — PTY output (wrapped in {"type":"out","data":...})
        #   ("meta", bytes) — pre-encoded JSON line (sent directly)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    try:
                        await ws.send_text(json.dumps({"type": "exit"}))
                    except Exception:
                        pass
                    # Close the WS from our side. Without this the receive
                    # loop + ping_loop keep a dead-session connection (and
                    # its two tasks) alive indefinitely — the client never
                    # closes on 'exit' either, and 30s pings keep both
                    # watchdogs happy. One zombie per dead tab adds up fast
                    # when a manager restart kills every session at once.
                    with contextlib.suppress(Exception):
                        await ws.close(code=1000)
                    return
                # Tuple items are metadata — claim-state broadcasts that the
                # pty-manager sent as complete JSON lines. Bypass the
                # {"type":"out"} envelope.
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "meta":
                    try:
                        await ws.send_text(item[1].decode("utf-8", errors="replace"))
                    except Exception:
                        pass
                    continue
                # Legacy JSON framing needs the bytes decoded to a str; the
                # binary path ships the raw bytes untouched (they're already
                # valid UTF-8 — trident_terminal produced them via
                # str.encode('utf-8', errors='replace')).
                text = ""
                if not bin_out:
                    try:
                        text = item.decode("utf-8", errors="replace")
                    except Exception:
                        continue  # malformed bytes (shouldn't happen with errors='replace')
                # ── flow-control gate ────────────────────────────────────
                # While paused, hold output here. The queue behind us fills
                # (512 items), then trident_terminal's pump blocks on put(),
                # then the manager TCP window fills — backpressure all the
                # way to the ConPTY read. The receive loop below is
                # untouched, so keystrokes and Ctrl-C reach the PTY
                # instantly even at full pause.
                # The wait is liveness-aware: an exit sentinel queued BEHIND
                # the item we're holding can't reach us while parked, so a
                # session that dies mid-pause (manager restart, PowerShell
                # exit) would otherwise freeze the tab with no "[session
                # exited]" until the client ACKs or disconnects. Every 2s we
                # ask the manager whether the session is still alive; if it
                # isn't (or the manager is gone), we drop the gate for good
                # and let the queue drain to the sentinel — the in-flight
                # window no longer matters for a dead session.
                while flow["paused"]:
                    try:
                        await asyncio.wait_for(resume_evt.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        info = await session.refresh()
                        if not info.get("alive"):
                            flow["paused"] = False
                            resume_evt.set()
                try:
                    if bin_out:
                        # Binary frame: the payload IS the frame. The client
                        # hands it to term.write(Uint8Array) — xterm decodes
                        # UTF-8 statefully, so even a split multibyte char
                        # across frames would reassemble correctly.
                        await ws.send_bytes(item)
                        sent_len = len(item)
                    else:
                        await ws.send_text(json.dumps({"type": "out", "data": text}))
                        # len(item) == len(text.encode('utf-8')) — items are
                        # produced by str.encode('utf-8', errors='replace')
                        # upstream, so the decode here is lossless (see the
                        # module-top flow-control comment). Counting the
                        # source bytes skips a full re-encode per chunk.
                        sent_len = len(item)
                except Exception as send_err:
                    # Log the real error so we can diagnose pump deaths.
                    # If the WS is still open this is usually a client
                    # disconnect — but log it regardless so we're not
                    # debugging blind.
                    logging.warning(
                        "terminal pump_out send failed for session %s: %s",
                        session_id[:6], send_err,
                    )
                    return
                # Count in-flight bytes and pause upstream past the high
                # watermark. Unit: raw UTF-8 bytes of the output payload —
                # binary clients count the frame's byteLength (the same
                # bytes), JSON clients count TextEncoder bytes of the
                # identical string, so both sides agree exactly in both
                # formats (see the module-top flow-control comment for the
                # lossless decode→re-encode argument). Only armed clients
                # ACK, so only armed connections may pause.
                if flow["armed"]:
                    flow["sent"] += sent_len
                    if (not flow["paused"]
                            and flow["sent"] - flow["acked"] >= FLOW_HIGH_WATER):
                        flow["paused"] = True
                        resume_evt.clear()
                        session.pause_output()
        except asyncio.CancelledError:
            pass

    pump_task = asyncio.create_task(pump_out())

    # Independent keepalive ping task — sends a ping every 30s regardless of
    # client activity. This keeps the client's watchdog (45s) from firing
    # spuriously when the terminal is idle. Without this, the client's own
    # 25s pings would keep resetting our receive timeout and we'd never send
    # a ping back, leaving the client with no received messages and triggering
    # a blank-refill reconnect flicker.
    async def ping_loop():
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    ping_task = asyncio.create_task(ping_loop())
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Client hasn't sent anything in 30s — this is unusual (client
                # pings every 25s) but could happen if the tab is backgrounded.
                # The ping_loop above handles server→client keepalive; this
                # branch is a safety net that also resets the Cloudflare idle
                # timer in the client→server direction.
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "in":
                data = msg.get("data")
                if isinstance(data, str):
                    session.write(data)
            elif t == "resize":
                rows = msg.get("rows")
                cols = msg.get("cols")
                if isinstance(rows, int) and isinstance(cols, int):
                    session.resize(rows, cols)
            elif t in ("claim", "release"):
                # Forward claim/release to the pty-manager. The claim dialog
                # (take-over / release button) lives in the frontend; these
                # messages are just relayed.
                session._send_msg({"type": t})
            elif t == "flow":
                # Client declares ACK-based flow-control support (sent once on
                # connect, before any output is counted). Arms the pause logic
                # in pump_out — without this hello we never pause, so an old
                # cached bundle keeps today's unbounded-push behavior.
                flow["armed"] = True
            elif t == "ack":
                # Delta ack: N more payload bytes processed by xterm since the
                # client's last ack. min() clamp guards a buggy/hostile client
                # from driving `acked` past `sent` (which would let the next
                # burst overshoot the window).
                n = msg.get("bytes")
                if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                    flow["acked"] = min(flow["acked"] + n, flow["sent"])
                    if (flow["paused"]
                            and flow["sent"] - flow["acked"] <= FLOW_LOW_WATER):
                        flow["paused"] = False
                        session.resume_output()
                        resume_evt.set()
            # Unknown types ignored — forward-compat for new client features.
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        ws_mgr.drop(ws)
        session.detach(sub_id)
        pump_task.cancel()
        ping_task.cancel()
        with contextlib.suppress(Exception):
            await pump_task
            await ping_task
