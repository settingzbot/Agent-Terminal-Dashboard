// Client for the dashboard's agent-routine endpoints. Backend lives in
// dashboard/routes_agents.py (the /api/agents router) -> trident_agent_client ->
// the standalone agent-manager (trident_agent_manager.py). An Agent is a saved
// recipe; a Run is one execution of it, hosted as a tagged pty-manager session.
//
// Operator-only surface (the router is mounted in the operator block in
// dashboard/app.py, not in customer mode). Shapes mirror the Python stores
// 1:1 -- trident_agent_store.validate_agent and trident_agent_run_store.validate_run.

// -- agent definitions ----------------------------------------------------------

// Bootstrap-command vocabulary -- the launcher verbs from scripts/claude*.ps1,
// the same words Nathan types in a terminal tab (claude=Anthropic, -ds=DeepSeek,
// -go=skip-permissions). See trident_agent_store.VALID_MODELS.
export type AgentModel = 'claude' | 'claude-ds' | 'claude-go' | 'claude-ds-go';
export const AGENT_MODELS: { value: AgentModel; label: string }[] = [
  { value: 'claude',       label: 'Claude' },
  { value: 'claude-ds',    label: 'DeepSeek' },
  { value: 'claude-go',    label: 'Go (skip-perms)' },
  { value: 'claude-ds-go', label: 'DS Go' },
];

// trigger is a tagged union on `kind` (trident_agent_store._validate_trigger).
// A watch trigger carries a `stage` (#78) selecting which pipeline the manager
// drives: 'claim' (Gordon, issue->PR), 'review' (Kleiner), or 'land' (Eli).
// Absent on the backend => 'claim', so a legacy def with no stage reads as claim.
export type WatchStage = 'claim' | 'review' | 'land';
export type WatchTrigger = { kind: 'watch'; poll_interval: number; condition: string; stage?: WatchStage };
export type ClockTrigger = { kind: 'clock'; schedule: string };
export type AgentTrigger = WatchTrigger | ClockTrigger;

export type AgentDefinition = {
  id: string;        // slug [a-z0-9][a-z0-9_-]* -- filename stem on the backend
  name: string;
  // One-line human blurb shown under the name in the agent tab (#78). Defaults
  // to "" on the backend; a legacy def with no `description` reads back as "".
  description?: string;
  prompt: string;
  model: AgentModel;
  // Per-run backstop (minutes): how long a run's claude session may go before
  // it's killed + the run failed. Healthy headless runs exit early; this only
  // bites a stuck session. See trident_agent_store.DEFAULT_TIMEOUT_MINUTES.
  timeout_minutes: number;
  trigger: AgentTrigger;
  options: Record<string, unknown>;
  // Operator pause switch (issue #39). Defaults true on the backend; a legacy
  // agent file with no `enabled` key reads back as true. OFF is a HARD STOP
  // (#40): a disabled agent never fires on a schedule/watch AND its Run-now is
  // refused (409) -- the >> button is disabled for an OFF row.
  enabled: boolean;
};

// -- run records -----------------------------------------------------------------

// 'halted' is a terminal problem state the manager writes when a run is parked
// for a human (a trading-surface/scope halt, or a needs-human filing). It feeds
// the red problems pill and must be a first-class status so the rail/detail can
// colour it and the problems view can filter on it.
export type RunStatus =
  'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'halted';

export type RunRecord = {
  run_id: string;
  agent_id: string;
  tag: string | null;
  command: string | null;
  cwd: string | null;
  // The pty-manager session id hosting the run -- attach a live terminal via
  // terminalWsUrl(session_id). Null until the run reaches `running`.
  session_id: string | null;
  status: RunStatus;
  outcome: string | null;
  created_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  // Session output-token total stamped by the #73 guardrail at run-complete
  // (optional/back-compat: absent on unmeasured or pre-#73 runs).
  tokens?: number | null;
  // True when a HALTED run has been acknowledged via the run-detail Acknowledge
  // button -- it's then excluded from the red problems pill but still listed
  // (marked checkmark) in the problems view. Server-annotated; absent on legacy payloads.
  acked?: boolean;
};

