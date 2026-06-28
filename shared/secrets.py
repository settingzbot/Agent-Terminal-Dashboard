"""
trident_secrets.py — secure storage for API keys and credentials via the OS keyring.

Resolution order for Lighter key (highest priority first):
    1. OS keyring        (Windows Credential Manager on Win; cryptfile on Linux)
    2. LIGHTER_PRIVATE_KEY env var
    3. trident_config.json  bot.api_private_key    (legacy, discouraged)

Backend selection:
    * Windows / macOS: `keyring` auto-picks Credential Manager / Keychain.
    * Linux server:    set env var
          PYTHON_KEYRING_BACKEND=keyrings.cryptfile.cryptfile.CryptFileKeyring
      and supply
          KEYRING_CRYPTFILE_PASSWORD=<master-passphrase>
      at dashboard / bot startup. The encrypted file lives at
          ~/.local/share/python_keyring/cryptfile_pass.cfg

The rest of the codebase should ONLY call the public functions below. Never read
credentials from cfg or env directly.  Phase 5 collapsed 20 near-identical CRUD
functions into one internal ``_CredentialStore`` class; the public API is unchanged.
"""

from __future__ import annotations
import json
import os
import logging
from typing import Callable

SERVICE = "trident"
USERNAME = "lighter_api_private_key"           # kept for test backward compat
USERNAME_X = "x_api_credentials"
_X_KEYS = ("api_key", "api_secret", "access_token", "access_token_secret")
USERNAME_BSKY = "bluesky_credentials"
_BSKY_KEYS = ("handle", "app_password")
USERNAME_4H = "lighter_api_private_key_4h"
USERNAME_DEEPSEEK = "deepseek_api_key"
USERNAME_WATCHDOG_GMAIL = "watchdog_gmail"
_WATCHDOG_GMAIL_KEYS = ("email", "app_password")
USERNAME_WAITLIST_STATS = "waitlist_stats_key"
USERNAME_EMAIL = "trident_email_app_password"

log = logging.getLogger(__name__)


# ── Keyring helper ──────────────────────────────────────────────────────────

def _keyring():
    """Import keyring lazily so the module is importable even if the package
    is missing in some dev environments."""
    try:
        import keyring
        # On Linux with a cryptfile backend, the password must be pre-supplied.
        pw = os.environ.get("KEYRING_CRYPTFILE_PASSWORD")
        if pw:
            try:
                backend = keyring.get_keyring()
                if hasattr(backend, "keyring_key"):
                    backend.keyring_key = pw
            except Exception:
                pass
        return keyring
    except Exception as e:
        log.warning("keyring unavailable: %s", e)
        return None


def _keyring_backend_name() -> str:
    """Return the keyring backend class name, or '' if unavailable."""
    kr = _keyring()
    if kr is None:
        return ""
    try:
        return type(kr.get_keyring()).__name__
    except Exception:
        return "unknown"


# ── Internal credential store ───────────────────────────────────────────────

