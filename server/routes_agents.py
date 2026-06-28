"""Agent CRUD routes for the Claude Code Dashboard (issue #27, parent #24).

Thin /api/agents router over the agent-manager's store-backed CRUD RPC. Every
handler delegates to agent_client.manager (the async TCP client); this
module owns only HTTP shape + status codes. Persistence and validation live in
the agent-manager / trident_agent_store, so the recipes survive a dashboard
restart and never sit in dashboard process memory.

Mirrors routes_terminal.py: an APIRouter with a prefix, handlers that await the
module-level client singleton, and HTTPException for the error shape.

  agents_router — /api/agents/*
"""

from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from daemons import agent_client
from agents import feed
from agents import provider


# ── Router ────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/agents", tags=["agents"])

# The operator's workspace root (repo root) — the SAME dir the live human Claude
# tabs run in, and the cwd a land/review/fixer run records. Passed to the feed
# assembler as the shared-cwd isolation guard so a transcript-less run there can
# never fall back onto the operator's own open session. Mirrors the BOT_DIR idiom
# in the sibling route modules (routes_terminal/routes_static/...).
BOT_DIR: Path = Path(__file__).resolve().parent.parent


# ── transcript base (injectable) ───────────────────────────────────────────────
# The feed route reads a run's Claude Code transcript directly off local disk
# (NOT through the agent-manager RPC — that is intended; transcripts are local
# files). The base dir is ~/.claude/projects in production; tests point it at a
# tmp fixture dir via set_transcript_base() so they never touch the real home.

_transcript_base: Path = feed.default_transcript_base()


def set_transcript_base(base) -> None:
    """Override the ~/.claude/projects base used by the feed route (tests)."""
    global _transcript_base
    _transcript_base = Path(base)


# ═══════════════════════════════════════════════════════════════════════════════
# Agent definition CRUD  (/api/agents*)
#
# Definitions persist as one gitignored JSON file per agent in the agent-manager
# (trident_agent_store). The manager validates on upsert and rejects bad
# definitions with a reason, which we surface as a 400. Editing here round-trips
# to disk: create/update → file written, read → file read back, delete → file
# removed.
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("")
async def api_agents_list():
    """All agent definitions."""
    return {"agents": await agent_client.manager.list_agents()}


# ═══════════════════════════════════════════════════════════════════════════════
# Run lifecycle  (/api/agents/runs*, /api/agents/{id}/run)  — issue #30
#
# A Run is one execution of an agent, hosted as a tagged pty-manager session and
# walked through queued → running → succeeded/failed/cancelled. Records persist
# in the gitignored run store (trident_agent_run_store, agent_runs/), so a
# completed run survives a dashboard restart and reads back here.
#
# NOTE on ordering: these /runs* routes are declared BEFORE the catch-all
# /{agent_id} GET so "runs" is never captured as an agent id.
# ═══════════════════════════════════════════════════════════════════════════════


@router.get("/runs")
async def api_agents_runs_list():
    """All run records (newest first)."""
    return {"runs": await agent_client.manager.list_runs()}


@router.get("/runs/{run_id}")
async def api_agents_run_get(run_id: str):
    """One run record — its final status + outcome once complete. 404 if missing."""
    run = await agent_client.manager.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@router.get("/runs/{run_id}/feed")
