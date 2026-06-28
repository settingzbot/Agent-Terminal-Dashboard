// The four-pill HITL notification counts for the Claude nav badge (#65, parent
// PRD #58). Polls the backend's derived pill state on a slow cadence so the
// operator sees at a glance -- without opening the Agents view -- that there is
// completed work to ack (green), supervised sign-off pending (yellow), a trading-surface
// approval waiting at the money wall (purple), or a problem to handle (red).
//
// Was a single in-flight-run count (issue #36); extended here to the four pill
// counts fed by GET /api/agents/pill-counts (manager.pill_counts -> the pure
// derive_pill_counts). The nav badge renders these four counts; the Claude tab's
// pill row + the approve/views consume the same numbers.
//
// NOTE (no silent magic): the first poll hits /api/agents/pill-counts, which
// lazy-spawns the standalone agent-manager daemon (trident_agent_manager.py) if
// it isn't already running. That daemon is idle until an agent fires and the
// autonomous watch loop is gated OFF by default, so this is safe -- but it does
// mean opening the dashboard starts the manager.

import { useCallback, useEffect, useState } from 'react';
import { getPillCounts, type PillCounts } from '../api/agents';

const POLL_MS = 15000;

const ZERO: PillCounts = { green: 0, yellow: 0, purple: 0, red: 0 };

export type AgentBadge = {
  counts: PillCounts;
  /** Sum of all four pills -- a quick "is there anything?" for callers. */
  total: number;
  /** Re-fetch now (e.g. right after acking the green pill so it clears live). */
  refresh: () => void;
};

export function useAgentBadge(): AgentBadge {
  const [counts, setCounts] = useState<PillCounts>(ZERO);
  const [nonce, setNonce] = useState(0);

  const refresh = useCallback(() => setNonce(n => n + 1), []);

  useEffect(() => {
    const ctrl = new AbortController();
    let timer: number | null = null;

    const tick = async () => {
      try {
        const c = await getPillCounts(ctrl.signal);
        if (!ctrl.signal.aborted) setCounts(c);
      } catch {
        // KEEP the last-known counts on a transient fetch error -- do NOT zero.
        // The manager lazy-spawns on the first poll after a hard refresh and can
        // blip (503/abort) mid-spawn; zeroing here made the badge flash in then
        // vanish ("pills appear and disappear on refresh", 2026-06-23). The first
        // load stays at ZERO (hidden) until the first success, so there's no
        // flicker; a genuinely-down manager shows slightly-stale counts, which is
        // strictly better than disappearing pills.
      }
    };

    void tick();
    timer = window.setInterval(() => { void tick(); }, POLL_MS);
    return () => { ctrl.abort(); if (timer !== null) window.clearInterval(timer); };
  }, [nonce]);

  const total = counts.green + counts.yellow + counts.purple + counts.red;
  return { counts, total, refresh };
}
