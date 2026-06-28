// Client for the dashboard's own lifecycle endpoints (distinct from the
// terminal-session API in terminal.ts). Currently just the restart flow used by
// the Settings menu's "Restart Dashboard" button.

// Ask the server to restart itself. The backend (server/app.py) flushes this
// 200 and then exits with its RESTART_EXIT_CODE; the launcher script relaunches
// uvicorn in place. The request may or may not resolve cleanly depending on how
// quickly the worker tears down — callers should treat a thrown/aborted request
// the same as success and move straight to polling /api/health.
export async function restartDashboard(signal?: AbortSignal): Promise<void> {
  await fetch('/api/restart', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
    signal,
  });
}

// One liveness probe. Resolves true only on a 200 from /api/health; any network
// error (server mid-restart) or non-OK status resolves false. cache:'no-store'
// so a probe never reads a stale cached 200 from before the restart.
export async function pingHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch('/api/health', { cache: 'no-store', signal });
    return res.ok;
  } catch {
    return false;
  }
}

// Drive a full restart: trigger it, wait for the server to actually go DOWN
// (so we don't reload into the still-running old instance), then wait for the
// new instance to answer, then hard-reload to pick up the rebuilt assets.
//
//   onState  — progress callback ('restarting' while polling, 'error' on
//              timeout). Success doesn't report — the page reloads instead.
//   reload   — injectable for tests; defaults to a real location.reload().
//
// Reload conditions: healthy AND (we observed a down window  OR  enough time
// has passed that the restart has certainly completed). The down-window guard
// is the common path; the time fallback covers the rare case where the brief
// outage falls entirely between two probes.
export async function performRestart(
  onState: (s: 'restarting' | 'error') => void,
  reload: () => void = () => window.location.reload(),
): Promise<void> {
  onState('restarting');
  // The request often never resolves (the worker exits mid-response); ignore
  // any error and go straight to polling.
  try { await restartDashboard(); } catch { /* expected — server is exiting */ }

  const start = Date.now();
  const FIRST_PROBE_MS = 1200;   // past the server's ~0.7s exit delay
  const INTERVAL_MS = 400;
  const CERTAINLY_RESTARTED_MS = 5000;  // healthy after this long ⇒ safe to reload
  const TIMEOUT_MS = 30000;

  let sawDown = false;
  await new Promise<void>(resolve => {
    const tick = async () => {
      const ok = await pingHealth();
      const elapsed = Date.now() - start;
      if (!ok) sawDown = true;
      if (ok && (sawDown || elapsed > CERTAINLY_RESTARTED_MS)) {
        reload();
        resolve();
        return;
      }
      if (elapsed > TIMEOUT_MS) {
        onState('error');
        resolve();
        return;
      }
      window.setTimeout(tick, INTERVAL_MS);
    };
    window.setTimeout(tick, FIRST_PROBE_MS);
  });
}