// -- run feed (structured transcript) -------------------------------------------
// Assembled server-side from the run's Claude Code transcript JSONL
// (trident_agent_feed.assemble_feed). "thinking" blocks are intentionally absent.

export type FeedEvent =
  | { kind: 'prompt'; text: string; ts?: string | null }
  | { kind: 'text'; text: string; ts?: string | null }
  | { kind: 'tool_use'; name: string | null; tool_use_id: string | null; input: Record<string, unknown>; ts?: string | null }
  | { kind: 'tool_result'; tool_use_id: string | null; text: string; is_error: boolean; ts?: string | null }
  // A transcript-less land run (Eli -- pure git/npm/pytest, no Claude session)
  // emits its progress as structured stage events instead of a transcript:
  // merge -> bundle -> test -> push, each ok/fail/skip/info. `log` is that stage's
  // FULL command output (the actual git/npm/pytest run), shown as an expandable
  // block -- present only when output was captured.
  | { kind: 'stage'; stage: string; status: 'ok' | 'fail' | 'skip' | 'info'; detail: string; log?: string; ts?: string | number | null };

export type FeedUsage = {
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
  message_count: number;
  model: string | null;
  cost_usd: number;  // display-only estimate; authoritative spend is the provider's
};

export type RunFeed = {
  run_id: string;
  transcript: string | null;  // resolved JSONL path, or null if none written yet
  events: FeedEvent[];
  usage: FeedUsage;
};

// -- transport ------------------------------------------------------------------

export class AgentApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(`Agent API ${status}: ${message}`);
    this.name = 'AgentApiError';
    this.status = status;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal });
  if (!res.ok) throw new AgentApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  if (!res.ok) throw new AgentApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function putJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new AgentApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function delJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { method: 'DELETE', signal });
  if (!res.ok) throw new AgentApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

// -- agent CRUD -----------------------------------------------------------------

export async function listAgents(signal?: AbortSignal): Promise<AgentDefinition[]> {
  const res = await getJson<{ agents: AgentDefinition[] }>('/api/agents', signal);
  return res.agents;
}

export function getAgent(id: string, signal?: AbortSignal): Promise<AgentDefinition> {
  return getJson<AgentDefinition>(`/api/agents/${encodeURIComponent(id)}`, signal);
}

// Create or update. The backend validates and rejects bad definitions with a
// 400 whose body is the human-readable reason -- AgentApiError.message carries it.
export function upsertAgent(defn: AgentDefinition, signal?: AbortSignal): Promise<AgentDefinition> {
  return putJson<AgentDefinition>('/api/agents', defn, signal);
}

export function deleteAgent(id: string, signal?: AbortSignal): Promise<{ deleted: string }> {
  return delJson<{ deleted: string }>(`/api/agents/${encodeURIComponent(id)}`, signal);
}

// Toggle an agent's enabled flag (issue #39) -- a cheap one-tap pause/resume that
// persists without a full upsert. Returns the updated definition. 404 if the
// agent is gone. OFF is a hard stop (#40): the backend gates all firing on this
// -- scheduled, watch, AND manual Run-now (409).
export function setAgentEnabled(id: string, enabled: boolean, signal?: AbortSignal): Promise<AgentDefinition> {
  return postJson<AgentDefinition>(`/api/agents/${encodeURIComponent(id)}/enabled`, { enabled }, signal);
}

// -- master watch-gate (the ARM toggle) -------------------------------------------
// The single master switch that arms/disarms the autonomous issue->PR loop. Distinct
// from the per-agent enabled flag above. Persisted machine-locally; takes effect on
// the next scheduler tick (no restart). The toggle is the SOLE control: `effective`
// = `enabled` (the toggle). The legacy TRIDENT_AGENT_WATCH_ENABLED env var was
// dropped as an arming path 2026-06-23 -- it no longer gates anything.
export type WatchGateStatus = {
  enabled: boolean;       // the persisted toggle value (the sole control)
  env_override: boolean;  // legacy env var detected but IGNORED -- informational only
  effective: boolean;     // what the scheduler uses; now equals `enabled`
};

