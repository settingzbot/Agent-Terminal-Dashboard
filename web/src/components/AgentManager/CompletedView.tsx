// Completed-work view (#65). Lists the agent runs that finished successfully --
// the work behind the green pill. Opening this view ACKs the completed set on the
// backend (the parent fires ackCompleted before rendering this), so the green pill
// "clears on viewing": once seen, the count drops to 0 until a NEW run succeeds.
//
// This is a read-only summary; there is no action here (acking is implicit in the
// open). The authoritative run detail + feed still lives in RunDetail.

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

export function CompletedView({
  theme, runs,
}: {
  theme: Theme; runs: RunRecord[];
}) {
  const done = runs.filter(r => r.status === 'succeeded');
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
          background: theme.green, color: theme.bg0,
          fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
        }}>{done.length}</span>
        Completed work
      </div>
      {done.length === 0 && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
        }}>no completed runs yet</div>
      )}
      {done.map(r => (
        <div key={r.run_id} style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '8px 14px', borderBottom: `1px solid ${theme.border}`,
        }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
            background: theme.green,
          }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{
              fontFamily: theme.fontMono, fontSize: 11, color: theme.text,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>{r.agent_id}</div>
            <div style={{
              fontFamily: theme.fontMono, fontSize: 9, color: theme.text3,
            }}>{fmtWhen(r.finished_at)}</div>
          </div>
          {r.outcome && (
            <span style={{
              fontFamily: theme.fontMono, fontSize: 9, color: theme.text2,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              maxWidth: 200,
            }}>{r.outcome}</span>
          )}
        </div>
      ))}
    </div>
  );
}
