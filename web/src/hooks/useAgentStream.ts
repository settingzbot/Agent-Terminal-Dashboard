// WebSocket client for /ws/agents -- the agent-panel live push (#151).
//
// Replaces the four polling effects (runs, pills, pulse, loop) in
// AgentManagerPane with a single WebSocket stream. On connect (and on every
// change-epoch advance / ~20s backstop) the server pushes the snapshot in TWO
// frames: the CHEAP half first (runs, pulse, watch_gate, supervised -- in-memory,
// microseconds) so the clickable agent cards render instantly, then the GH half
// (pill_counts + loop_health -- the ~8 `gh` calls) a beat later. We merge frames
// per-section into the refs below, so a cheap frame followed by a gh frame
// reconstructs the full snapshot with no duplicated keys -- and a section absent
// from a frame simply holds its last-known value (#151 perf follow-up).
//
// Reconnect strategy: exponential backoff up to 10s if the socket drops.
// State is held across reconnects so the panel never freezes empty.

import { useEffect, useRef, useState } from 'react';
import type {
  AgentSnapshot, RunRecord, PillCounts, AgentPulse,
  LoopStatus, WatchGateStatus, SupervisedStatus, StalenessStatus,
} from '../api/agents';

export type AgentStreamState = {
  /** Latest full snapshot from the server, or null until the first message. */
  snapshot: AgentSnapshot | null;
  /** Derived sections fanned out from the snapshot for convenience. */
  runs: RunRecord[];
  pills: PillCounts;
  pulse: AgentPulse;
  loop: LoopStatus | null;
  watchGate: WatchGateStatus | null;
  supervised: SupervisedStatus | null;
  staleness: StalenessStatus | null;
  /** Connection health. 'connecting' until the first successful message. */
  status: 'connecting' | 'open' | 'closed';
  /** Non-null when the most recent connection attempt failed. */
  error: string | null;
};

const ZERO_PILLS: PillCounts = { green: 0, yellow: 0, purple: 0, red: 0 };
const ZERO_PULSE: AgentPulse = { active: false, tick_seq: 0 };

/**
 * Open a /ws/agents WebSocket and return the latest pushed agent-panel state.
 * State is held across reconnects so the panel never freezes empty. Only
 * connects when `enabled` is true (the Agents view is on screen).
 */
export function useAgentStream(enabled: boolean): AgentStreamState {
  const [snapshot, setSnapshot] = useState<AgentSnapshot | null>(null);
  const [status, setStatus] = useState<'connecting' | 'open' | 'closed'>('connecting');
  const [error, setError] = useState<string | null>(null);

  // Track the latest snapshot sections so we can hold them across reconnects.
  const latestRuns = useRef<RunRecord[]>([]);
  const latestPills = useRef<PillCounts>(ZERO_PILLS);
  const latestPulse = useRef<AgentPulse>(ZERO_PULSE);
  const latestLoop = useRef<LoopStatus | null>(null);
  const latestWatchGate = useRef<WatchGateStatus | null>(null);
  const latestSupervised = useRef<SupervisedStatus | null>(null);
  const latestStaleness = useRef<StalenessStatus | null>(null);

  useEffect(() => {
    if (!enabled) {
      setStatus('closed');
      return;
    }

    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let backoffMs = 500;

    const connect = () => {
      if (cancelled) return;
      setStatus('connecting');
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(`${proto}//${location.host}/ws/agents`);

      ws.onopen = () => {
        if (cancelled) return;
        setStatus('open');
        setError(null);
        backoffMs = 500;
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
        try {
          const snap = JSON.parse(ev.data) as AgentSnapshot;

          // Fan out sections and persist in refs so we hold last-known across
          // reconnects -- the panel never freezes empty.
          if (Array.isArray(snap.runs)) latestRuns.current = snap.runs;
          if (snap.pill_counts) latestPills.current = snap.pill_counts;
          if (snap.pulse) latestPulse.current = snap.pulse;
          if (snap.loop_health) latestLoop.current = snap.loop_health;
          if (snap.watch_gate && Object.keys(snap.watch_gate).length > 0) {
            latestWatchGate.current = snap.watch_gate as WatchGateStatus;
          }
          if (snap.supervised && Object.keys(snap.supervised).length > 0) {
            latestSupervised.current = snap.supervised as SupervisedStatus;
          }
          if (snap.staleness) {
            latestStaleness.current = snap.staleness;
          }

          // Reconstruct the full snapshot so downstream consumers see a
          // consistent object even on partial updates.
          setSnapshot({
            runs: latestRuns.current,
            pill_counts: latestPills.current,
            pulse: latestPulse.current,
            loop_health: latestLoop.current ?? {
              armed: false, frozen: false, frozen_reason: null,
              in_flight: 0, open_prs: 0, pr_cap: 1,
              stage_running: { claim: 0, review: 0, land: 0 },
              claims_withheld: true, withhold_reason: 'connecting...',
              withhold_kind: null, blockers: [], gh_ok: false,
            },
            watch_gate: latestWatchGate.current ?? ({} as WatchGateStatus),
            supervised: latestSupervised.current ?? ({} as SupervisedStatus),
            staleness: latestStaleness.current ?? { loaded_sha: null, repo_head: null, stale: false },
          });
        } catch {
          // Malformed message; skip silently.
        }
      };

      ws.onerror = () => {
        if (cancelled) return;
        setError('connection error');
      };

      ws.onclose = () => {
        if (cancelled) return;
        setStatus('closed');
        reconnectTimer = window.setTimeout(connect, backoffMs);
        backoffMs = Math.min(backoffMs * 2, 10_000);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      if (ws && ws.readyState === WebSocket.OPEN) ws.close();
    };
  }, [enabled]);

  return {
    snapshot,
    runs: latestRuns.current,
    pills: latestPills.current,
    pulse: latestPulse.current,
    loop: latestLoop.current,
    watchGate: latestWatchGate.current,
    supervised: latestSupervised.current,
    staleness: latestStaleness.current,
    status,
    error,
  };
}