export function getWatchGate(signal?: AbortSignal): Promise<WatchGateStatus> {
  return getJson<WatchGateStatus>('/api/agents/watch-gate', signal);
}

export function setWatchGate(enabled: boolean, signal?: AbortSignal): Promise<WatchGateStatus> {
  return postJson<WatchGateStatus>('/api/agents/watch-gate', { enabled }, signal);
}

// -- per-project supervised-mode toggle (the yellow PM gate -- #66) ------------------
// When ON, a Kleiner-clean review parks at needs-signoff (the yellow PM gate) for a
// human sign-off BEFORE Eli integrates; when OFF, full-auto (clean -> approved ->
// Eli lands). Persisted machine-locally per project; `effective` = `enabled`. The
// live effect lands once Kleiner/ReviewPipeline is manager-wired (a later slice).
export type SupervisedStatus = {
  enabled: boolean;    // the persisted toggle value (the sole control)
  effective: boolean;  // what the review pipeline uses; equals `enabled`
};

export function getSupervised(signal?: AbortSignal): Promise<SupervisedStatus> {
  return getJson<SupervisedStatus>('/api/agents/supervised', signal);
}

export function setSupervised(enabled: boolean, signal?: AbortSignal): Promise<SupervisedStatus> {
  return postJson<SupervisedStatus>('/api/agents/supervised', { enabled }, signal);
}

// -- four-pill HITL badge (#65) ----------------------------------------------------
// The notification-pill counts driving the nav badge + the Claude-tab pill row.
// Backend: GET /api/agents/pill-counts -> manager.pill_counts (derive_pill_counts).
//   green  -- completed work the operator hasn't acknowledged (succeeded runs).
//   yellow -- issues in needs-signoff (supervised PM gate; zero unless supervised).
//   purple -- issues at the #63 money wall (needs-trade-approval).
//   red    -- problems: needs-human + halted runs + the architecture freeze.
export type PillCounts = {
  green: number;
  yellow: number;
  purple: number;
  red: number;
};

export function getPillCounts(signal?: AbortSignal): Promise<PillCounts> {
  return getJson<PillCounts>('/api/agents/pill-counts', signal);
}

// Mark all currently-succeeded runs SEEN -- clears the green pill on viewing the
// completed view. Returns the new total acked count.
export function ackCompleted(signal?: AbortSignal): Promise<{ acked: number }> {
  return postJson<{ acked: number }>('/api/agents/completed/ack', {}, signal);
}

// Acknowledge a halted run so it drops out of the red problems pill. Idempotent.
// Returns the new total acknowledged count. The run stays listed (marked checkmark).
export function ackProblem(runId: string, signal?: AbortSignal): Promise<{ acked: number }> {
  return postJson<{ acked: number }>(
    `/api/agents/runs/${encodeURIComponent(runId)}/ack`, {}, signal,
  );
}

// The trading-surface approval brief for an issue parked at the #63 money wall:
// the plain-English brief + the issue title. Shown in the approve view.
export type TradeApprovalBrief = {
  number: number;
  title: string;
  brief: string;
};

export function getTradeApprovalBrief(number: number, signal?: AbortSignal): Promise<TradeApprovalBrief> {
  return getJson<TradeApprovalBrief>(`/api/agents/issues/${number}/trade-approval`, signal);
}

// The inbox of issues parked at the #63 money wall -- {number, title} each. The
// view lists these and opens the brief for the one the operator picks.
export type TradeApprovalSummary = { number: number; title: string };

export async function listTradeApprovals(signal?: AbortSignal): Promise<TradeApprovalSummary[]> {
  const res = await getJson<{ issues: TradeApprovalSummary[] }>('/api/agents/trade-approvals', signal);
  return res.issues;
}

