# Claude Dashboard — Claude Code operations dashboard

Standalone web dashboard for Claude Code: terminal sessions + autonomous agent orchestration. Extracted from the Trident trading bot project (settingzbot/Trident) as a shareable reference implementation.

## Architecture

Two standalone daemons + FastAPI server + React frontend:

```
Browser (React + xterm.js)
  │
  ├─ HTTP/WS ─► FastAPI (server/app.py, port 8080)
  │                │
  │                ├─ TCP 58999 ─► PTY Manager (daemons/pty_manager.py)
  │                │                  └─ Spawns PowerShell ConPTY sessions
  │                │
  │                └─ TCP 58998 ─► Agent Manager (daemons/agent_manager.py)
  │                                   └─ Agent scheduler, run lifecycle, pipelines
  │
  └─ Static files served from web/dist/ (production) or Vite dev server (dev)
```

Both daemons are lazy-spawned on first request and survive server restarts. They write PID files to `logs/`.

## Quick start

```bash
# Terminal 1 — backend
pip install -r requirements.txt
python -m uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8080

# Terminal 2 — frontend dev server
cd web
npm install
npm run dev
```

Open http://localhost:5173 (dev) or http://localhost:8080 (production build).

## The agent system

Three reference agent pipelines are included (extracted from Trident's autonomous dev fleet):

| Agent | Role | Pipeline module |
|-------|------|----------------|
| **Gordon** | Claims `ready-for-agent` issues, implements them, opens PRs | `agents/pipeline.py` |
| **Kleiner** | Reviews PRs, files fix requests, runs the fixer | `agents/review_pipeline.py` |
| **Eli** | Lands reviewed PRs (merge, test, push to main) | `agents/eli_pipeline.py` |

These are **reference implementations** — they're wired to GitHub Issues on `settingzbot/Trident` and assume a specific workflow (issue labels, PR conventions, trade-approval gates). To adapt them for your own project, see the fleet workers guide below.

### Defining your own agents

Agent definitions are JSON files in `agents/` (one per agent). Create them via the Agent Manager UI or by writing the JSON directly. Key fields:

- `model`: `claude`, `claude-ds` (DeepSeek), `claude-go`, `claude-ds-go`
- `trigger`: `watch` (GitHub issue-driven) or `clock` (cron schedule)
- `watch_stage`: `claim`, `review`, or `land`
- `prompt_template`: the system prompt
- `timeout_minutes`: run deadline (default 30)

### Fleet worker setup guide

The Trident fleet (Gordon/Kleiner/Eli) works on a GitHub Issues → PR → review → land pipeline. To run it on your own repo:

1. **Install prerequisites:** Claude Code CLI, `gh` CLI (authenticated), Python 3.12+, Node 20+
2. **Configure GitHub:** The gateway (`shared/github_gateway.py`) shells out to `gh`. Set `GITHUB_REPO` env var to your `owner/repo`.
3. **Define agent recipes** in the Agent Manager UI — set `watch_stage` to match each agent's role
4. **Customize the pipelines** (`agents/pipeline.py`, `agents/review_pipeline.py`, `agents/eli_pipeline.py`) — the prompt builders and workflow logic are Trident-specific. Replace with your own project's conventions.
5. **Arm the watch gate** in the Agent Manager UI — the scheduler starts polling for work
6. **Monitor** via the Agent Manager's run feed, terminal view, and HITL notification pills

Key design patterns to carry forward:
- **Worktree isolation:** Every agent run gets its own `git worktree` (see `agents/worktree.py`)
- **Pre-push guard:** A git hook blocks agent pushes to main + checks for built artifacts
- **Token guardrail:** Runs that exceed 300k tokens are halted with a needs-human report
- **Provider toggle:** Switch between Claude and DeepSeek system-wide via the Agent Manager

## Directory map

```
server/          FastAPI app + routes (terminal, agents, WebSocket)
daemons/         Standalone daemons (PTY manager, agent manager) + TCP clients
agents/          Agent logic — stores, scheduler, pipelines, guards, feed parser
shared/          Cross-cutting: secrets (keyring), GitHub gateway, loop gate
web/             React + Vite frontend
scripts/         Launcher scripts (PowerShell, batch)
```

## Shell & platform

- **Windows:** The PTY system uses Windows ConPTY (`pyte` for VT parsing). PowerShell is the default shell for terminal sessions.
- **macOS/Linux:** The terminal PTY system is Windows-only. The agent manager and API work cross-platform, but terminal sessions won't function without ConPTY. PRs welcome for `ptyprocess`/`pexpect` support.

## Configuration

- Agent manager port: `58998` (TCP, 127.0.0.1)
- PTY manager port: `58999` (TCP, 127.0.0.1)
- Dashboard HTTP: `8080`
- All ports configurable in `server/settings.py`

Agent data is stored as JSON files:
- `agents/` — agent definitions
- `agent_runs/` — run records
- `watch_gate.json` — master arm switch
- `agents/provider.json` — Claude/DeepSeek toggle

## Updating the frontend

```bash
cd web
npm run build     # outputs to web/dist/
```

The server serves `web/dist/` as static files. No deploy step — just rebuild and refresh.