class _CredentialStore:
    """One keyring-backed credential (single string or JSON bundle).

    *keys* is None for a single-string secret; pass a tuple of field names for a
    JSON bundle.  *scrub* is an optional per-field callback called during
    ``set()`` — e.g. to strip a leading ``@`` from a Bluesky handle.
    """

    def __init__(
        self,
        username: str,
        *,
        keys: tuple[str, ...] | None = None,
        scrub: Callable[[str, str], str] | None = None,
        log_label: str = "",
    ):
        self.username = username
        self.keys = keys          # None → single string; tuple → JSON bundle
        self.scrub = scrub
        self.log_label = log_label or username

    # -- get ------------------------------------------------------------------

    def get(self) -> str | dict | None:
        """Read from the keyring.  Returns a string (single), a dict (bundle),
        or None if not set / incomplete / keyring unavailable."""
        kr = _keyring()
        if kr is None:
            return None
        try:
            raw = kr.get_password(SERVICE, self.username)
        except Exception as e:
            log.warning("keyring read for %s failed: %s", self.log_label, e)
            return None
        if not raw:
            return None
        if self.keys is None:
            return raw                     # type: ignore[no-any-return]  # single string
        # JSON bundle
        try:
            creds = json.loads(raw)
        except Exception as e:
            log.warning("%s keyring entry is not valid JSON: %s", self.log_label, e)
            return None
        if not all(k in creds and creds[k] for k in self.keys):
            return None
        return creds  # type: ignore[no-any-return]

    # -- set ------------------------------------------------------------------

    def set(self, value: str | dict) -> None:
        """Store in the OS keyring.
        Raises ValueError on missing/empty input.
        Raises RuntimeError if no keyring backend is available."""
        kr = _keyring()
        if kr is None:
            raise RuntimeError(
                "keyring package is not installed. "
                "pip install keyring (and keyrings.cryptfile on headless Linux)."
            )

        if self.keys is None:
            # Single string
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Empty key")
            kr.set_password(SERVICE, self.username, value.strip())
        else:
            # JSON bundle
            if not isinstance(value, dict):
                raise ValueError("creds must be a dict")
            cleaned: dict[str, str] = {}
            for k in self.keys:
                v = value.get(k, "")
                if not isinstance(v, str) or not v.strip():
                    raise ValueError(f"Missing or empty: {k}")
                v = v.strip()
                if self.scrub is not None:
                    v = self.scrub(k, v)
                cleaned[k] = v
            kr.set_password(SERVICE, self.username, json.dumps(cleaned))

    # -- clear ----------------------------------------------------------------

    def clear(self) -> None:
        """Remove from the OS keyring (no-op if not present or unavailable)."""
        kr = _keyring()
        if kr is None:
            return
        try:
            kr.delete_password(SERVICE, self.username)
        except Exception:
            pass

    # -- status ---------------------------------------------------------------

    def status(self) -> dict:
        """Non-sensitive metadata.  Never returns credential values — only
        last-4 tails and a configured flag."""
        backend = _keyring_backend_name()
        value = self.get()
        _PUBLIC_FIELDS = frozenset({"handle", "email"})

        if not value:
            base: dict = {"configured": False, "backend": backend}
            if self.keys is None:
                base["last4"] = None
            else:
                for k in self.keys:
                    base[f"{k}_last4"] = None
                    if k in _PUBLIC_FIELDS:
                        base[k] = None
            return base

        base = {"configured": True, "backend": backend}
        if self.keys is None:
            base["last4"] = value[-4:]
        else:
            for k in self.keys:
                base[f"{k}_last4"] = value[k][-4:]   # type: ignore[index]
                if k in _PUBLIC_FIELDS:
                    base[k] = value[k]                # type: ignore[index]
        return base


# ── Credential store instances ──────────────────────────────────────────────

def _strip_at_handle(_field: str, value: str) -> str:
    """Tolerate a leading @ on Bluesky handles — bsky.app shows them with one,
    the AT Protocol login expects it stripped."""
    return value[1:] if value.startswith("@") else value


_lighter = _CredentialStore(USERNAME, log_label="lighter key")
_lighter_4h = _CredentialStore(USERNAME_4H, log_label="lighter key 4H")
_x = _CredentialStore(USERNAME_X, keys=_X_KEYS, log_label="X creds")
_bluesky = _CredentialStore(
    USERNAME_BSKY, keys=_BSKY_KEYS, scrub=_strip_at_handle, log_label="Bluesky creds"
)
_deepseek = _CredentialStore(USERNAME_DEEPSEEK, log_label="DeepSeek key")
_watchdog = _CredentialStore(
    USERNAME_WATCHDOG_GMAIL, keys=_WATCHDOG_GMAIL_KEYS, log_label="watchdog Gmail creds"
)
_waitlist_stats = _CredentialStore(USERNAME_WAITLIST_STATS, log_label="waitlist stats key")
_email_pw = _CredentialStore(USERNAME_EMAIL, log_label="email app password")


# ── Public API — Lighter key (3-tier fallback: keyring → env → config) ──────

def get_lighter_key(cfg: dict | None = None) -> str:
    """Return the Lighter API private key, or '' if not set anywhere."""
    val = _lighter.get()
    if val:
        assert isinstance(val, str), "expected string credential"
        return val

    env_val = os.environ.get("LIGHTER_PRIVATE_KEY", "")
    if env_val:
        return env_val

    if cfg is not None:
        try:
            legacy = (cfg.get("bot", {}) or {}).get("api_private_key", "") or ""
            if legacy:
                log.warning(
                    "Lighter key loaded from trident_config.json (legacy path). "
                    "Migrate it into the OS keyring via the dashboard Settings tab."
                )
                return legacy
        except Exception:
            pass

    return ""


def set_lighter_key(value: str) -> None:
    """Store the key in the OS keyring."""
    _lighter.set(value)


def clear_lighter_key() -> None:
    """Remove the key from the OS keyring (no-op if not present)."""
    _lighter.clear()