async def api_agents_run_feed(run_id: str):
    """Structured activity feed for a run.

    Loads the run record and assembles the feed via feed. Two feed
    sources, picked per run by the assembler: a Claude-session run (Gordon /
    Kleiner) resolves its own transcript JSONL on local disk (ordered prompt /
    text / tool_use / tool_result events + a token/cost summary); a transcript-less
    land run (Eli — pure git/npm/pytest) renders its structured stage log
    (merge → bundle → test → push) as ``stage`` events instead. ``BOT_DIR`` is
    passed as the shared-cwd isolation guard so a run in the operator's workspace
    with no pinned transcript id never falls back onto the live human session's
    transcript. Works live (polled, tolerant of mid-write truncation) and
    finished; a just-launched run with nothing yet returns an empty-but-valid feed.

    404 only if the run record itself does not exist.
    """
    run = await agent_client.manager.get_run(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    feed_result = feed.assemble_feed_for_run(
        run, _transcript_base, shared_cwd=BOT_DIR,
    )
    return {
        "run_id": run_id,
        "transcript": feed_result["transcript"],
        "events": feed_result["events"],
        "usage": feed_result["usage"],
    }


@router.post("/runs/{run_id}/cancel")
async def api_agents_run_cancel(run_id: str):
    """Cancel a running run: kill its session and mark it cancelled.

    404 if the run does not exist. 503 if the agent-manager is unreachable.
    """
    try:
        return await agent_client.manager.cancel_run(run_id)
    except ValueError:
        raise HTTPException(404, "run not found")
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/runs/{run_id}/ack")
async def api_agents_run_ack(run_id: str):
    """Acknowledge a halted run so it drops out of the red problems pill.
    Idempotent. Returns ``{acked: <int>}``. 503 if the agent-manager is
    unreachable.
    """
    try:
        return await agent_client.manager.ack_problem(run_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/{agent_id}/run")
async def api_agents_run_now(agent_id: str):
    """Launch an agent now. Returns the new Run record (status queued/running,
    or failed on launch).

    404 if the agent does not exist. 409 if the agent is DISABLED (OFF) — a hard
    stop (#40), so the manual override is refused too. 503 if the agent-manager
    is unreachable.
    """
    try:
        return await agent_client.manager.run_now(agent_id)
    except agent_client.AgentDisabledError as e:
        # Catch the disabled subclass BEFORE the generic ValueError below.
        raise HTTPException(409, str(e))
    except ValueError:
        raise HTTPException(404, "agent not found")
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/{agent_id}/enabled")
async def api_agents_set_enabled(agent_id: str, payload: dict = Body(...)):
    """Toggle an agent's ``enabled`` flag (issue #39).

    A cheap one-tap pause/resume that persists the flag without a full
    PUT-upsert round-trip. Body: ``{"enabled": <bool>}``. Returns the updated
    agent definition. 400 on a non-boolean body, 404 if the agent does not
    exist, 503 if the agent-manager is unreachable (a downed manager must not
    masquerade as 'agent not found' — same contract as the run_now route).
    """
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    try:
        agent = await agent_client.manager.set_enabled(agent_id, enabled)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


# ── master watch-gate (the ARM toggle) ─────────────────────────────────────────
# Registered BEFORE the /{agent_id} catch-all GET below, so "watch-gate" is never
# mistaken for an agent id. This is the master arm switch for the autonomous
# issue→PR loop — distinct from the per-agent enabled flag above.

@router.get("/watch-gate")
async def api_agents_get_watch_gate():
    """The master watch-gate arm state: ``{enabled, env_override, effective}``.
    503 if the agent-manager is unreachable (never report a misleading 'disarmed').
    """
    try:
        return await agent_client.manager.get_watch_gate()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/watch-gate")
async def api_agents_set_watch_gate(payload: dict = Body(...)):
    """Arm/disarm the autonomous watch loop. Body: ``{"enabled": <bool>}``.
    Returns the new arm status. 400 on a non-boolean body, 503 if the manager is
    unreachable. Takes effect on the next scheduler tick — no restart.
    """
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    try:
        return await agent_client.manager.set_watch_gate(enabled)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── per-project supervised-mode toggle (the PM-gate switch — #66) ────────────
# Registered BEFORE the /{agent_id} catch-all GET, so "supervised" is never
# mistaken for an agent id. ON ⇒ a Kleiner-clean review parks at needs-signoff
# (the PM gate) before Eli lands; OFF ⇒ full-auto. The live effect lands once
# Kleiner/ReviewPipeline is manager-wired (a later slice); this persists + surfaces
# the value and feeds the ReviewPipeline `supervised` seam.

@router.get("/supervised")
async def api_agents_get_supervised():
    """The per-project supervised-mode state: ``{enabled, effective}``.
    503 if the agent-manager is unreachable (never report a misleading 'off')."""
    try:
        return await agent_client.manager.get_supervised()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/supervised")
async def api_agents_set_supervised(payload: dict = Body(...)):
    """Turn supervised mode ON/OFF. Body: ``{"enabled": <bool>}``. Returns the new
    status. 400 on a non-boolean body, 503 if the manager is unreachable. Takes
    effect on the next review cycle once Kleiner is manager-wired — no restart.
    """
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be a boolean")
    try:
        return await agent_client.manager.set_supervised(enabled)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── agent provider toggle (system-wide: all agents use Claude OR DeepSeek) ─────
# Registered BEFORE the /{agent_id} catch-all, so "provider" is never mistaken
# for an agent id. A single toggle switches Gordon, Kleiner, and Eli at once.
# The provider file is written AND env vars are applied to this process so the
# agent-manager subprocess inherits them on its next lazy respawn
# (agent_client._spawn_manager does env=inherit).
#
# No restart is strictly necessary — the next agent run picks up the new env.
# But the UI offers a one-click manager restart so the operator can be sure.


@router.get("/provider")
async def api_agents_get_provider():
    """The system-wide agent provider: ``{provider, deepseek_key_configured}``.

    ``provider`` is ``"claude"`` (Anthropic) or ``"deepseek"``. All three agents
    (Gordon / Kleiner / Eli) share it — flipping the toggle switches the whole fleet.
    ``deepseek_key_configured`` is ``true`` when the OS keyring holds a DeepSeek key.
    """
    return provider.provider_status()


@router.post("/provider")
async def api_agents_set_provider(payload: dict = Body(...)):
    """Set the system-wide agent provider. Body: ``{"provider": "claude"|"deepseek"}``.

    Persists to ``agents/provider.json``, then applies ``ANTHROPIC_*`` env vars to
    the current dashboard process so the agent-manager subprocess inherits them on
    its next lazy respawn. Returns the new provider status.

    400 if ``provider`` is not ``"claude"`` or ``"deepseek"``.
    """
    provider_name = payload.get("provider") if isinstance(payload, dict) else None
    if not isinstance(provider_name, str):
        raise HTTPException(400, "provider must be a string")
    try:
        provider.set_provider(provider_name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Apply env vars to THIS process now. The agent-manager subprocess inherits
    # the dashboard's env on next spawn (subprocess.Popen with no env= override
    # inherits by default). No restart required for the change to take effect.
    provider.apply_provider_env()
    return provider.provider_status()


# ── dashboard pill counts (the four-pill HITL badge — #65) ──────────────────────
# Registered BEFORE the /{agent_id} catch-all GET below, so "pill-counts" is never
# mistaken for an agent id.

@router.get("/pill-counts")
async def api_agents_pill_counts():
    """The four notification-pill counts for the nav badge (#65):
    ``{green, yellow, purple, red}`` — green = unacknowledged completed work,
    yellow = needs-signoff (supervised), purple = needs-trade-approval (#63 money
    wall), red = needs-human + halted runs + architecture freeze. 503 if the
    agent-manager is unreachable (never report misleading zeros for a downed
    manager)."""
    try:
        return await agent_client.manager.get_pill_counts()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── loop-health snapshot + mechanical unblocks ──────────────────────────────────
# Registered BEFORE the /{agent_id} catch-all GET below, so "loop-status" is never
# mistaken for an agent id.

@router.get("/loop-status")
async def api_agents_loop_status():
    """The loop-health snapshot for the dashboard: arm/freeze state, in-flight /
    PR-cap, per-stage running flags, withheld claims, and concrete blockers.
    503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.loop_status()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/pulse")
async def api_agents_pulse():
    """Cheap glow/pulse signal for the Claude-tab AGENTS button: ``{active,
    tick_seq}``. ``active`` = any agent run is in flight (running/queued);
    ``tick_seq`` = the monotonic scheduler-check counter (the dashboard flashes
    the tab each time it advances). No gh — safe to poll every few seconds.
    Registered before the ``/{agent_id}`` catch-all so "pulse" isn't read as an
    agent id. 503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.pulse_status()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/snapshot")
async def api_agents_snapshot():
    """The consolidated agent-panel view-model (#149): runs (log-stripped),
    pill_counts, pulse, loop_health, watch_gate, and supervised — in one object.
    Replaces six individual GETs. 503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.get_snapshot()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/loop-status/resolve")
async def api_agents_resolve_blocker(payload: dict = Body(...)):
    """Perform one mechanical unblock. Body: ``{"action": {...}}`` where action is
    ``{"type": "clear_freeze"}`` or ``{"type": "close_pr", "pr": <int>}``. 400 on a
    bad action, 503 if the agent-manager is unreachable."""
    action = payload.get("action")
    try:
        return await agent_client.manager.resolve_blocker(action)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── staleness (loaded SHA vs repo HEAD — issue #182) ──────────────────────────
# The agent-manager is a detached daemon that survives the dashboard restart, so
# a code fix is invisible until the daemon is recycled. This tells the dashboard
# whether the running code matches the current checkout.

@router.get("/staleness")
async def api_agents_staleness():
    """The manager staleness snapshot: ``{loaded_sha, repo_head, stale}``.
    ``stale`` True means the running daemon was loaded from a different commit
    than the current checkout — a recycle is needed for the fix to take effect.
    503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.get_staleness()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── completed-work ack (clears the green pill on viewing — #65) ───────────────

@router.post("/completed/ack")
async def api_agents_ack_completed():
    """Mark all currently-succeeded runs SEEN — the green pill's "clears on viewing".
    Returns ``{acked: <int>}``. 503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.ack_completed()
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ── PM approve / reject (Slice C) ───────────────────────────────────────────────
# Registered BEFORE the /{agent_id} catch-all GET below. The "/issues/..." prefix
# is distinct from a bare agent id, but these explicit POST routes live here with
# the other named routes to be unambiguously safe from catch-all shadowing.

@router.post("/issues/{number}/approve")
async def api_agents_approve_issue(number: int):
    """PM sign-off APPROVE: flip issue #number needs-signoff -> approved and log
    the PM decision. 503 if the agent-manager is unreachable."""
    try:
        await agent_client.manager.approve_issue(number)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@router.post("/issues/{number}/reject")
async def api_agents_reject_issue(number: int):
    """PM sign-off REJECT: flip issue #number needs-signoff -> rejected and log
    the PM decision. 503 if the agent-manager is unreachable."""
    try:
        await agent_client.manager.reject_issue(number)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


# ── trading-surface approve (the #63 money wall — single action, no reject) ───
# A DIFFERENT clearance from the Slice-C sign-off approve above: this applies the
# trade-approved label (the #63 two-pass handshake) so Eli re-polls and proceeds.
# There is deliberately NO reject route — approving is the only way to clear the
# money wall.

@router.get("/trade-approvals")
async def api_agents_list_trade_approvals():
    """The purple inbox: ``{issues: [{number, title}]}`` — open issues parked at the
    #63 money wall. 503 if the agent-manager is unreachable."""
    try:
        return {"issues": await agent_client.manager.list_trade_approval_issues()}
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.get("/issues/{number}/trade-approval")
async def api_agents_trade_approval_brief(number: int):
    """The purple view payload for issue #number: ``{number, title, brief}`` — the
    plain-English #63 money-wall brief. 503 if the agent-manager is unreachable."""
    try:
        return await agent_client.manager.trade_approval_brief(number)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/issues/{number}/trade-approve")
async def api_agents_trade_approve_issue(number: int):
    """TRADING-SURFACE APPROVE: clear the #63 money wall on issue #number by
    applying the trade-approved (+ approved) labels so Eli re-polls and proceeds.
    503 if the agent-manager is unreachable."""
    try:
        await agent_client.manager.trade_approve_issue(number)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@router.get("/{agent_id}")
async def api_agents_get(agent_id: str):
    """One agent definition. 404 if it does not exist."""
    agent = await agent_client.manager.get_agent(agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


@router.put("")
async def api_agents_upsert(payload: dict = Body(...)):
    """Create or update an agent definition (upsert).

    Body: the full agent definition (id, name, prompt, model, trigger, options).
    Invalid definitions are rejected 400 with the manager's reason. 503 if the
    agent-manager is unreachable.
    """
    if not isinstance(payload, dict):
        raise HTTPException(400, "agent definition must be a JSON object")
    try:
        agent = await agent_client.manager.upsert_agent(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return agent


@router.delete("/{agent_id}")
async def api_agents_delete(agent_id: str):
    """Delete an agent definition. 404 if it did not exist."""
    deleted = await agent_client.manager.delete_agent(agent_id)
    if not deleted:
        raise HTTPException(404, "agent not found")
    return {"deleted": agent_id}


# ═══════════════════════════════════════════════════════════════════════════════
# Agent-manager lifecycle  (/api/agents/manager/restart)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/manager/restart")
async def api_agents_manager_restart():
    """Stop the standalone agent-manager process (trident_agent_manager.py).

    Halts run scheduling and in-flight run tracking; the manager respawns lazily
    on the next agent API access via agent_client._ensure_manager,
    inheriting the dashboard's CURRENT environment. That inheritance is the point
    of this control: it's how a freshly-set TRIDENT_AGENT_WATCH_ENABLED gate (or
    any other env change) actually reaches the manager without restarting the
    whole dashboard. If the manager isn't running this is a success no-op
    (method "not-running").

    Returns {ok, method, detail} where method is "rpc" (graceful shutdown RPC),
    "pid-kill" (verified force-kill fallback), or "not-running".
    """
    result = await agent_client.manager.restart_manager()
    if not result.get("ok"):
        raise HTTPException(500, result.get("detail") or "agent manager restart failed")
    return result
