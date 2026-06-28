// Create / edit an agent definition (issue #27 CRUD surface). A thin form over
// PUT /api/agents -- the backend (trident_agent_store.validate_agent) is the
// source of truth for validation, so a bad definition comes back as a 400 whose
// reason we surface inline rather than re-implementing every rule here.

import { useState } from 'react';
import {
  AGENT_MODELS, upsertAgent, deleteAgent,
  type AgentDefinition, type AgentModel, type AgentTrigger, type WatchStage,
} from '../../api/agents';
import { type Theme } from '../../theme';

type Props = {
  theme: Theme;
  accent: string;
  // The agent being edited, or null for a fresh create.
  agent: AgentDefinition | null;
  onSaved: (a: AgentDefinition) => void;
  onDeleted: (id: string) => void;
  onCancel: () => void;
};

const SLUG_RE = /^[a-z0-9][a-z0-9_-]*$/;

export function AgentForm({ theme, accent, agent, onSaved, onDeleted, onCancel }: Props) {
  const editing = agent !== null;
  const [id, setId] = useState(agent?.id ?? '');
  const [name, setName] = useState(agent?.name ?? '');
  const [description, setDescription] = useState(agent?.description ?? '');
  const [model, setModel] = useState<AgentModel>(agent?.model ?? 'claude');
  const [timeoutMin, setTimeoutMin] = useState(agent?.timeout_minutes ?? 60);
  const [triggerKind, setTriggerKind] = useState<'watch' | 'clock'>(agent?.trigger.kind ?? 'clock');
  // Trigger-specific state.
  const [watchInterval, setWatchInterval] = useState(
    (agent?.trigger as { kind: string; poll_interval?: number })?.poll_interval ?? 30,
  );
  const [watchCondition, setWatchCondition] = useState(
    (agent?.trigger as { kind: string; condition?: string })?.condition ?? '',
  );
  const [watchStage, setWatchStage] = useState<WatchStage>(
    (agent?.trigger as { kind: string; stage?: WatchStage })?.stage ?? 'claim',
  );
  const [schedule, setSchedule] = useState(
    (agent?.trigger as { kind: string; schedule?: string })?.schedule ?? '0 * * * *',
  );
  const [prompt, setPrompt] = useState(agent?.prompt ?? '');
  const [saving, setSaving] = useState(false);
  const [savingError, setSavingError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function handleSave() {
    setSavingError(null);
    if (!id || !name || !prompt) {
      setSavingError('id, name, and prompt are required');
      return;
    }
    if (!SLUG_RE.test(id)) {
      setSavingError('id must start with a letter and contain only lowercase letters, digits, underscores, and hyphens');
      return;
    }
    const trigger: AgentTrigger = triggerKind === 'clock'
      ? { kind: 'clock', schedule }
      : { kind: 'watch', poll_interval: watchInterval, condition: watchCondition || 'always', stage: watchStage };
    const defn: AgentDefinition = {
      id, name, description: description || undefined,
      model, timeout_minutes: timeoutMin,
      trigger,
      prompt,
      options: {},
      enabled: agent?.enabled ?? true,
    };
    setSaving(true);
    try {
      const saved = await upsertAgent(defn);
      onSaved(saved);
    } catch (e) {
      setSavingError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!agent) return;
    if (!window.confirm(`Delete agent "${agent.name}"? This cannot be undone.`)) return;
    setDeleting(true);
    try {
      await deleteAgent(agent.id);
      onDeleted(agent.id);
    } catch (e) {
      setSavingError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  }

  const inputStyle = (): React.CSSProperties => ({
    width: '100%',
    padding: '7px 10px',
    fontFamily: theme.fontMono, fontSize: 12,
    color: theme.text,
    background: theme.bg2,
    border: `1px solid ${theme.border}`,
    borderRadius: 4,
    outline: 'none',
    boxSizing: 'border-box',
  });

  return (
    <div style={{
      flex: 1, minHeight: 0, overflowY: 'auto',
      padding: 16, display: 'flex', flexDirection: 'column', gap: 12,
    }}>
      <div style={{ fontFamily: theme.fontBody, fontSize: 14, fontWeight: 600, color: theme.text }}>
        {editing ? `Edit agent: ${agent!.name}` : 'New agent'}
      </div>

      <label style={labelStyle(theme)}>ID
        <input value={id} onChange={e => setId(e.target.value)} disabled={editing}
          placeholder="my-agent" style={inputStyle()} /></label>
      <label style={labelStyle(theme)}>Name
        <input value={name} onChange={e => setName(e.target.value)}
          placeholder="My Agent" style={inputStyle()} /></label>
      <label style={labelStyle(theme)}>Description
        <input value={description} onChange={e => setDescription(e.target.value)}
          placeholder="Optional one-liner" style={inputStyle()} /></label>
      <label style={labelStyle(theme)}>Model
        <select value={model} onChange={e => setModel(e.target.value as AgentModel)}
          style={inputStyle()}>
          {AGENT_MODELS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
        </select></label>
      <label style={labelStyle(theme)}>Timeout (minutes)
        <input type="number" value={timeoutMin} onChange={e => setTimeoutMin(Math.max(1, parseInt(e.target.value) || 60))}
          min={1} style={inputStyle()} /></label>
      <label style={labelStyle(theme)}>Trigger kind
        <select value={triggerKind} onChange={e => setTriggerKind(e.target.value as 'clock' | 'watch')}
          style={inputStyle()}>
          <option value="watch">Watch (GitHub polling)</option>
          <option value="clock">Clock (cron)</option>
        </select></label>
      {triggerKind === 'watch' ? (
        <>
          <label style={labelStyle(theme)}>Poll interval (seconds)
            <input type="number" value={watchInterval}
              onChange={e => setWatchInterval(Math.max(10, parseInt(e.target.value) || 30))}
              min={10} style={inputStyle()} /></label>
          <label style={labelStyle(theme)}>Condition
            <input value={watchCondition} onChange={e => setWatchCondition(e.target.value)}
              placeholder="always" style={inputStyle()} /></label>
          <label style={labelStyle(theme)}>Stage
            <select value={watchStage} onChange={e => setWatchStage(e.target.value as WatchStage)}
              style={inputStyle()}>
              <option value="claim">Claim (Gordon -- issue to PR)</option>
              <option value="review">Review (Kleiner -- PR review)</option>
              <option value="land">Land (Eli -- merge + deploy)</option>
            </select></label>
        </>
      ) : (
        <label style={labelStyle(theme)}>Cron schedule
          <input value={schedule} onChange={e => setSchedule(e.target.value)}
            placeholder="0 * * * *" style={inputStyle()} /></label>
      )}
      <label style={labelStyle(theme)}>Prompt
        <textarea value={prompt} onChange={e => setPrompt(e.target.value)}
          rows={8} placeholder="System prompt for the agent..."
          style={{ ...inputStyle(), resize: 'vertical', minHeight: 100 }} /></label>

      {savingError && (
        <div style={{
          padding: '8px 12px', fontFamily: theme.fontMono, fontSize: 10,
          color: theme.red, background: `${theme.red}14`, borderRadius: 4,
        }}>{savingError}</div>
      )}

      <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        {editing && (
          <button onClick={handleDelete} disabled={deleting}
            style={btnStyle(theme, theme.red)}>
            {deleting ? 'Deleting…' : 'Delete'}
          </button>
        )}
        <button onClick={onCancel} style={btnStyle(theme, theme.text2)}>Cancel</button>
        <button onClick={handleSave} disabled={saving}
          style={{ ...btnStyle(theme, accent), fontWeight: 700 }}>
          {saving ? 'Saving…' : (editing ? 'Save' : 'Create')}
        </button>
      </div>
    </div>
  );
}

function labelStyle(theme: Theme): React.CSSProperties {
  return {
    display: 'flex', flexDirection: 'column', gap: 4,
    fontFamily: theme.fontMono, fontSize: 10, color: theme.text2,
    letterSpacing: '0.04em', textTransform: 'uppercase',
  };
}

function btnStyle(theme: Theme, color: string): React.CSSProperties {
  return {
    padding: '6px 16px', border: `1px solid ${color}55`, borderRadius: 4,
    background: `${color}14`, color,
    fontFamily: theme.fontMono, fontSize: 11, cursor: 'pointer',
  };
}