def key_status(cfg: dict | None = None) -> dict:
    """Return non-sensitive metadata about the current key.
    NEVER returns the key itself."""
    backend = _keyring_backend_name()

    key = get_lighter_key(cfg)
    if not key:
        return {
            "configured": False,
            "source": None,
            "last4": None,
            "backend": backend,
        }

    # Determine source without re-exposing the key.
    source = "unknown"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(SERVICE, USERNAME):
                source = "keyring"
        except Exception:
            pass
    if source == "unknown" and os.environ.get("LIGHTER_PRIVATE_KEY"):
        source = "env"
    if source == "unknown" and cfg is not None:
        if (cfg.get("bot", {}) or {}).get("api_private_key"):
            source = "config_file_legacy"

    return {
        "configured": True,
        "source": source,
        "last4": key[-4:],
        "backend": backend,
    }


# ── Public API — 4H Lighter key (keyring-only, no env/config fallback) ──────

def get_lighter_key_4h() -> str:
    """Return the 4H Lighter API private key, or '' if not set.
    Keyring-only — no env-var or legacy-config fallback. The 4H bot
    trades its own sub-account with its own API key."""
    val = _lighter_4h.get()
    if val:
        assert isinstance(val, str), "expected string credential"
        return val
    return ""


def set_lighter_key_4h(value: str) -> None:
    """Store the 4H key in the OS keyring."""
    _lighter_4h.set(value)


def clear_lighter_key_4h() -> None:
    """Remove the 4H key from the OS keyring (no-op if not present)."""
    _lighter_4h.clear()


def lighter_key_4h_status() -> dict:
    """Return non-sensitive metadata about the 4H key.
    NEVER returns the key itself — only configured flag + last4 + backend."""
    return _lighter_4h.status()


# ── Public API — X (Twitter) credentials ────────────────────────────────────

def get_x_credentials() -> dict | None:
    return _x.get()  # type: ignore[return-value]

def set_x_credentials(creds: dict) -> None:
    _x.set(creds)

def clear_x_credentials() -> None:
    _x.clear()

def x_credentials_status() -> dict:
    return _x.status()


# ── Public API — Bluesky credentials ────────────────────────────────────────

def get_bluesky_credentials() -> dict | None:
    return _bluesky.get()  # type: ignore[return-value]

def set_bluesky_credentials(creds: dict) -> None:
    _bluesky.set(creds)

def clear_bluesky_credentials() -> None:
    _bluesky.clear()

def bluesky_credentials_status() -> dict:
    return _bluesky.status()


# ── Public API — DeepSeek API key ───────────────────────────────────────────

def get_deepseek_key() -> str | None:
    return _deepseek.get()  # type: ignore[return-value]

def set_deepseek_key(key: str) -> None:
    _deepseek.set(key)

def clear_deepseek_key() -> None:
    _deepseek.clear()

def deepseek_key_status() -> dict:
    return _deepseek.status()


# ── Public API — Watchdog Gmail credentials ─────────────────────────────────

def get_watchdog_gmail() -> dict | None:
    return _watchdog.get()  # type: ignore[return-value]

def set_watchdog_gmail(creds: dict) -> None:
    _watchdog.set(creds)

def clear_watchdog_gmail() -> None:
    _watchdog.clear()

def watchdog_gmail_status() -> dict:
    return _watchdog.status()


# ── Public API — Waitlist stats key ─────────────────────────────────────────
# Shared secret for GET tridentv4.nexus/api/waitlist (signup counts). The same
# value is stored as the WAITLIST_STATS_KEY Pages secret on the trident-landing
# Cloudflare project; dashboard/routes_waitlist.py sends it as a Bearer token.

def get_waitlist_stats_key() -> str | None:
    return _waitlist_stats.get()  # type: ignore[return-value]

def set_waitlist_stats_key(key: str) -> None:
    _waitlist_stats.set(key)

def clear_waitlist_stats_key() -> None:
    _waitlist_stats.clear()

def waitlist_stats_key_status() -> dict:
    return _waitlist_stats.status()


# ── Public API — Email app password ──────────────────────────────────────────
# Single-string secret stored under trident:trident_email_app_password.
# Consumed by trident_email.py for Gmail SMTP auth. The email address and
# SMTP server are not secrets — they stay in trident_config.json.


def get_email_app_password() -> str | None:
    """Return the email app password, or None if not set / unavailable."""
    return _email_pw.get()  # type: ignore[return-value]


def set_email_app_password(value: str) -> None:
    """Store the email app password in the OS keyring."""
    _email_pw.set(value)


def clear_email_app_password() -> None:
    """Remove the email app password from the OS keyring (no-op if not present)."""
    _email_pw.clear()


def email_app_password_status() -> dict:
    """Return non-sensitive metadata about the email app password.
    NEVER returns the password itself — only configured flag + last4 + backend."""
    return _email_pw.status()
