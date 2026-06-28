// One agent run in detail (issues #30/#31/#32). A status header over a two-way
// toggle between the structured FEED (parsed transcript) and the live TERMINAL
// (xterm attached to the run's pty session, so the operator can watch and
// intervene). Cancel is available while the run is active.

import { useRef, useState } from 'react';
import { ackProblem, cancelRun, isRunActive, type RunRecord, type RunStatus } from '../../api/agents';
import { type Theme } from '../../theme';
import { RunFeed, type RunFeedHandle } from './RunFeed';
import { RunTerminal, type RunTerminalHandle } from './RunTerminal';

type Props = {
  theme: Theme;
  accent: string;
  run: RunRecord;
  isMobile?: boolean;
  // Bubble a status change (e.g. after cancel) so the list refreshes.
  onChanged: () => void;
};

type View = 'feed' | 'terminal';

const STATUS_COLOR = (theme: Theme, s: RunStatus): string => ({
  queued: theme.amber,
  running: theme.green,
  succeeded: theme.green,
  failed: theme.red,
  cancelled: theme.text3,
  halted: theme.red,
}[s]);

export function RunDetail({ theme, accent, run, isMobile = false, onChanged }: Props) {
  const [view, setView] = useState<View>('feed');
  const [cancelling, setCancelling] = useState(false);
  const feedRef = useRef<RunFeedHandle>(null);
  const termRef = useRef<RunTerminalHandle>(null);
  const active = isRunActive(run.status);
  const halted = run.status === 'halted';

  const handleCancel = async () => {
    if (!window.confirm('Cancel this run? The agent session will be killed.')) return;
    setCancelling(true);
    try {
      await cancelRun(run.run_id);
      onChanged();
    } catch {
      // Error surfaces through the parent's error line.
    } finally {
      setCancelling(false);
    }
  };

  // Ack a halted problem so it drops out of the red pill (but stays listed, marked).
  const handleAck = async () => {
    try {
      await ackProblem(run.run_id);
      onChanged();
    } catch {
      // Error surfaces through the parent.
    }
  };

  const headerStyle: React.CSSProperties = {
    padding: '10px 14px', borderBottom: `1px solid ${theme.border}`,
    display: 'flex', flexDirection: 'column', gap: 6,
  };

  return (
    <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* Status header */}
      <div style={headerStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{
            width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
            background: STATUS_COLOR(theme, run.status),
            boxShadow: active ? `0 0 0 3px ${theme.green}22` : 'none',
            animation: active ? 'tridentPulse 2s ease-in-out infinite' : 'none',
          }} />
          <span style={{
            fontFamily: theme.fontBody, fontSize: 14, fontWeight: 600, color: theme.text,
          }}>{run.agent_id}</span>
          <span style={{
            fontFamily: theme.fontMono, fontSize: 10, color: theme.text2,
            letterSpacing: '0.04em', marginLeft: 'auto',
          }}>{run.status.toUpperCase()}</span>
        </div>
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          {/* Feed / Terminal toggle */}
          <button onClick={() => setView('feed')}
            style={tabBtn(theme, accent, view === 'feed')}>Feed</button>
          <button onClick={() => setView('terminal')}
            style={tabBtn(theme, accent, view === 'terminal')}>Terminal</button>
          <div style={{ flex: 1 }} />
          {active && (
            <button onClick={handleCancel} disabled={cancelling}
              style={{ ...actionBtn(theme, theme.red), marginLeft: 'auto' }}>
              {cancelling ? '…' : 'Cancel'}
            </button>
          )}
          {halted && !run.acked && (
            <button onClick={handleAck}
              style={actionBtn(theme, accent)}>Acknowledge</button>
          )}
        </div>
        {run.outcome && (
          <div style={{
            fontFamily: theme.fontMono, fontSize: 10, color: theme.text2,
            background: `${theme.bg2}44`, padding: '6px 10px', borderRadius: 4,
            whiteSpace: 'pre-wrap', lineHeight: 1.4, maxHeight: 80, overflowY: 'auto',
          }}>{run.outcome}</div>
        )}
      </div>

      {/* Content: feed or terminal */}
      {view === 'feed' && (
        <RunFeed ref={feedRef} runId={run.run_id} theme={theme} accent={accent}
          live={active} />
      )}
      {view === 'terminal' && run.session_id && (
        <RunTerminal ref={termRef} sessionId={run.session_id} theme={theme}
          accent={accent} alive={active} isMobile={isMobile} />
      )}
      {view === 'terminal' && !run.session_id && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: theme.fontMono, fontSize: 11, color: theme.text3,
        }}>
          {run.status === 'queued' ? 'waiting to start…' : 'no terminal session for this run'}
        </div>
      )}
    </div>
  );
}

function tabBtn(theme: Theme, accent: string, on: boolean): React.CSSProperties {
  return {
    padding: '4px 12px', border: `1px solid ${on ? accent : theme.border}`,
    borderRadius: 4, background: on ? `${accent}14` : 'transparent',
    color: on ? accent : theme.text2, fontFamily: theme.fontMono, fontSize: 10,
    fontWeight: on ? 700 : 400, cursor: 'pointer',
  };
}

function actionBtn(theme: Theme, color: string): React.CSSProperties {
  return {
    padding: '4px 14px', border: `1px solid ${color}55`, borderRadius: 4,
    background: `${color}14`, color, fontFamily: theme.fontMono, fontSize: 10,
    fontWeight: 600, cursor: 'pointer',
  };
}