// TRADING-SURFACE APPROVE -- the ONLY action on the money wall (no reject). Applies
// the trade-approved (+ approved) labels via the #63 two-pass handshake so Eli
// re-polls the issue and proceeds. Reusing the existing clearance label, NOT a new
// path. There is deliberately no reject counterpart.
export function tradeApproveIssue(number: number, signal?: AbortSignal): Promise<{ ok: boolean }> {
  return postJson<{ ok: boolean }>(`/api/agents/issues/${number}/trade-approve`, {}, signal);
}

// -- loop-health panel (the situational-awareness rail surface) ---------------------
// GET /api/agents/loop-status -> the operator's at-a-glance view of the autonomous
// loop: whether it's armed/frozen, how many runs are in flight, PR cap headroom,
// which stages are running, whether claims are being withheld and why, plus a
// blockers list (the things keeping the loop from clearing). Each blocker is
// either mechanically resolvable (a button -- close an orphan PR, clear a freeze)
// or a judgment call (needs_human -- no button, resolve in a Claude Code session).
export type LoopBlocker = {
  kind: 'needs_human' | 'frozen' | 'orphan_pr';
  severity: 'info' | 'action';
  resolvable: boolean;       // true == show a button (mechanical, no judgment)
  title: string;
  detail: string;
  issue: number | null;
  pr: number | null;
  // The mechanical unblock to POST back, or null for a judgment-call blocker.
  action: { type: 'clear_freeze' | 'close_pr'; pr?: number } | null;
};

export type LoopStatus = {
  armed: boolean;
  frozen: boolean;
  frozen_reason: string | null;
  in_flight: number;
  open_prs: number;
  pr_cap: number;
  stage_running: { claim: number; review: number; land: number };
  claims_withheld: boolean;
  withhold_reason: string | null;
  // Which gate caused the withhold. 'in_flight' is the BENIGN one (loop is healthy,
  // just serializing one issue at a time) -- the panel renders it as a quiet white
  // note; the rest are real holds needing an operator (amber/red).
  withhold_kind: 'disarmed' | 'frozen' | 'in_flight' | 'pr_cap' | null;
  blockers: LoopBlocker[];
  gh_ok: boolean;
};

export function getLoopStatus(signal?: AbortSignal): Promise<LoopStatus> {
  return getJson<LoopStatus>('/api/agents/loop-status', signal);
}

// Cheap glow/pulse signal for the Claude-tab AGENTS button. `active` = any agent
// run is in flight; `tick_seq` = a monotonic counter the manager bumps on every
// loop check (~30s) so the tab can flash on a REAL "checks for work" beat. No gh
// behind it -- safe to poll far more often than getLoopStatus.
export type AgentPulse = {
  active: boolean;
  tick_seq: number;
};

export function getPulse(signal?: AbortSignal): Promise<AgentPulse> {
  return getJson<AgentPulse>('/api/agents/pulse', signal);
}

// Perform a mechanical unblock (close an orphan PR, clear a freeze). The action
// is the one carried on a resolvable blocker. 400 on a bad action, 503 if the
// manager is down -- both surface via AgentApiError.message.
export function resolveBlocker(
  action: LoopBlocker['action'], signal?: AbortSignal,
): Promise<{ ok?: boolean; detail?: string } & Record<string, unknown>> {
  return postJson('/api/agents/loop-status/resolve', { action }, signal);
}

// -- run lifecycle ---------------------------------------------------------------

export async function listRuns(signal?: AbortSignal): Promise<RunRecord[]> {
  const res = await getJson<{ runs: RunRecord[] }>('/api/agents/runs', signal);
  return res.runs;
}

export function getRun(runId: string, signal?: AbortSignal): Promise<RunRecord> {
  return getJson<RunRecord>(`/api/agents/runs/${encodeURIComponent(runId)}`, signal);
}

export function getRunFeed(runId: string, signal?: AbortSignal): Promise<RunFeed> {
  return getJson<RunFeed>(`/api/agents/runs/${encodeURIComponent(runId)}/feed`, signal);
}

