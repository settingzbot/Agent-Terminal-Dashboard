// Client for the dashboard's terminal-session endpoints. Backend lives in
// trident_terminal.py (PTY registry) + a small section of trident_dashboard.py
// (REST + /ws/terminal/{id} websocket).
//
// Sessions are PowerShell PTYs at the repo root. They persist across browser
// disconnects until killed via DELETE or until the dashboard process exits.

export type TerminalSession = {
  id: string;
  label: string;
  cwd: string;
  created_at: number;  // unix seconds
  alive: boolean;
  // Opaque tag set at create time. Agent-run sessions carry "agent:<run-id>"
  // (trident_pty_manager #25) so the UI can show them as `agent N` tabs and
  // tell them apart from the operator's own shells. Absent on normal sessions.
  tag?: string | null;
  command?: string | null;
};

class TerminalApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(`Terminal API ${status}: ${message}`);
    this.name = 'TerminalApiError';
    this.status = status;
  }
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal });
  if (!res.ok) throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal,
  });
  if (!res.ok) throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function delJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { method: 'DELETE', signal });
  if (!res.ok) throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

async function patchJson<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  return res.json() as Promise<T>;
}

export async function listSessions(signal?: AbortSignal): Promise<TerminalSession[]> {
  const res = await getJson<{ sessions: TerminalSession[] }>('/api/terminal/sessions', signal);
  return res.sessions;
}

export function createSession(label?: string, signal?: AbortSignal): Promise<TerminalSession> {
  return postJson<TerminalSession>('/api/terminal/sessions', label ? { label } : {}, signal);
}

export function killSession(id: string, signal?: AbortSignal): Promise<{ killed: string }> {
  return delJson<{ killed: string }>(`/api/terminal/sessions/${encodeURIComponent(id)}`, signal);
}

export function renameSession(id: string, label: string, signal?: AbortSignal): Promise<{ renamed: string; label: string }> {
  return patchJson<{ renamed: string; label: string }>(
    `/api/terminal/sessions/${encodeURIComponent(id)}`, { label }, signal,
  );
}

// Restart the standalone PTY manager (trident_pty_manager.py) that owns every
// terminal session. This KILLS all terminal tabs -- sessions cannot be
// recovered. The manager respawns lazily on the next terminal API access, so
// no explicit start call exists (or is needed).
//   method "rpc"         -- graceful shutdown RPC (sessions killed cleanly)
//   method "pid-kill"    -- verified force-kill fallback (old/hung manager)
//   method "not-running" -- manager wasn't running; success no-op
export type ManagerRestartResult = {
  ok: boolean;
  method: 'rpc' | 'pid-kill' | 'not-running';
  detail: string;
};

export function restartTerminalManager(signal?: AbortSignal): Promise<ManagerRestartResult> {
  return postJson<ManagerRestartResult>('/api/terminal/manager/restart', {}, signal);
}

export function terminalWsUrl(id: string, takeover?: { rows: number; cols: number }): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  // ?bin=1 -- this bundle accepts terminal output as raw binary WS frames
  // (UTF-8 PTY bytes, no JSON envelope). Declared in the URL rather than a
  // post-open hello so the server knows the framing BEFORE it sends its
  // first frame (the screen-state replay fires immediately on attach). An
  // old dashboard ignores the query param and keeps sending JSON
  // {type:'out'} text frames, which this bundle still understands.
  //
  // ?takeover=1&rows=&cols= -- the dual-purpose ↻ refresh: this device claims
  // the resize lock and sizes the shared PTY to its own display BEFORE the
  // replay is serialized, so history reflows to THIS device's width instead of
  // running off the edge (sized for whatever device last held the lock). In
  // the URL for the same reason as ?bin=1 -- the manager attach fires at accept
  // time, before any message. An old server ignores the params (normal attach).
  let url = `${proto}//${location.host}/ws/terminal/${encodeURIComponent(id)}?bin=1`;
  if (takeover && takeover.rows > 0 && takeover.cols > 0) {
    url += `&takeover=1&rows=${takeover.rows}&cols=${takeover.cols}`;
  }
  return url;
}

// --- Claude image attachments ---------------------------------------------------
// Uploads a single image to the dashboard, which saves it under
// .tmp/claude_uploads/ on the laptop and returns the absolute path. The
// Claude tab's chat box pastes that path so the user can include it in their
// message -- Claude Code reads the file off disk via its Read tool.
//
// Send the image as the raw POST body (Content-Type: image/<ext>) instead of
// multipart/form-data so the bot host doesn't need a python-multipart dep.

export type ClaudeImageUpload = {
  path: string;      // absolute path on the dashboard host (Windows)
  filename: string;  // basename, e.g. img-1735930812-a3f1c2d4.png
  size: number;      // bytes written
};

export async function uploadClaudeImage(
  file: File | Blob,
  signal?: AbortSignal,
): Promise<ClaudeImageUpload> {
  const contentType = file.type || 'application/octet-stream';
  const res = await fetch('/api/claude/upload-image', {
    method: 'POST',
    headers: { 'Content-Type': contentType },
    body: file,
    signal,
  });
  if (!res.ok) {
    throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  }
  return res.json() as Promise<ClaudeImageUpload>;
}

// --- Claude file attachments (general) ------------------------------------------
// 2026-06-10 generalization of the image pipe: zips, text/markdown, images.
// Same raw-body transport; the original filename rides in the X-Filename
// header (URI-encoded -- header values must be ISO-8859-1-safe) so the server
// can keep a recognizable name + pick the extension. Server allowlists
// extensions and returns 400 with the allowed list for anything else.

export async function uploadClaudeFile(
  file: File | Blob,
  signal?: AbortSignal,
): Promise<ClaudeImageUpload> {
  const contentType = file.type || 'application/octet-stream';
  const name = file instanceof File ? file.name : '';
  const headers: Record<string, string> = { 'Content-Type': contentType };
  if (name) headers['X-Filename'] = encodeURIComponent(name);
  const res = await fetch('/api/claude/upload-file', {
    method: 'POST',
    headers,
    body: file,
    signal,
  });
  if (!res.ok) {
    throw new TerminalApiError(res.status, await res.text().catch(() => ''));
  }
  return res.json() as Promise<ClaudeImageUpload>;
}
