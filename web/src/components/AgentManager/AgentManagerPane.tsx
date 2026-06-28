// Agent Manager -- the non-terminal view in the Claude tab (issue #29). A
// master/detail surface: the left rail lists saved agents (recipes) and recent
// runs; the right panel is either the create/edit form or a run's detail (status
// + feed + live terminal). Runs are polled so agent-triggered and run-now
// executions appear and update their status live.
//
// This is a sibling React view to ClaudeTerminalCard rather than a new pane type
// inside it -- the terminal card's pane model is fused to xterm/WS/keyboard
// plumbing, so weaving a non-terminal pane through it was needless risk for an
// identical user-facing result. The Claude tab switches between the two.

import { useCallback, useEffect, useState } from 'react';
import {
  listAgents, listRuns, runNow, setAgentEnabled, setWatchGate,
  getPillCounts, ackCompleted, isRunActive,
  getLoopStatus, resolveBlocker, getProvider, setProvider, restartAgentManager,
  getSnapshot,
  type AgentDefinition, type RunRecord, type WatchGateStatus,
  type AgentProviderStatus, type PillCounts,
  type LoopStatus, type LoopBlocker, type AgentSnapshot,
  type StalenessStatus,
} from '../../api/agents';
import { useAgentStream } from '../../hooks/useAgentStream';
import { type Theme } from '../../theme';
import { pillColor } from '../PillBadge';
import { AgentForm } from './AgentForm';
import { RunDetail } from './RunDetail';
import { TradeApprovalView } from './TradeApprovalView';
import { CompletedView } from './CompletedView';
import { ProblemsView } from './ProblemsView';
import { LoopHealthPanel } from './LoopHealthPanel';

type Props = {
  theme: Theme;
  accent: string;
  isMobile?: boolean;
  // Pause polling while the Agents view isn't on screen.
  isActive?: boolean;
};

// Right-panel state: nothing selected, the form (new or editing), a run, the
// trading-surface approval view, the completed-work view, or the problems view.
type Selection =
  | { kind: 'none' }
  | { kind: 'form'; agent: AgentDefinition | null }
  | { kind: 'run'; runId: string }
  | { kind: 'trade-approval' }
  | { kind: 'completed' }
  | { kind: 'problems' };

const BACKSTOP_POLL_MS = 30_000;  // slow HTTP backstop -- the stream pushes live updates (#151)

const ZERO_PILLS: PillCounts = { green: 0, yellow: 0, purple: 0, red: 0 };

