# Claude Dashboard

A web-based operations dashboard for [Claude Code](https://claude.ai/claude-code) — persistent terminal sessions, autonomous agent orchestration, and real-time run monitoring.

![Claude Dashboard](screenshots/terminal.png)

## What it does

- **Web terminal** — Full xterm.js terminal in the browser, connected to a persistent PowerShell PTY on the host. Multiple tabs, split-view layout, mobile support, file upload. Sessions survive browser disconnects.
- **Agent Manager** — Define, schedule, and monitor autonomous Claude Code agents. Each agent gets a recipe (model, prompt, trigger), and runs are tracked with structured transcript feeds, embedded terminals, token accounting, and a HITL (human-in-the-loop) notification system.
- **Reference fleet** — Includes three example agent pipelines (Gordon/Kleiner/Eli) extracted from a production trading-bot project. They implement a GitHub Issues → PR → review → land workflow with git worktree isolation, pre-push guards, and token guardrails.

## Screenshots

<img width="1790" height="1038" alt="image" src="https://github.com/user-attachments/assets/2f3e9f2b-9f15-42b6-9404-fc664c7ca78a" />
<img width="422" height="707" alt="image" src="https://github.com/user-attachments/assets/fd3403bc-2a31-4ae6-8446-6ce3bab9b855" />
<img width="313" height="483" alt="image" src="https://github.com/user-attachments/assets/fa32b757-910b-4f5d-b1fd-3ff9aaa360f5" />



## Quick start

### Prerequisites

- **Python 3.12+** 
- **Node.js 20+**
- **Claude Code** CLI installed and in PATH
- **Windows** (terminal PTY uses ConPTY; agent manager works cross-platform)
- **`gh` CLI** (for agent GitHub features)

### Setup

```bash
# Clone
git clone https://github.com/settingzbot/claude-dashboard.git
cd claude-dashboard

# Backend
pip install -r requirements.txt
python -m uvicorn server.app:create_app --factory --host 127.0.0.1 --port 8080

# Frontend (separate terminal)
cd web
npm install
npm run dev
```

Open **http://localhost:5173** — the Vite dev server proxies API calls to the backend on port 8080.

### Production

```bash
cd web && npm run build    # builds to web/dist/
python -m uvicorn server.app:create_app --factory --host 0.0.0.0 --port 8080
```

The server serves the built frontend from `web/dist/`. No separate static file server needed.

## Architecture

```
Browser ── HTTP/WS ──► FastAPI (:8080)
                          ├── TCP :58999 ──► PTY Manager (ConPTY sessions)
                          └── TCP :58998 ──► Agent Manager (scheduler, runs)
```

Both daemons are lazy-spawned on first request and survive server restarts. They communicate with the FastAPI server over local TCP (JSON-lines protocol).

## Agent system

The agent system lets you define **agent recipes** (model, system prompt, trigger) and the scheduler runs them automatically.

### Built-in reference pipelines

Three agents extracted from a production trading-bot project demonstrate the full pattern:

| Agent | Stage | What it does |
|-------|-------|-------------|
| Gordon | `claim` | Claims GitHub issues labeled `ready-for-agent`, implements them in a git worktree, opens a PR |
| Kleiner | `review` | Reviews open PRs, files structured fix requests, runs a fixer agent on demand |
| Eli | `land` | Merges reviewed PRs, runs the test suite, pushes to main, closes the issue |

These are **reference implementations** — they're configured for a specific repo and workflow. See [CLAUDE.md](CLAUDE.md) for the full fleet worker setup guide.

### Creating your own agent

1. Open the Agent Manager (AGENTS button in the terminal toolbar)
2. Click **+ New Agent**
3. Fill in: name, model (`claude` / `claude-ds` / `claude-go` / `claude-ds-go`), trigger type, and prompt
4. Enable the agent
5. Arm the **watch gate** to let the scheduler launch runs

Agent definitions are stored as JSON files in `agents/`. Run records in `agent_runs/`.

### Provider switching

Toggle between Claude and DeepSeek system-wide from the Agent Manager. Requires a DeepSeek API key (stored in the OS keyring via `shared/secrets.py`).

## Configuration

| Setting | Default | Where |
|---------|---------|-------|
| Server port | 8080 | `server/settings.py` |
| Agent manager port | 58998 | `server/settings.py` |
| PTY manager port | 58999 | `server/settings.py` |
| Agent timeout | 30 min | Per-agent in Agent Manager |
| Token ceiling | 300k | `agents/tokens.py` |

## Platform notes

- **Terminal sessions are Windows-only** (ConPTY via `pyte`). The agent manager and API run on any platform.
- **DeepSeek support** requires a DeepSeek API key. The PowerShell launchers (`scripts/claude-ds.ps1`) inject it as `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`.
- **GitHub integration** shells out to the `gh` CLI. Make sure `gh auth status` passes.

## License

MIT — see [LICENSE](LICENSE).

## Credits

Extracted from [settingzbot/Trident](https://github.com/settingzbot/Trident), a production algorithmic trading bot. The Claude tab was its primary operations surface — this is that surface, packaged standalone.
