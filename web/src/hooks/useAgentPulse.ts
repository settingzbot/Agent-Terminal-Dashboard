// Drives the Claude-tab AGENTS button glow + pulse. Nathan's ask (2026-06-24):
// the tab should glow/sparkle in the dashboard accent while an agent is active,
// and flash once every time an agent checks for work.
//
// `active` powers the steady breathing glow. `pulseKey` bumps once each time the
// backend scheduler-tick counter advances -- i.e. on a REAL manager loop check
// (~30s), not a UI timer -- so a component can key a one-shot flash off it.
//
// Backed by GET /api/agents/pulse, a cheap gh-free read (active-run flag + tick
// counter). Unlike loop-status it makes no GitHub calls, so polling every few
// seconds is safe. We only poll while `enabled` (the Claude tab is on screen) --
// the strip isn't visible otherwise, and the nav badge covers cross-tab
// awareness.

import { useEffect, useRef, useState } from 'react';
import { getPulse } from '../api/agents';

const POLL_MS = 4000;

export type AgentPulse = {
  /** Any agent run is in flight (running/queued) -> steady glow. */
  active: boolean;
  /** Bumps on each real scheduler check -> drives a one-shot flash. */
  pulseKey: number;
  /**
   * Manager reachability, driving the AGENTS tab's status dot (green/amber/red,
   * mirroring the session tabs). `null` = first poll pending (connecting/amber);
   * `true` = last pulse succeeded (online/green); `false` = last pulse failed
   * (down or mid-respawn/red).
   */
  online: boolean | null;
};

export function useAgentPulse(enabled: boolean): AgentPulse {
  const [active, setActive] = useState(false);
  const [pulseKey, setPulseKey] = useState(0);
  const [online, setOnline] = useState<boolean | null>(null);
  // Last tick_seq we observed. Null until the first successful poll so we never
  // flash on mount (or on re-enable) -- only on a genuine advance afterwards.
  const lastTick = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) {
      setActive(false);
      return;
    }
    const ctrl = new AbortController();

    const tick = async () => {
      try {
        const p = await getPulse(ctrl.signal);
        if (ctrl.signal.aborted) return;
        setActive(!!p.active);
        setOnline(true);
        const seq = Number(p.tick_seq) || 0;
        if (lastTick.current !== null && seq > lastTick.current) {
          setPulseKey(k => k + 1);
        }
        lastTick.current = seq;
      } catch {
        // Manager blip (503/abort during lazy-spawn) -- keep last-known glow
        // state, no flicker (a stale glow beats a flash). But the status dot
        // should be honest: a failed pulse means the manager is unreachable
        // right now, so flag it offline. An abort (tab navigated away) isn't a
        // real outage, so don't mark down in that case.
        if (!ctrl.signal.aborted) setOnline(false);
      }
    };

    void tick();
    const timer = window.setInterval(() => { void tick(); }, POLL_MS);
    return () => { ctrl.abort(); window.clearInterval(timer); };
  }, [enabled]);

  return { active, pulseKey, online };
}
