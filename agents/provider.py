"""System-wide agent provider toggle — Claude (Anthropic) vs DeepSeek.

One file (``agents/provider.json``) switches all three agents at once: Gordon,
Kleiner, and Eli. When provider=deepseek, :func:`get_provider_env_prefix` returns a PowerShell
command prefix that sets ``ANTHROPIC_BASE_URL`` + ``ANTHROPIC_AUTH_TOKEN`` (from
the OS keyring) + the model-mapping vars inline in the command string. This
prefix is injected at agent-launch time by both ``build_claude_command`` and
``build_run_command`` — the provider is read fresh from disk on every run, so
a toggle takes effect on the **next agent run** with no process restart needed.

When provider=claude, the same function CLEARS those env vars so ``claude`` hits
api.anthropic.com by default.

Mirrors the env-var set from ``scripts/claude-ds.ps1`` exactly, so agent runs
behave identically to a human-launched DeepSeek Claude Code session.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

PROVIDER_FILENAME = "provider.json"
VALID_PROVIDERS = ("claude", "deepseek")
DEFAULT_PROVIDER = "claude"

DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"

# Env vars that route Claude Code through DeepSeek. These are set as a block
# when provider=deepseek and cleared as a block when provider=claude. The
# ANTHROPIC_AUTH_TOKEN key is handled separately (it comes from the keyring, not
# a constant), but it's cleared alongside these on switch-back.
_DEEPSEEK_ENV_VARS = {
    "ANTHROPIC_BASE_URL": DEEPSEEK_BASE_URL,
    "ANTHROPIC_MODEL": DEEPSEEK_MODEL,
    "ANTHROPIC_DEFAULT_OPUS_MODEL": DEEPSEEK_MODEL,
    "ANTHROPIC_DEFAULT_SONNET_MODEL": DEEPSEEK_MODEL,
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": DEEPSEEK_FLASH_MODEL,
    "CLAUDE_CODE_SUBAGENT_MODEL": DEEPSEEK_FLASH_MODEL,
    "CLAUDE_CODE_EFFORT_LEVEL": "max",
}

# All env vars that get cleared on switch to claude, including the auth token.
_ALL_PROVIDER_ENV_VARS = list(_DEEPSEEK_ENV_VARS) + ["ANTHROPIC_AUTH_TOKEN"]


# ── file path ─────────────────────────────────────────────────────────────────

def _resolve_workspace() -> str:
    """Repo root — this file lives at the root, so its own dir is the workspace."""
    return os.path.dirname(os.path.abspath(__file__))


def _provider_path(workspace: str | None = None) -> str:
    ws = workspace or _resolve_workspace()
    return os.path.join(ws, "agents", PROVIDER_FILENAME)


# ── public API ────────────────────────────────────────────────────────────────

def get_provider(workspace: str | None = None) -> str:
    """Read the current provider from ``agents/provider.json``.

    Returns ``"claude"`` if the file is missing, unreadable, or contains an
    unrecognised value — the safe default.
    """
    path = _provider_path(workspace)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        provider = data.get("provider", DEFAULT_PROVIDER)
        if provider in VALID_PROVIDERS:
            return provider
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_PROVIDER


def set_provider(provider: str, workspace: str | None = None) -> None:
    """Persist the provider to ``agents/provider.json``.

    Raises :class:`ValueError` if *provider* is not ``"claude"`` or ``"deepseek"``.
    Creates the ``agents/`` directory if it does not exist.
    """
    provider = provider.strip().lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError(
            f"provider must be one of {VALID_PROVIDERS}, got {provider!r}"
        )
    path = _provider_path(workspace)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"provider": provider}, f, indent=2)
        f.write("\n")


def apply_provider_env(workspace: str | None = None) -> None:
    """Apply the current provider to :obj:`os.environ`.

    Call this in the dashboard process BEFORE the agent-manager spawns, and
    again in the agent-manager's own ``main()``, so every child process (PTY
    sessions → ``claude``) inherits the right routing.

    **deepseek**: reads the API key from the OS keyring via
    ``trident_secrets.get_deepseek_key()``, sets ``ANTHROPIC_AUTH_TOKEN``, then
    sets the full block of ``ANTHROPIC_*`` + ``CLAUDE_CODE_*`` routing vars.
    If the keyring is empty, logs a warning and returns without setting anything
    (agent runs will fail with auth errors rather than silently hitting
    api.anthropic.com with a DeepSeek key).

    **claude**: clears all provider-set env vars so ``claude`` uses its default
    Anthropic endpoint.
    """
    provider = get_provider(workspace)
    if provider == "deepseek":
        # Import locally — trident_secrets touches keyring at import time, and
        # this module may be imported before the keyring backend is ready (e.g.
        # during test collection on a headless CI runner).
        from shared.secrets import get_deepseek_key
        key = get_deepseek_key()
        if not key:
            _LOG.warning(
                "agent provider is deepseek but no DeepSeek key in keyring — "
                "agent runs will fail with auth errors"
            )
            return
        os.environ["ANTHROPIC_AUTH_TOKEN"] = key
        for var, value in _DEEPSEEK_ENV_VARS.items():
            os.environ[var] = value
        _LOG.info("agent provider: deepseek (base_url=%s)", DEEPSEEK_BASE_URL)
    else:
        for var in _ALL_PROVIDER_ENV_VARS:
            os.environ.pop(var, None)
        _LOG.info("agent provider: claude (default Anthropic)")


def get_provider_env_prefix(workspace: str | None = None) -> str:
    """Return a PowerShell command prefix that sets provider env vars, or ``""``.

    Reads the current provider from ``agents/provider.json`` on every call — so
    a toggle takes effect on the **next agent run**, no process restart needed.
    The caller prepends this to their ``claude`` invocation inside a PowerShell
    command string.

    **deepseek**: returns a semicolon-separated block of ``$env:VAR="value"``
    assignments that route ``claude`` through DeepSeek's Anthropic-compatible
    endpoint. The API key is read from the OS keyring. If the keyring is empty,
    logs a warning and returns ``""`` (the run will fail with an auth error
    rather than silently hitting the wrong endpoint).

    **claude**: returns ``""`` — ``claude`` hits api.anthropic.com by default.
    """
    provider = get_provider(workspace)
    if provider != "deepseek":
        return ""
    from shared.secrets import get_deepseek_key
    key = get_deepseek_key()
    if not key:
        _LOG.warning(
            "agent provider is deepseek but no DeepSeek key in keyring — "
            "agent run will fail with auth errors"
        )
        return ""
    # Escape for a PowerShell double-quoted string: ` → ``, $ → `$, " → `"
    safe_key = key.replace("`", "``").replace("$", "`$").replace('"', '`"')
    parts = [f'$env:ANTHROPIC_AUTH_TOKEN="{safe_key}"']
    for var, value in _DEEPSEEK_ENV_VARS.items():
        safe_value = value.replace("`", "``").replace("$", "`$").replace('"', '`"')
        parts.append(f'$env:{var}="{safe_value}"')
    prefix = "; ".join(parts) + "; "
    _LOG.debug("injected DeepSeek env prefix (%d vars)", len(parts))
    return prefix


def provider_status(workspace: str | None = None) -> dict:
    """Dashboard-facing status: ``{provider, deepseek_key_configured}``.

    ``deepseek_key_configured`` is ``True`` when the OS keyring holds a DeepSeek
    API key — the dashboard can use this to show a warning when the provider is
    ``deepseek`` but no key is set.
    """
    from shared.secrets import deepseek_key_status
    return {
        "provider": get_provider(workspace),
        "deepseek_key_configured": deepseek_key_status().get("configured", False),
    }
