"""Compatibility helpers for profile-scoped Hermes gateway runtimes.

The Zulip plugin is also used outside Hermes, so imports of Hermes internals
remain lazy.  In a multiplexed gateway the active secret scope is
authoritative: a missing setting deliberately does not fall back to the
process environment, which may belong to a different profile.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional


SCOPED_SETTINGS_EXTRA_KEY = "_zulip_scoped_settings"
PROFILE_DATA_DIR_EXTRA_KEY = "_zulip_profile_data_dir"


def _secret_scope_module():
    """Return Hermes' secret scope module when this is running inside Hermes."""
    try:
        from agent import secret_scope
    except ImportError:
        return None
    return secret_scope


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a Zulip setting without leaking across Hermes profiles.

    Outside Hermes this is exactly ``os.getenv``.  Inside Hermes, use its
    scoped resolver whenever a profile scope is installed (and let its
    fail-closed guard reject unscoped reads while multiplexing is active).
    """
    scope = _secret_scope_module()
    if scope is not None:
        active_scope = scope.current_secret_scope()
        if active_scope is not None or scope.is_multiplex_active():
            return scope.get_secret(name, default)
    return os.getenv(name, default)


def should_snapshot_settings() -> bool:
    """Whether construction is happening under Hermes profile isolation."""
    scope = _secret_scope_module()
    return bool(
        scope is not None
        and (scope.current_secret_scope() is not None or scope.is_multiplex_active())
    )


def has_active_profile_scope() -> bool:
    """Return whether a Hermes secret scope makes profile data authoritative."""
    scope = _secret_scope_module()
    return bool(scope is not None and scope.current_secret_scope() is not None)


def is_unscoped_multiplexer() -> bool:
    """Whether Hermes is multiplexing but this call has no active profile."""
    scope = _secret_scope_module()
    return bool(
        scope is not None
        and scope.is_multiplex_active()
        and scope.current_secret_scope() is None
    )


def get_profile_data_dir() -> str:
    """Return the active profile's Hermes home, with legacy fallbacks.

    ``get_hermes_home`` observes Hermes' context-local home override, unlike
    ``HERMES_DATA_DIR`` which is process-global and unsafe in a multiplexer.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        get_hermes_home = None
    if get_hermes_home is not None:
        return str(get_hermes_home())
    return os.getenv("HERMES_DATA_DIR", str(Path.home() / ".hermes"))


def snapshot_settings(names: tuple[str, ...]) -> dict[str, str]:
    """Resolve settings once so an adapter cannot observe a later scope swap."""
    return {name: get_setting(name, "") or "" for name in names}
