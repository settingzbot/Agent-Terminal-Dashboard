// Loop-health panel -- the operator's at-a-glance situational-awareness surface
// in the AgentManager left rail (placed right after PillRow, before AGENTS). Two
// parts: always-visible STATUS ROWS (a headline + a compact stats line), and a
// BLOCKERS LIST that only appears when something is keeping the loop from clearing.
//
// Mechanically-resolvable blockers (close an orphan PR, clear a freeze) render a
// confirm-then-POST button; judgment-call blockers (needs_human) render no button
// -- just a quiet "resolve in a Claude Code session" hint. The panel only informs;
// the human owns the judgment.
//
// Inline-styled / theme-token idiom matches AgentManagerPane's WatchGateBar /
// SupervisedBar / PillRow exactly -- no CSS framework, no new deps.

import { type Theme } from '../../theme';
import { type LoopStatus, type LoopBlocker, type StalenessStatus } from '../../api/agents';

type Props = {
  theme: Theme;
  accent: string;
  status: LoopStatus | null;
  // The action currently mid-resolve (its button shows a busy/disabled state).
  // We key on the action object so only the clicked blocker's button is busy.
  busyAction: LoopBlocker['action'] | null;
  // Called AFTER the in-component window.confirm -- the parent does the POST + refresh.
  onResolve: (action: NonNullable<LoopBlocker['action']>) => void;
  // Manager staleness (loaded SHA != repo HEAD, #182). When true, "stale brain"
  // is shown inline in the stats line instead of as a separate banner.
  staleness?: StalenessStatus | null;
};

export function LoopHealthPanel({
  theme, accent, status, busyAction, onResolve, staleness,
}: Props) {
  if (!status) return null;

  // Render each blocker as a small card with a title, detail, and optional button.
  // Judgment-call blockers (needs_human) get no button; mechanical ones get one.
  const st = status;

  return (
    <div style={{
      borderBottom: `1px solid ${theme.border}`,
      padding: '8px 12px',
      background: `${theme.bg2}33`,
      flexShrink: 0,
    }}>
      {/* Headline */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
        fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
        color: st.armed ? accent : theme.text2,
        letterSpacing: '0.06em', marginBottom: 4,
      }}>
        <span>LOOP HEALTH</span>
        <span style={{
          color: st.armed ? theme.green : theme.text3,
          fontWeight: 600,
        }}>
          {st.armed ? 'ARMED' : 'DISARMED'}
        </span>
      </div>

      {/* Compact stats line -- always visible */}
      <div style={{
        display: 'flex', gap: 8, flexWrap: 'wrap',
        fontFamily: theme.fontMono, fontSize: 9, color: theme.text2,
      }}>
        <span>in-flight: {st.in_flight}</span>
        <span>PRs: {st.open_prs}/{st.pr_cap}</span>
        <span>gh: {st.gh_ok ? 'ok' : 'error'}</span>
        {st.claims_withheld && (
          <span style={{
            color: st.withhold_kind === 'in_flight' ? theme.text2 : theme.amber,
          }}>withheld ({st.withhold_kind ?? 'unknown'})</span>
        )}
        {st.frozen && (
          <span style={{ color: theme.red }}>FROZEN</span>
        )}
        {staleness?.stale && (
          <span style={{ color: theme.amber }}>stale brain</span>
        )}
      </div>

      {/* Staleness detail (when stale) */}
      {staleness?.stale && (
        <div style={{
          marginTop: 4, fontFamily: theme.fontMono, fontSize: 8.5,
          color: theme.amber, lineHeight: 1.3,
        }}>
          loaded {staleness.loaded_sha?.slice(0, 7) ?? '?'} vs HEAD {staleness.repo_head?.slice(0, 7) ?? '?'}
          -- restart the manager
        </div>
      )}

      {/* Blocker list */}
      {st.blockers.length > 0 && (
        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {st.blockers.map((b, i) => (
            <BlockerCard key={i} blocker={b} theme={theme} accent={accent}
              busy={busyAction !== null &&
                b.action !== null &&
                busyAction?.type === b.action.type &&
                busyAction?.pr === b.action.pr}
              onResolve={onResolve} />
          ))}
        </div>
      )}
    </div>
  );
}

function BlockerCard({
  blocker: b, theme, accent, busy, onResolve,
}: {
  blocker: LoopBlocker; theme: Theme; accent: string;
  busy: boolean; onResolve: (action: NonNullable<LoopBlocker['action']>) => void;
}) {
  const severityColor = b.severity === 'action' ? theme.red : theme.amber;
  const showBtn = b.resolvable && b.action !== null;

  const handleClick = () => {
    if (!b.action) return;
    const label = b.action.type === 'close_pr'
      ? `Close PR #${b.action.pr}?`
      : 'Clear the architecture freeze?';
    if (window.confirm(label)) {
      onResolve(b.action!);
    }
  };

  return (
    <div style={{
      padding: '5px 8px', borderRadius: 4,
      background: `${severityColor}0a`,
      borderLeft: `2px solid ${severityColor}`,
    }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 6,
        fontFamily: theme.fontMono, fontSize: 9, color: theme.text,
        fontWeight: 600, marginBottom: 2,
      }}>
        <span style={{ color: severityColor }}>{b.kind.replace('_', ' ')}</span>
        {b.issue && <span style={{ color: theme.text3 }}>#{b.issue}</span>}
        {b.pr && <span style={{ color: theme.blue }}>PR #{b.pr}</span>}
      </div>
      <div style={{
        fontFamily: theme.fontMono, fontSize: 9, color: theme.text2,
        lineHeight: 1.3, marginBottom: showBtn ? 4 : 0,
      }}>
        {b.detail}
      </div>
      {showBtn && (
        <button onClick={handleClick} disabled={busy}
          style={{
            padding: '2px 8px', border: `1px solid ${accent}55`, borderRadius: 3,
            background: `${accent}14`, color: accent,
            fontFamily: theme.fontMono, fontSize: 8.5, fontWeight: 600,
            cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.5 : 1,
          }}>
          {busy ? '…' : (b.action?.type === 'close_pr' ? `Close PR #${b.action.pr}` : 'Clear freeze')}
        </button>
      )}
    </div>
  );
}
