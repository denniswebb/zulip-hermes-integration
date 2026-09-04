"""Multi-account config resolver for Zulip Hermes integration.

Supports backward-compatible single-account config and multi-account
configs. Future: full multi-account support with isolated event queues.
"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from .runtime_scope import SCOPED_SETTINGS_EXTRA_KEY, get_setting, is_unscoped_multiplexer


@dataclass
class ZulipAccount:
    """Represents a single Zulip account configuration."""

    name: str
    email: str
    api_key: str
    site: str
    streams: list[str] | None = None
    dm_policy: str = "open"
    allow_from: list[str] | None = None


class AccountResolver:
    """Resolve account configuration from env vars or Hermes config."""

    def __init__(self, extra: dict[str, Any] | None = None):
        self.extra = extra or {}

    def resolve(self) -> list[ZulipAccount]:
        """Return list of configured accounts.

        Backward-compatible: if no 'accounts' section, returns single account
        from env vars or top-level config.
        """
        accounts_raw = self.extra.get("accounts")
        if isinstance(accounts_raw, dict):
            return self._parse_multi(accounts_raw)
        return [self._single_account()]

    def _single_account(self) -> ZulipAccount:
        """Build single account from env vars or top-level config."""
        def setting(name: str, extra_key: str, default: str = "") -> str:
            captured = self.extra.get(SCOPED_SETTINGS_EXTRA_KEY)
            if isinstance(captured, dict):
                return str(captured.get(name) or self.extra.get(extra_key) or default)
            if is_unscoped_multiplexer():
                # Explicit config is not process-global and is safe to use
                # while Hermes deliberately fails closed on env reads.
                return str(self.extra.get(extra_key) or default)
            return get_setting(name, default) or self.extra.get(extra_key, default)

        return ZulipAccount(
            name="default",
            email=setting("ZULIP_EMAIL", "email"),
            api_key=setting("ZULIP_API_KEY", "api_key"),
            site=setting("ZULIP_SITE", "site"),
            streams=self._parse_streams(self.extra.get("streams")),
            dm_policy=setting("ZULIP_DM_POLICY", "dm_policy", "open"),
            allow_from=self._parse_allowlist(self.extra.get("allow_from")),
        )

    def _parse_multi(self, accounts_raw: dict) -> list[ZulipAccount]:
        """Parse multi-account config."""
        accounts = []
        for name, cfg in accounts_raw.items():
            if not isinstance(cfg, dict):
                continue
            account = ZulipAccount(
                name=name,
                email=cfg.get("email", ""),
                api_key=cfg.get("api_key", ""),
                site=cfg.get("site", ""),
                streams=self._parse_streams(cfg.get("streams")),
                dm_policy=cfg.get("dm_policy", "open"),
                allow_from=self._parse_allowlist(cfg.get("allow_from")),
            )
            accounts.append(account)
        return accounts if accounts else [self._single_account()]

    @staticmethod
    def _parse_streams(raw: Any) -> list[str] | None:
        if isinstance(raw, list):
            return [str(s) for s in raw]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return None

    @staticmethod
    def _parse_allowlist(raw: Any) -> list[str] | None:
        if isinstance(raw, list):
            return [str(s).strip().lower() for s in raw]
        if isinstance(raw, str):
            return [s.strip().lower() for s in raw.split(",") if s.strip()]
        return None