// Launch an agent now. Returns the new Run (status queued/running, or failed on
// launch). 404 if the agent is gone, 409 if the agent is OFF (a hard stop, #40),
// 503 if the agent-manager is unreachable.
export function runNow(agentId: string, signal?: AbortSignal): Promise<RunRecord> {
  return postJson<RunRecord>(`/api/agents/${encodeURIComponent(agentId)}/run`, {}, signal);
}

export function cancelRun(runId: string, signal?: AbortSignal): Promise<RunRecord | { cancelled: string }> {
  return postJson<RunRecord | { cancelled: string }>(
    `/api/agents/runs/${encodeURIComponent(runId)}/cancel`, {}, signal,
  );
}

// -- agent-manager lifecycle ----------------------------------------------------
// Restart the standalone agent-manager process (trident_agent_manager.py) that
// schedules and tracks runs. Halts scheduling + in-flight run tracking; the
// manager respawns lazily on the next agent API access, inheriting the
// dashboard's CURRENT environment -- this is how a freshly-set
// TRIDENT_AGENT_WATCH_ENABLED gate reaches it without a full dashboard restart.
//   method "rpc"         -- graceful shutdown RPC
//   method "pid-kill"    -- verified force-kill fallback
//   method "not-running" -- manager wasn't running; success no-op
export type AgentManagerRestartResult = {
  ok: boolean;
  method: 'rpc' | 'pid-kill' | 'not-running';
  detail: string;
};

export function restartAgentManager(signal?: AbortSignal): Promise<AgentManagerRestartResult> {
  return postJson<AgentManagerRestartResult>('/api/agents/manager/restart', {}, signal);
}

// -- staleness (loaded SHA vs repo HEAD -- issue #182) ----------------------------
// The agent-manager is a detached daemon that survives the dashboard restart, so
// a code fix is dead until the daemon is recycled. stale=True means the running
// code doesn't match the current checkout -- the dashboard shows a warning banner.

export type StalenessStatus = {
  loaded_sha: string | null;
  repo_head: string | null;
  stale: boolean;
};

export function getStaleness(signal?: AbortSignal): Promise<StalenessStatus> {
  return getJson<StalenessStatus>('/api/agents/staleness', signal);
}

// -- agent provider toggle (system-wide: all agents use Claude OR DeepSeek) --------
// GET /api/agents/provider  ->  {provider, deepseek_key_configured}
// POST /api/agents/provider  ->  set the provider + apply env vars

export type AgentProvider = 'claude' | 'deepseek';

export type AgentProviderStatus = {
  provider: AgentProvider;
  deepseek_key_configured: boolean;
};

export function getProvider(signal?: AbortSignal): Promise<AgentProviderStatus> {
  return getJson<AgentProviderStatus>('/api/agents/provider', signal);
}

export function setProvider(
  provider: AgentProvider,
  signal?: AbortSignal,
): Promise<AgentProviderStatus> {
  return postJson<AgentProviderStatus>('/api/agents/provider', { provider }, signal);
}

// -- consolidated agent-panel snapshot (#149) ------------------------------------
// Replaces six individual GETs (runs, pill-counts, pulse, loop-status, watch-gate,
// supervised) with one call. Each section is gathered independently with fail-soft
// on the backend -- a degraded section never throws the whole snapshot out.

export type AgentSnapshot = {
  runs: RunRecord[];            // log-stripped (activity[].log removed)
  pill_counts: PillCounts;
  pulse: AgentPulse;
  loop_health: LoopStatus;
  watch_gate: WatchGateStatus | Record<string, never>;
  supervised: SupervisedStatus | Record<string, never>;
  staleness: StalenessStatus;   // loaded SHA vs repo HEAD (#182)
};

export function getSnapshot(signal?: AbortSignal): Promise<AgentSnapshot> {
  return getJson<AgentSnapshot>('/api/agents/snapshot', signal);
}

// -- helpers --------------------------------------------------------------------

export const RUNNING_STATUSES: RunStatus[] = ['queued', 'running'];

export function isRunActive(status: RunStatus): boolean {
  return RUNNING_STATUSES.includes(status);
}
