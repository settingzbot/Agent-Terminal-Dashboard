// Trading-surface approval view (#65, the #63 money wall). When Dr. Eli Vance's diff
// touches the live-trading surface (the money path), Eli REFUSES to auto-integrate
// and parks the issue at the wall. This view is where the operator clears it.
//
// The money wall is cleared by APPROVAL, period -- there is deliberately NO reject
// / "not now" button. Approving applies the `trade-approved` clearance label (the
// #63 two-pass handshake) so Eli re-polls the issue and lands it the normal way.
//
// Layout: a list of the issues currently at the wall (usually one -- the loop is
// strict-serial); pick one to read its plain-English brief, then a single Approve.

import { useCallback, useEffect, useState } from 'react';
import {
  listTradeApprovals, getTradeApprovalBrief, tradeApproveIssue,
  type TradeApprovalSummary, type TradeApprovalBrief,
} from '../../api/agents';
import { PILL_PURPLE } from '../PillBadge';
import type { Theme } from '../../theme';

export function TradeApprovalView({
  theme, accent, onApproved,
}: {
  theme: Theme; accent: string;
  /** Called after a successful approve so the parent can refresh the pill + close. */
  onApproved: () => void;
}) {
  const [issues, setIssues] = useState<TradeApprovalSummary[]>([]);
  const [picked, setPicked] = useState<number | null>(null);
  const [brief, setBrief] = useState<TradeApprovalBrief | null>(null);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    void (async () => {
      try {
        const list = await listTradeApprovals(ctrl.signal);
        if (ctrl.signal.aborted) return;
        setIssues(list);
        setError(null);
      } catch (e) {
        if (!ctrl.signal.aborted) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    })();
    return () => ctrl.abort();
  }, []);

  const handlePick = useCallback(async (number: number) => {
    setPicked(number);
    setBrief(null);
    try {
      const b = await getTradeApprovalBrief(number);
      setBrief(b);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const handleApprove = useCallback(async () => {
    if (picked === null) return;
    setApproving(true);
    setError(null);
    try {
      await tradeApproveIssue(picked);
      onApproved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setApproving(false);
    }
  }, [picked, onApproved]);

  const containerStyle: React.CSSProperties = {
    flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden',
  };

  return (
    <div style={containerStyle}>
      <div style={{
        padding: '10px 14px', borderBottom: `1px solid ${theme.border}`,
        fontFamily: theme.fontBody, fontSize: 14, fontWeight: 600, color: theme.text,
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        <span style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          minWidth: 20, height: 20, borderRadius: 10,
          background: PILL_PURPLE, color: theme.bg0,
          fontFamily: theme.fontMono, fontSize: 10, fontWeight: 700,
        }}>{issues.length}</span>
        Trading-surface approval
      </div>

      {loading && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
        }}>loading…</div>
      )}

      {error && (
        <div style={{
          padding: '8px 12px', fontFamily: theme.fontMono, fontSize: 10,
          color: theme.red, background: `${theme.red}14`, borderRadius: 4, margin: 8,
        }}>{error}</div>
      )}

      {!loading && !error && issues.length === 0 && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
        }}>no issues at the money wall</div>
      )}

      {!loading && issues.length > 0 && (
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
          {/* Issue list */}
          <div style={{ overflowY: 'auto', flexShrink: 0, maxHeight: 200 }}>
            {issues.map(issue => (
              <button key={issue.number} onClick={() => handlePick(issue.number)}
                style={{
                  display: 'block', width: '100%', textAlign: 'left',
                  padding: '8px 14px', border: 'none', borderBottom: `1px solid ${theme.border}`,
                  background: picked === issue.number ? `${accent}14` : 'transparent',
                  color: picked === issue.number ? accent : theme.text,
                  fontFamily: theme.fontMono, fontSize: 11, cursor: 'pointer',
                }}>
                <span style={{ fontWeight: 700 }}>#{issue.number}</span>
                {' '}{issue.title}
              </button>
            ))}
          </div>

          {/* Brief + approve */}
          {picked !== null && (
            <div style={{
              flex: 1, padding: 14, overflowY: 'auto',
              display: 'flex', flexDirection: 'column', gap: 10,
            }}>
              <div style={{
                fontFamily: theme.fontBody, fontSize: 12, color: theme.text,
                lineHeight: 1.5, whiteSpace: 'pre-wrap',
                background: `${theme.bg2}44`, padding: 12, borderRadius: 6,
              }}>{brief ? brief.brief : 'loading brief…'}</div>
              <button onClick={handleApprove} disabled={approving || !brief}
                style={{
                  alignSelf: 'flex-start',
                  padding: '8px 24px', border: 'none', borderRadius: 6,
                  background: PILL_PURPLE, color: theme.bg0,
                  fontFamily: theme.fontMono, fontSize: 11, fontWeight: 700,
                  cursor: (approving || !brief) ? 'default' : 'pointer',
                  opacity: (approving || !brief) ? 0.5 : 1,
                }}>
                {approving ? 'Approving…' : 'Approve'}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
