// Problems view (#65 follow-up, 2026-06-23). Lists the agent runs that HALTED
// for a human -- the work behind the red pill. Before this, the red pill was a
// status-only chip with no click target, so a halt's reason was unreachable from
// the dashboard ("2 red indicators that do nothing when I click them"). Each card
// surfaces the halt reason inline (the run record's `outcome`, which already holds
// the full "Stage / Reason / Detail" text the manager filed) and deep-links into
// the authoritative RunDetail (feed + live terminal) on click.
//
// Read-only summary: the actual handling (relabel the issue, re-arm, etc.) still
// happens on GitHub / in RunDetail. This just makes "what broke?" answerable.

import type { RunRecord } from '../../api/agents';
import type { Theme } from '../../theme';

function fmtWhen(ts: number | null): string {
  if (!ts) return '--';
  const secs = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (secs < 60) return `${secs}s ago`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

// Pull the human-meaningful "Reason: ..." line out of the manager's multi-line halt
// outcome for the card headline; fall back to the whole thing (or the first line)
// when it isn't in the expected shape.
function reasonLine(outcome: string | null): string {
  if (!outcome) return 'halted (no reason recorded)';
  const m = outcome.match(/Reason:\s*(.+)/i);
  if (m) return m[1].trim();
  return outcome.split('\n')[0].trim();
}

export function ProblemsView({
  theme, runs, onSelectRun,
}: {
  theme: Theme; runs: RunRecord[];
  onSelectRun: (runId: string) => void;
}) {
  const halted = runs.filter(r => r.status === 'halted');
  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflowY: 'auto' }}>
      <div style={{
        padding: '10px 14px', borderBottom: `1px solid ${theme.border}`,
        fontFamily: theme.fontBody, fontSize: 14, fontWeight: 600, color: theme.text,
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <span style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          minWidth: 20, height: 20, borderRadius: 10,
          background: theme.red, color: theme.bg0,
          fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
        }}>{halted.length}</span>
        Problems
      </div>
      {halted.length === 0 && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
        }}>no problems</div>
      )}
      {halted.map(r => (
        <div key={r.run_id} onClick={() => onSelectRun(r.run_id)}
          style={{
            display: 'flex', alignItems: 'flex-start', gap: 8,
            padding: '8px 14px', borderBottom: `1px solid ${theme.border}`,
            cursor: 'pointer',
          }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%', flexShrink: 0, marginTop: 4,
            background: theme.red,
          }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{
              fontFamily: theme.fontMono, fontSize: 11, color: theme.text,
              fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{r.agent_id}</div>
            <div style={{
              fontFamily: theme.fontMono, fontSize: 10, color: theme.red,
              marginTop: 2, lineHeight: 1.3,
            }}>{reasonLine(r.outcome)}</div>
            <div style={{
              fontFamily: theme.fontMono, fontSize: 9, color: theme.text3, marginTop: 2,
            }}>
              {fmtWhen(r.finished_at)}
              {r.acked ? ' -- acknowledged' : ''}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