export function AgentManagerPane({ theme, accent, isMobile = false, isActive = true }: Props) {
  const [agents, setAgents] = useState<AgentDefinition[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [sel, setSel] = useState<Selection>({ kind: 'none' });
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  // Which agent is mid-launch (disables its and shows a spinner label).
  const [launching, setLaunching] = useState<string | null>(null);
  // Master watch-gate arm state (the autonomous issue->PR loop). null until loaded.
  const [watchGate, setWatchGateState] = useState<WatchGateStatus | null>(null);
  const [armBusy, setArmBusy] = useState(false);
  // System-wide agent provider (Claude vs DeepSeek) -- switches all three agents.
  const [agentProvider, setAgentProvider] = useState<AgentProviderStatus | null>(null);
  const [providerBusy, setProviderBusy] = useState(false);
  // Two-step toggle: flip the switch -> confirm modal -> commit + restart.
  const [pendingProvider, setPendingProvider] = useState<'claude' | 'deepseek' | null>(null);
  // The four-pill HITL counts (#65) -- drives the pill row + which detail views
  // are reachable. Polled so a freshly-parked money-wall issue / halt appears.
  const [pills, setPills] = useState<PillCounts>(ZERO_PILLS);
  // The loop-health snapshot (armed/frozen, in-flight, PR cap, blockers) driving
  // the LoopHealthPanel. null until first load; kept last-known on a transient
  // fetch error (same blip-tolerance rationale as pills). Polled while active.
  const [loopStatus, setLoopStatus] = useState<LoopStatus | null>(null);
  // The blocker action currently mid-resolve (its button shows a busy state).
  const [busyAction, setBusyAction] = useState<LoopBlocker['action'] | null>(null);
  // Agent-manager liveness, driven off the RUNS poll (the cheapest frequent call,
  // every 3s) as a heartbeat. true=last poll reached the manager, false=it threw
  // (a downed/unresponsive manager makes every /api/agents/* route 503), null=not
  // yet known / connecting. Drives the status strip at the top of the rail.
  const [managerOk, setManagerOk] = useState<boolean | null>(null);
  // Manager staleness (loaded SHA != repo HEAD, #182). Drives the "stale brain"
  // banner. null until first snapshot lands.
  const [staleness, setStaleness] = useState<StalenessStatus | null>(null);

  // -- live stream: /ws/agents pushes the consolidated snapshot on every
  // change-epoch advance + a slow ~20s backstop (#151) ----------------------
  const stream = useAgentStream(isActive);

  // Fan out each pushed snapshot into the component's state setters (#151).
  useEffect(() => {
    const snap = stream.snapshot;
    if (!snap) return;
    setRuns(snap.runs);
    setPills(snap.pill_counts);
    if (snap.loop_health) setLoopStatus(snap.loop_health);
    if (snap.watch_gate && Object.keys(snap.watch_gate).length > 0) {
      setWatchGateState(snap.watch_gate as WatchGateStatus);
    }
    if (snap.staleness) setStaleness(snap.staleness);
    setManagerOk(true);
    setError(null);
  }, [stream.snapshot]);

  // Track stream transport health for the manager-status bar -- when the
  // WebSocket drops, flag the manager as unreachable until it reconnects.
  useEffect(() => {
    if (stream.status === 'closed') setManagerOk(false);
    else if (stream.status === 'connecting') setManagerOk(prev => prev ?? null);
  }, [stream.status]);

  const refreshLoopStatus = useCallback(async (signal?: AbortSignal) => {
    try {
      const s = await getLoopStatus(signal);
      if (!signal?.aborted) setLoopStatus(s);
    } catch {
      // KEEP the last-known loop status on a transient fetch error -- same rationale
      // as refreshPills: the manager lazy-spawns and can blip mid-spawn; the panel
      // shows slightly-stale state rather than flashing empty.
    }
  }, []);

  const refreshPills = useCallback(async (signal?: AbortSignal) => {
    try {
      const p = await getPillCounts(signal);
      if (!signal?.aborted) setPills(p);
    } catch {
      // KEEP the last-known pills on a transient fetch error -- do NOT zero. The
      // manager lazy-spawns on the first poll after a hard refresh and can blip
      // mid-spawn; zeroing made the pills flash in then vanish (2026-06-23). The
      // initial state is ZERO_PILLS (hidden) until the first success, so there's
      // no flicker on a clean load; a downed manager shows slightly-stale counts.
    }
  }, []);

  const refreshProvider = useCallback(async (signal?: AbortSignal) => {
    try {
      const p = await getProvider(signal);
      if (!signal?.aborted) setAgentProvider(p);
    } catch (e) {
      if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshAgents = useCallback(async (signal?: AbortSignal) => {
    try {
      const a = await listAgents(signal);
      if (!signal?.aborted) { setAgents(a); setError(null); }
    } catch (e) {
      if (!signal?.aborted) setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const refreshRuns = useCallback(async (signal?: AbortSignal) => {
    try {
      const r = await listRuns(signal);
      if (!signal?.aborted) { setRuns(r); setManagerOk(true); setError(null); }
    } catch (e) {
      // A throw here means the manager is unreachable (every /api/agents/* route
      // 503s when it's down). Flag it for the status strip -- but KEEP the
      // last-known runs data (don't clear setRuns), same blip-tolerance as pills.
      if (!signal?.aborted) { setManagerOk(false); setError(e instanceof Error ? e.message : String(e)); }
    }
  }, []);

  // Consolidated snapshot -- one call replaces runs, pills, pulse, loop, watch-gate,
  // and supervised (#149). On success, fans the sections out to the corresponding
  // state setters. On error, keeps last-known values for each section (same
  // blip-tolerance as the individual polls) and flags the manager as unreachable.
  const refreshSnapshot = useCallback(async (signal?: AbortSignal) => {
    try {
      const snap: AgentSnapshot = await getSnapshot(signal);
      if (signal?.aborted) return;
      setRuns(snap.runs);
      setPills(snap.pill_counts);
      // Pulse not consumed here (useAgentPulse drives the Claude-tab AGENTS
      // button); still populated so the snapshot is self-contained.
      setLoopStatus(snap.loop_health);
      setWatchGateState(snap.watch_gate && Object.keys(snap.watch_gate).length > 0
        ? snap.watch_gate as WatchGateStatus : null);
      if (snap.staleness) setStaleness(snap.staleness);
      setManagerOk(true);
      setError(null);
    } catch (e) {
      // Manager unreachable -- flag the status strip but KEEP last-known data.
      if (!signal?.aborted) {
        setManagerOk(false);
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }, []);

  // Initial load -- one consolidated snapshot replaces six individual status reads
  // (runs, pill_counts, pulse, loop_health, watch_gate, supervised -- #149).
  useEffect(() => {
    const ctrl = new AbortController();
    void (async () => {
      await Promise.all([
        refreshAgents(ctrl.signal),
        refreshSnapshot(ctrl.signal),
        refreshProvider(ctrl.signal),
      ]);
      if (!ctrl.signal.aborted) setLoaded(true);
    })();
    return () => ctrl.abort();
  }, [refreshAgents, refreshSnapshot, refreshProvider]);

  // Slow HTTP backstop -- the /ws/agents stream pushes live updates on every
  // change-epoch advance; this slow poll catches any edge case where the stream
  // misses a push (e.g. a long manager blip spanning multiple backstop windows).
  // Runs at 30s instead of 3s -- the stream is the primary path (#151).
  useEffect(() => {
    if (!isActive) return;
    const ctrl = new AbortController();
    const t = window.setInterval(() => { void refreshSnapshot(ctrl.signal); }, BACKSTOP_POLL_MS);
    return () => { ctrl.abort(); window.clearInterval(t); };
  }, [isActive, refreshSnapshot]);

  // Open the completed view -> ack so the green pill clears on viewing (#65).
  // The ack folds the current succeeded run_ids into the server-side seen set;
  // we re-fetch the counts so the cleared pill reflects immediately.
  const openCompleted = useCallback(async () => {
    setSel({ kind: 'completed' });
    try {
      await ackCompleted();
    } catch {
      /* best-effort: the view still shows; the pill re-clears on the next poll */
    }
    void refreshPills();
  }, [refreshPills]);

  const selectedRun = sel.kind === 'run' ? runs.find(r => r.run_id === sel.runId) ?? null : null;
  // A selected run that vanished from the list (deleted) is treated as no
  // selection for rendering -- derived, not an effect, so there's no
  // setState-in-effect cascade. `sel` stays the setter target; the next
  // selection overwrites it.
  const effectiveSel: Selection =
    sel.kind === 'run' && loaded && !selectedRun ? { kind: 'none' } : sel;

  const doRunNow = useCallback(async (agentId: string) => {
    setLaunching(agentId);
    setError(null);
    try {
      const run = await runNow(agentId);
      await refreshRuns();
      setSel({ kind: 'run', runId: run.run_id });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLaunching(null);
    }
  }, [refreshRuns]);

  // Toggle an agent's enabled flag (issue #39). Optimistic: flip locally, then
  // reconcile with the server's normalized def; roll back to the prior state on
  // error. No firing is gated on this yet -- it's an operator pause switch + dim.
  const doToggleEnabled = useCallback(async (a: AgentDefinition) => {
    const next = !a.enabled;
    setAgents(prev => prev.map(p => p.id === a.id ? { ...p, enabled: next } : p));
    setError(null);
    try {
      const saved = await setAgentEnabled(a.id, next);
      setAgents(prev => prev.map(p => p.id === saved.id ? saved : p));
    } catch (e) {
      setAgents(prev => prev.map(p => p.id === a.id ? { ...p, enabled: a.enabled } : p));
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  // Arm/disarm the master watch gate. Arming is guarded by a confirm -- this is
  // the switch that lets autonomous agents open PRs, so it should never be a
  // stray one-tap. Disarming is immediate (turning safety ON needs no gate).
  const doToggleWatchGate = useCallback(async () => {
    const armed = watchGate?.enabled ?? false;
    const next = !armed;
    if (next && !window.confirm(
      'Arm the autonomous watch loop?\n\n'
      + 'Agents will start claiming ready-for-agent issues and opening PRs on '
      + 'their own (one open PR at a time). You review every PR before it merges. '
      + 'You can disarm here at any time.'
    )) return;
    setArmBusy(true);
    setError(null);
    try {
      const g = await setWatchGate(next);
      setWatchGateState(g);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setArmBusy(false);
    }
  }, [watchGate]);

  const doSetProvider = useCallback(async () => {
    if (!pendingProvider) return;
    setProviderBusy(true);
    try {
      const p = await setProvider(pendingProvider);
      setAgentProvider(p);
      setPendingProvider(null);
      // Restart the agent manager so it picks up the new env.
      const r = await restartAgentManager();
      if (!r.ok) {
        setError(`provider set to ${p.provider}, but manager restart: ${r.detail}`);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setProviderBusy(false);
    }
  }, [pendingProvider]);

  // Perform a mechanical blocker unblock (close an orphan PR, clear a freeze).
  // The window.confirm has already happened in the panel; here we just POST and
  // refresh. Mark the action busy so its button disables, then refresh both the
  // loop status (the blocker should clear) and the pills (a freeze feeds the red
  // pill). Errors surface in the rail's error line; the busy marker always clears.
  const doResolveBlocker = useCallback(async (action: NonNullable<LoopBlocker['action']>) => {
    setBusyAction(action);
    setError(null);
    try {
      await resolveBlocker(action);
      void refreshLoopStatus();
      void refreshPills();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyAction(null);
    }
  }, [refreshLoopStatus, refreshPills]);

  const onSaved = useCallback((a: AgentDefinition) => {
    setAgents(prev => {
      const i = prev.findIndex(p => p.id === a.id);
      if (i >= 0) { const next = [...prev]; next[i] = a; return next; }
      return [...prev, a].sort((x, y) => x.id.localeCompare(y.id));
    });
    setSel({ kind: 'none' });
  }, []);

  const onDeleted = useCallback((id: string) => {
    setAgents(prev => prev.filter(p => p.id !== id));
    setSel({ kind: 'none' });
  }, []);

  // Mobile: detail replaces the list when something is selected; a back arrow
  // returns. Desktop: list rail + detail side by side. Rendering keys off
  // effectiveSel so a vanished run collapses to the empty state.
  const showDetail = effectiveSel.kind !== 'none';
  const detailOpen = isMobile ? showDetail : true;
  const listOpen = isMobile ? !showDetail : true;

  return (
    <div style={{ flex: 1, display: 'flex', minHeight: 0, overflow: 'hidden' }}>
      {listOpen && (
        <AgentRail
          theme={theme} accent={accent} isMobile={isMobile}
          agents={agents} runs={runs} loaded={loaded} error={error}
          launching={launching}
          managerOk={managerOk}
          staleness={staleness}
          pills={pills}
          loopStatus={loopStatus} busyAction={busyAction} onResolveBlocker={doResolveBlocker}
          activeView={effectiveSel.kind === 'trade-approval' ? 'trade-approval'
            : effectiveSel.kind === 'completed' ? 'completed'
            : effectiveSel.kind === 'problems' ? 'problems' : null}
          onOpenTradeApproval={() => setSel({ kind: 'trade-approval' })}
          onOpenCompleted={openCompleted}
          onOpenProblems={() => setSel({ kind: 'problems' })}
          selectedRunId={effectiveSel.kind === 'run' ? effectiveSel.runId : null}
          editingId={effectiveSel.kind === 'form' ? effectiveSel.agent?.id ?? '__new__' : null}
          onNew={() => setSel({ kind: 'form', agent: null })}
          onEdit={a => setSel({ kind: 'form', agent: a })}
          onRunNow={doRunNow}
          onToggleEnabled={doToggleEnabled}
          onSelectRun={runId => setSel({ kind: 'run', runId })}
          watchGate={watchGate} armBusy={armBusy} onToggleWatchGate={doToggleWatchGate}
          provider={agentProvider} providerBusy={providerBusy}
          pendingProvider={pendingProvider}
          onSetProvider={doSetProvider}
          onSetPendingProvider={setPendingProvider}
        />
      )}
      {detailOpen && (
        <div style={{
          flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', minHeight: 0,
          borderLeft: isMobile ? 'none' : `1px solid ${theme.border}`,
        }}>
          {isMobile && showDetail && (
            <button onClick={() => setSel({ kind: 'none' })}
              style={{
                textAlign: 'left', padding: '8px 14px', background: 'transparent',
                border: 'none', borderBottom: `1px solid ${theme.border}`,
                color: theme.text2, fontFamily: theme.fontMono, fontSize: 11, cursor: 'pointer',
              }}>
              back
            </button>
          )}
          {effectiveSel.kind === 'form' && (
            <AgentForm theme={theme} accent={accent} agent={liveFormAgent(agents, effectiveSel.agent)}
              onSaved={onSaved} onDeleted={onDeleted} onCancel={() => setSel({ kind: 'none' })} />
          )}
          {effectiveSel.kind === 'run' && selectedRun && (
            <RunDetail theme={theme} accent={accent} run={selectedRun} isMobile={isMobile}
              onChanged={() => { void refreshRuns(); void refreshPills(); }} />
          )}
          {effectiveSel.kind === 'trade-approval' && (
            <TradeApprovalView theme={theme} accent={accent}
              onApproved={() => { setSel({ kind: 'none' }); void refreshPills(); }} />
          )}
          {effectiveSel.kind === 'completed' && (
            <CompletedView theme={theme} runs={runs} />
          )}
          {effectiveSel.kind === 'problems' && (
            <ProblemsView theme={theme} runs={runs}
              onSelectRun={runId => setSel({ kind: 'run', runId })} />
          )}
          {effectiveSel.kind === 'none' && !isMobile && (
            <div style={{
              flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: theme.text3, fontFamily: theme.fontMono, fontSize: 12, textAlign: 'center', padding: 24,
            }}>
              <div>
                pick a run to watch its feed + terminal,<br />
                or <span style={{ color: accent }}>+ New agent</span> to create one
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// System-wide provider toggle bar -- Claude or DeepSeek for all three agents.
// Inline bar (same pattern as WatchGateBar / SupervisedBar) with a confirm-then-
// commit+restart flow so an accidental flip doesn't kill a running loop.
function ProviderBar({
  theme, accent, provider, busy, onToggle,
}: {
  theme: Theme; accent: string;
  provider: AgentProviderStatus | null; busy: boolean;
  onToggle: (next: 'claude' | 'deepseek') => void;
}) {
  const current = provider?.provider ?? 'claude';
  const keyOk = provider?.deepseek_key_configured ?? false;
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      padding: '6px 12px', borderBottom: `1px solid ${theme.border}`,
      background: current === 'deepseek' ? `${accent}0a` : 'transparent',
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontFamily: theme.fontBody, fontSize: 10, color: theme.text2,
          letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 2,
        }}>Provider</div>
        <div style={{
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text,
          fontWeight: 500,
        }}>
          {current === 'deepseek' ? 'DeepSeek' : 'Claude'}
          {current === 'deepseek' && !keyOk ? (
            <span style={{ color: theme.amber, marginLeft: 6, fontSize: 9 }}>no key</span>
          ) : null}
        </div>
      </div>
      <button
        onClick={() => onToggle(current === 'deepseek' ? 'claude' : 'deepseek')}
        disabled={busy}
        style={{
          padding: '3px 10px', border: `1px solid ${theme.borderHi}`, borderRadius: 4,
          background: 'transparent', color: busy ? theme.text3 : theme.text,
          fontFamily: theme.fontMono, fontSize: 9, cursor: busy ? 'default' : 'pointer',
          whiteSpace: 'nowrap',
        }}
      >{busy ? '...' : current === 'deepseek' ? 'Switch to Claude' : 'Switch to DeepSeek'}</button>
    </div>
  );
}

// -- left rail: agents + runs ---------------------------------------------------

function AgentRail({
  theme, accent, isMobile, agents, runs, loaded, error, launching,
  managerOk, staleness,
  pills, loopStatus, busyAction, onResolveBlocker,
  activeView, onOpenTradeApproval, onOpenCompleted, onOpenProblems,
  selectedRunId, editingId, onNew, onEdit, onRunNow, onToggleEnabled, onSelectRun,
  watchGate, armBusy, onToggleWatchGate,
  provider, providerBusy, pendingProvider, onSetProvider, onSetPendingProvider,
}: {
  theme: Theme; accent: string; isMobile: boolean;
  agents: AgentDefinition[]; runs: RunRecord[]; loaded: boolean;
  error: string | null; launching: string | null;
  managerOk: boolean | null;
  staleness: StalenessStatus | null;
  pills: PillCounts;
  loopStatus: LoopStatus | null;
  busyAction: LoopBlocker['action'] | null;
  onResolveBlocker: (action: NonNullable<LoopBlocker['action']>) => void;
  activeView: 'trade-approval' | 'completed' | 'problems' | null;
  onOpenTradeApproval: () => void; onOpenCompleted: () => void; onOpenProblems: () => void;
  selectedRunId: string | null; editingId: string | null;
  onNew: () => void; onEdit: (a: AgentDefinition) => void;
  onRunNow: (id: string) => void; onToggleEnabled: (a: AgentDefinition) => void;
  onSelectRun: (runId: string) => void;
  watchGate: WatchGateStatus | null; armBusy: boolean; onToggleWatchGate: () => void;
  provider: AgentProviderStatus | null; providerBusy: boolean;
  pendingProvider: 'claude' | 'deepseek' | null;
  onSetProvider: () => void;
  onSetPendingProvider: (next: 'claude' | 'deepseek' | null) => void;
}) {
  // Partition runs so in-flight ones pin to the top of the list (#86). runs is
  // already newest-first from the store, so each partition keeps that order.
  const activeRuns = runs.filter(r => isRunActive(r.status));
  const restRuns = runs.filter(r => !isRunActive(r.status));
  // Collapsible agents list -- hide the recipe list when not needed.
  const [agentsCollapsed, setAgentsCollapsed] = useState(false);
  return (
    <div style={{
      width: isMobile ? '100%' : 280, flexShrink: 0,
      display: 'flex', flexDirection: 'column', minHeight: 0,
      background: `${theme.bg2}22`,
    }}>
      {/* agent-manager liveness strip (heartbeat off the 3s RUNS poll) */}
      <ManagerStatusBar theme={theme} ok={managerOk} />

      {/* loop-health panel -- pinned under the manager status so it's always visible */}
      <LoopHealthPanel theme={theme} accent={accent}
        status={loopStatus} busyAction={busyAction} onResolve={onResolveBlocker}
        staleness={staleness} />

      {/* Single scroll region under the pinned strips: watch gate, provider,
          pill row, agents and runs all share one scrollbar. */}
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto' }}>
      {/* master watch-gate ARM switch */}
      <WatchGateBar theme={theme} accent={accent}
        gate={watchGate} busy={armBusy} onToggle={onToggleWatchGate} />

      {/* system-wide agent provider toggle -- switches all three agents at once */}
      <ProviderBar theme={theme} accent={accent}
        provider={provider} busy={providerBusy}
        onToggle={(next) => onSetPendingProvider(next)} />

      {/* four-pill HITL row (#65) -- clickable shortcuts into the views */}
      <PillRow theme={theme} pills={pills} activeView={activeView}
        onOpenTradeApproval={onOpenTradeApproval} onOpenCompleted={onOpenCompleted}
        onOpenProblems={onOpenProblems} />

      {/* agents section */}
      <div style={sectionHeadStyle(theme)}>
        <button
          onClick={() => setAgentsCollapsed(c => !c)}
          title={agentsCollapsed ? 'Show agents' : 'Hide agents'}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            background: 'none', border: 'none', cursor: 'pointer',
            fontFamily: theme.fontMono, fontSize: 10, letterSpacing: '0.12em',
            color: theme.text2, fontWeight: 700, padding: 0,
          }}>
          <span style={{
            display: 'inline-block', transition: 'transform 120ms ease',
            transform: agentsCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)',
            fontSize: 8,
          }}>V</span>
          <span>AGENTS</span>
          {loaded && (
            <span style={{ color: theme.text3, fontWeight: 400, letterSpacing: 0 }}>
              . {agents.length}
            </span>
          )}
        </button>
        <button onClick={onNew} style={addBtnStyle(theme, accent)}>+ New</button>
      </div>
      {!agentsCollapsed && (
      <div>
        {loaded && agents.length === 0 && (
          <div style={emptyStyle(theme)}>no agents yet -- + New to create one</div>
        )}
        {agents.map(a => {
          const editing = editingId === a.id;
          const isLaunching = launching === a.id;
          // OFF agents render dimmed (issue #39). The toggle itself stays full
          // opacity so it's still obviously clickable to flip back ON.
          const off = a.enabled === false;
          return (
            <div key={a.id}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '8px 12px',
                borderBottom: `1px solid ${theme.border}`,
                background: editing ? `${accent}10` : 'transparent',
              }}>
              <div style={{ flex: 1, minWidth: 0, cursor: 'pointer', opacity: off ? 0.4 : 1 }}
                onClick={() => onEdit(a)}>
                <div style={{
                  fontFamily: theme.fontBody, fontSize: 12, color: theme.text,
                  fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>{a.name}</div>
                {a.description ? (
                  <div style={{
                    fontFamily: theme.fontBody, fontSize: 10, color: theme.text2,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>{a.description}</div>
                ) : null}
                <div style={{ fontFamily: theme.fontMono, fontSize: 9, color: theme.text3, letterSpacing: '0.04em' }}>
                  {off ? 'off . ' : ''}
                  {a.trigger.kind === 'clock'
                    ? `clock . ${a.trigger.schedule}`
                    : `watch . ${a.trigger.stage ?? 'claim'} . ${a.trigger.poll_interval}s`}
                </div>
              </div>
              <ToggleSwitch theme={theme} accent={accent} on={!off}
                title={off ? 'Agent paused -- click to enable' : 'Agent enabled -- click to pause'}
                onToggle={() => onToggleEnabled(a)} />
              <button onClick={() => onRunNow(a.id)} disabled={isLaunching || off}
                title={off ? 'Agent is OFF -- enable it to run' : 'Run now'}
                style={{
                  padding: '4px 10px', background: `${accent}14`, color: accent,
                  border: `1px solid ${accent}44`, borderRadius: 4,
                  // OFF is a hard stop (#40): the launch the backend would reject
                  // with a 409 isn't even attemptable here.
                  cursor: (isLaunching || off) ? 'not-allowed' : 'pointer',
                  fontFamily: theme.fontMono, fontSize: 11, fontWeight: 700, flexShrink: 0,
                  opacity: isLaunching ? 0.5 : (off ? 0.4 : 1),
                }}>
                {isLaunching ? '...' : '>'}
              </button>
            </div>
          );
        })}
      </div>
      )}

      {/* runs section -- active (running/queued) runs are PINNED at the top and
          rendered prominently so an armed, working loop is unmistakable (#86).
          The data layer always had the running record (run_store writes it at
          stage start, polled every 3s); the old compact row just looked nearly
          identical to a finished one, so a live loop read as dead. */}
      <div style={sectionHeadStyle(theme)}>
        <span>RUNS</span>
        {activeRuns.length > 0 && (
          <span style={{
            display: 'inline-flex', alignItems: 'center', gap: 5,
            fontFamily: theme.fontMono, fontSize: 9, fontWeight: 700,
            color: theme.green, letterSpacing: '0.06em',
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%', background: theme.green,
              animation: 'tridentPulse 2s ease-in-out infinite',
            }} />
            {activeRuns.length} ACTIVE
          </span>
        )}
      </div>
      <div>
        {error && <div style={{ ...emptyStyle(theme), color: theme.red }}>{error}</div>}
        {loaded && runs.length === 0 && !error && (
          <div style={emptyStyle(theme)}>no runs yet</div>
        )}
        {activeRuns.map(r => (
          <RunRow key={r.run_id} theme={theme} accent={accent} run={r}
            selected={selectedRunId === r.run_id} prominent onSelect={onSelectRun} />
        ))}
        {activeRuns.length > 0 && restRuns.length > 0 && (
          <div style={{
            padding: '7px 12px 4px', fontFamily: theme.fontMono, fontSize: 8.5,
            letterSpacing: '0.12em', color: theme.text3, fontWeight: 700,
            background: `${theme.bg2}22`,
          }}>HISTORY</div>
        )}
        {restRuns.map(r => (
          <RunRow key={r.run_id} theme={theme} accent={accent} run={r}
            selected={selectedRunId === r.run_id} onSelect={onSelectRun} />
        ))}
      </div>

      {/* System-wide agent provider confirm modal */}
      {pendingProvider && (
        <div style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
          display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000,
        }} onClick={() => onSetPendingProvider(null)}>
          <div style={{
            background: theme.bg2, border: `1px solid ${theme.borderHi}`,
            borderRadius: 8, padding: '20px 24px', maxWidth: 360,
          }} onClick={e => e.stopPropagation()}>
            <div style={{
              fontFamily: theme.fontBody, fontSize: 14, color: theme.text,
              fontWeight: 600, marginBottom: 8,
            }}>
              Switch all agents to {pendingProvider === 'deepseek' ? 'DeepSeek' : 'Claude'}?
            </div>
            <div style={{
              fontFamily: theme.fontBody, fontSize: 11, color: theme.text2,
              marginBottom: 16, lineHeight: 1.5,
            }}>
              {pendingProvider === 'deepseek'
                ? 'Gordon, Kleiner, and Eli will use DeepSeek V4-Pro. The agent manager will restart to pick up the new routing. Requires a DeepSeek API key in the keyring.'
                : 'Gordon, Kleiner, and Eli will use the Claude Max subscription. No API key needed. The agent manager will restart.'
              }
            </div>
            {pendingProvider === 'deepseek' && !provider?.deepseek_key_configured ? (
              <div style={{
                fontFamily: theme.fontBody, fontSize: 10, color: theme.amber,
                marginBottom: 12, padding: '6px 10px',
                background: `${theme.amber}10`, borderRadius: 4,
              }}>
                No DeepSeek key in the keyring. Set it in Settings first.
              </div>
            ) : null}
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button onClick={() => onSetPendingProvider(null)} style={{
                padding: '5px 14px', border: `1px solid ${theme.border}`, borderRadius: 4,
                background: 'transparent', color: theme.text2,
                fontFamily: theme.fontMono, fontSize: 10, cursor: 'pointer',
              }}>Cancel</button>
              <button onClick={onSetProvider} disabled={providerBusy} style={{
                padding: '5px 14px', border: 'none', borderRadius: 4,
                background: accent, color: theme.bg0,
                fontFamily: theme.fontMono, fontSize: 10, fontWeight: 600,
                cursor: providerBusy ? 'default' : 'pointer',
                opacity: providerBusy ? 0.5 : 1,
              }}>
                {providerBusy ? 'Restarting...' : 'Switch & Restart'}
              </button>
            </div>
          </div>
        </div>
      )}

      </div>
    </div>
  );
}

// The four-pill HITL row (#65). Each pill is a labelled count chip; the green,
// purple and red pills are CLICKABLE -- they open the completed view (which acks
// -> clears green), the trading-surface approval view, and the problems view
// (halted runs + their reasons). Yellow (supervised sign-off) renders as a
// status-only chip here (its dedicated surface lives elsewhere in the loop).
// The whole row hides when every count is zero, so a quiet system is quiet.
function PillRow({
  theme, pills, activeView, onOpenTradeApproval, onOpenCompleted, onOpenProblems,
}: {
  theme: Theme; pills: PillCounts;
  activeView: 'trade-approval' | 'completed' | 'problems' | null;
  onOpenTradeApproval: () => void; onOpenCompleted: () => void; onOpenProblems: () => void;
}) {
  const any = pills.green + pills.yellow + pills.purple + pills.red > 0;
  if (!any) return null;
  const chip = (
    label: string, kind: 'green' | 'yellow' | 'purple' | 'red',
    count: number, onClick?: () => void, on?: boolean,
  ) => {
    if (!(count > 0)) return null;
    const c = pillColor(kind, theme);
    const clickable = !!onClick;
    return (
      <button
        key={kind}
        onClick={onClick}
        disabled={!clickable}
        title={clickable ? `Open ${label} (${count})` : `${label}: ${count}`}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 5,
          padding: '3px 8px', borderRadius: 5,
          border: `1px solid ${c}${on ? 'aa' : '44'}`,
          background: on ? `${c}22` : `${c}12`,
          color: theme.text, cursor: clickable ? 'pointer' : 'default',
          fontFamily: theme.fontMono, fontSize: 10, fontWeight: 600,
        }}>
        <span style={{
          minWidth: 14, height: 14, padding: '0 4px', borderRadius: 7,
          background: c, color: theme.bg0, fontSize: 9, fontWeight: 700,
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          lineHeight: 1, fontVariantNumeric: 'tabular-nums',
        }}>{count}</span>
        <span style={{ color: theme.text2 }}>{label}</span>
      </button>
    );
  };
  return (
    <div style={{
      display: 'flex', flexWrap: 'wrap', gap: 6, padding: '8px 12px',
      borderBottom: `1px solid ${theme.border}`, flexShrink: 0,
      background: `${theme.bg2}22`,
    }}>
      {chip('done', 'green', pills.green, onOpenCompleted, activeView === 'completed')}
      {chip('sign-off', 'yellow', pills.yellow)}
      {chip('trade approval', 'purple', pills.purple, onOpenTradeApproval, activeView === 'trade-approval')}
      {chip('problems', 'red', pills.red, onOpenProblems, activeView === 'problems')}
    </div>
  );
}

// A small pill toggle for an agent's enabled flag (issue #39). Stops click
// propagation so flipping it never opens the edit form behind it.
function ToggleSwitch({
  theme, accent, on, title, onToggle,
}: {
  theme: Theme; accent: string; on: boolean; title: string; onToggle: () => void;
}) {
  return (
    <button
      role="switch" aria-checked={on} title={title}
      onClick={e => { e.stopPropagation(); onToggle(); }}
      style={{
        flexShrink: 0, width: 30, height: 17, padding: 0, borderRadius: 9,
        position: 'relative', cursor: 'pointer',
        background: on ? `${accent}55` : `${theme.text3}`,
        border: `1px solid ${on ? `${accent}88` : theme.border}`,
        transition: 'background 120ms ease',
      }}>
      <span style={{
        position: 'absolute', top: 1, left: on ? 14 : 1,
        width: 13, height: 13, borderRadius: '50%',
        background: on ? accent : theme.bg0,
        transition: 'left 120ms ease',
      }} />
    </button>
  );
}

// At-a-glance "is the agent-manager subprocess alive" strip, pinned to the top of
// the rail. The dashboard page and the manager are separate processes; when the
// manager is down/unresponsive every /api/agents/* route 503s and the RUNS poll
// throws -- that throw flips `ok` to false here. true=connected (pulsing green),
// false=unreachable (steady red + restart hint), null=connecting (dim). One
// compact mono row, washed + bottom-bordered to match the strips below it.
function ManagerStatusBar({
  theme, ok,
}: { theme: Theme; ok: boolean | null }) {
  const dotColor = ok === true ? theme.green : ok === false ? theme.red : theme.text3;
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8, padding: '7px 12px',
      borderBottom: `1px solid ${theme.border}`,
      background: `${theme.bg2}44`,
      flexShrink: 0,
    }}>
      <span style={{
        width: 7, height: 7, borderRadius: '50%', flexShrink: 0, background: dotColor,
        animation: ok === true ? 'tridentPulse 2s ease-in-out infinite' : 'none',
      }} />
      <span style={{
        fontFamily: theme.fontMono, fontSize: 10, letterSpacing: '0.12em',
        color: theme.text2, fontWeight: 700,
      }}>AGENT MANAGER</span>
      {ok === true && (
        <span style={{
          fontFamily: theme.fontMono, fontSize: 10, color: theme.green, fontWeight: 700,
        }}>connected</span>
      )}
      {ok === false && (
        <span style={{
          fontFamily: theme.fontMono, fontSize: 10, color: theme.red, fontWeight: 700,
        }}>unreachable -- restart the dashboard</span>
      )}
      {ok === null && (
        <span style={{
          fontFamily: theme.fontMono, fontSize: 10, color: theme.text3, fontWeight: 700,
        }}>connecting...</span>
      )}
    </div>
  );
}

// The master ARM switch for the autonomous issue->PR loop. This is the single
// surface that arms/disarms the watch gate (replacing the old env-var + restart
// dance). Visually distinct from the per-agent toggles -- it's a system-level
// safety switch -- and rendered ARMED in the accent colour with a live state line.
function WatchGateBar({
  theme, accent, gate, busy, onToggle,
}: {
  theme: Theme; accent: string; gate: WatchGateStatus | null;
  busy: boolean; onToggle: () => void;
}) {
  const armed = gate?.effective ?? false;
  // The legacy TRIDENT_AGENT_WATCH_ENABLED env var no longer arms anything
  // (dropped 2026-06-23 -- the dashboard toggle is the sole control). We still
  // surface it as an informational "detected but ignored" hint, never as if it
  // forces the loop on. armed/effective reflects the TOGGLE only.
  const legacyEnvDetected = gate?.env_override ?? false;
  const state = gate === null ? 'loading...' : armed ? 'ARMED' : 'DISARMED';
  const stateColor = gate === null ? theme.text3 : armed ? accent : theme.text2;
  return (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      gap: 10, padding: '10px 12px',
      borderBottom: `1px solid ${theme.border}`, flexShrink: 0,
      // a faint accent wash when armed so the live state reads at a glance
      background: armed ? `${accent}12` : `${theme.bg2}44`,
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{
          fontFamily: theme.fontMono, fontSize: 10, letterSpacing: '0.12em',
          color: theme.text2, fontWeight: 700,
        }}>AUTONOMOUS WATCH</div>
        <div style={{
          fontFamily: theme.fontMono, fontSize: 10, marginTop: 2,
          color: stateColor, fontWeight: 700, letterSpacing: '0.06em',
        }}>
          {state}
          {legacyEnvDetected && (
            <span style={{ color: theme.text3, fontWeight: 400 }}> . legacy env detected (ignored)</span>
          )}
        </div>
      </div>
      <ToggleSwitch theme={theme} accent={accent} on={armed}
        title={
          busy ? 'Working...'
            : armed ? 'Autonomous watch ARMED -- click to disarm'
            : 'Autonomous watch DISARMED -- click to arm (confirms first)'
        }
        // The toggle is the sole control now; the legacy env var no longer arms.
        onToggle={() => { if (!busy) onToggle(); }} />
    </div>
  );
}

// The edit form must bind to the LIVE agent (its current persisted `enabled`),
// not the snapshot captured when the form opened. Otherwise saving an unrelated
// field edit re-asserts the stale `enabled` and silently un-pauses an agent the
// operator just toggled OFF (issue #39 review fix). Resolve the snapshot to the
// freshest copy by id; fall back to the snapshot for a brand-new agent.
function liveFormAgent(
  agents: AgentDefinition[], snap: AgentDefinition | null,
): AgentDefinition | null {
  if (!snap) return null;
  return agents.find(a => a.id === snap.id) ?? snap;
}

function runDot(theme: Theme, r: RunRecord): string {
  if (r.status === 'running' || r.status === 'queued') return theme.green;
  if (r.status === 'succeeded') return theme.blue;
  if (r.status === 'failed' || r.status === 'halted') return theme.red;
  return theme.text3;  // cancelled
}

// A single run in the RUNS list. `prominent` is the in-flight treatment (#86):
// a green left-bar, larger glowing pulse dot, the stage word + issue badge, and
// a RUNNING/QUEUED chip with a live elapsed timer -- so an active loop is
// unmistakable next to the dim, compact history rows below it.
function RunRow({
  theme, accent, run: r, selected, prominent = false, onSelect,
}: {
  theme: Theme; accent: string; run: RunRecord; selected: boolean;
  prominent?: boolean; onSelect: (runId: string) => void;
}) {
  const active = isRunActive(r.status);
  const { stage, issue } = stageMeta(r.agent_id);
  const dot = runDot(theme, r);
  return (
    <div onClick={() => onSelect(r.run_id)}
      style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: prominent ? '9px 12px' : '8px 12px',
        borderBottom: `1px solid ${theme.border}`, cursor: 'pointer',
        borderLeft: prominent ? `3px solid ${theme.green}` : '3px solid transparent',
        background: selected ? `${accent}14` : prominent ? `${theme.green}12` : 'transparent',
      }}>
      <span style={{
        width: prominent ? 9 : 7, height: prominent ? 9 : 7, borderRadius: '50%',
        flexShrink: 0, background: dot,
        boxShadow: active ? `0 0 0 3px ${dot}22` : 'none',
        animation: r.status === 'running' ? 'tridentPulse 2s ease-in-out infinite' : 'none',
      }} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 6,
          fontFamily: theme.fontMono, fontSize: prominent ? 12 : 11,
          color: selected ? accent : theme.text, fontWeight: prominent ? 700 : 400,
          minWidth: 0,
        }}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {prominent ? stage.toUpperCase() : r.agent_id}
          </span>
          {prominent && issue && (
            <span style={{
              flexShrink: 0, padding: '0 5px', borderRadius: 4,
              background: `${accent}22`, color: accent, fontSize: 10, fontWeight: 700,
            }}>#{issue}</span>
          )}
        </div>
        {prominent ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
            <span style={{
              padding: '1px 6px', borderRadius: 4, background: theme.green, color: theme.bg0,
              fontFamily: theme.fontMono, fontSize: 8.5, fontWeight: 700, letterSpacing: '0.06em',
            }}>{r.status === 'queued' ? 'QUEUED' : 'RUNNING'}</span>
            <span style={{ fontFamily: theme.fontMono, fontSize: 9, color: theme.text3 }}>
              {fmtElapsed(r.started_at ?? r.created_at)}
            </span>
          </div>
        ) : (
          <div style={{ fontFamily: theme.fontMono, fontSize: 9, color: theme.text3 }}>
            {r.status} . {fmtAgo(r.created_at)}
          </div>
        )}
      </div>
    </div>
  );
}

// Parse a loop run's agent_id into a human stage + issue number. Gordon's runs
// are keyed watch-issue-<n>, Kleiner's review-issue-<n>, Eli's land-issue-<n>;
// the "watch" prefix reads as "claim" to match the operator's claim->review->land
// mental model. A manual/clock run (no match) keeps its agent id and no issue.
function stageMeta(agentId: string): { stage: string; issue: string | null } {
  const m = /^(watch|review|land)-issue-(\d+)/.exec(agentId);
  if (!m) return { stage: agentId, issue: null };
  return { stage: m[1] === 'watch' ? 'claim' : m[1], issue: m[2] };
}

function sectionHeadStyle(theme: Theme): React.CSSProperties {
  return {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '8px 12px', borderBottom: `1px solid ${theme.border}`,
    fontFamily: theme.fontMono, fontSize: 10, letterSpacing: '0.12em',
    color: theme.text2, fontWeight: 700, flexShrink: 0,
    background: `${theme.bg2}44`,
  };
}

function addBtnStyle(theme: Theme, accent: string): React.CSSProperties {
  return {
    padding: '3px 9px', background: `${accent}14`, color: accent,
    border: `1px solid ${accent}44`, borderRadius: 4, cursor: 'pointer',
    fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700, letterSpacing: '0.04em',
  };
}

function emptyStyle(theme: Theme): React.CSSProperties {
  return {
    padding: '12px', fontFamily: theme.fontMono, fontSize: 10,
    color: theme.text3, lineHeight: 1.5,
  };
}

function fmtAgo(ts: number | null): string {
  if (!ts) return '--';
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (secs < 60) return `${secs}s ago`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// Elapsed time since a run started -- the live "how long has this been going"
// for an active row (#86). Distinct from fmtAgo ("...ago"): this is a duration,
// updated each 3s poll. Falls back to "just now" before a timestamp exists.
function fmtElapsed(ts: number | null): string {
  if (!ts) return 'just now';
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ${secs % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
