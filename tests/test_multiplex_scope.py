"""Regression coverage for profile-scoped Zulip adapters in one process."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from zulip import adapter as adapter_module
from zulip.adapter import (
    ZulipAdapter,
    _env_enablement,
    _resolve_chatmode,
    _resolve_standalone_credentials,
    _standalone_send,
    validate_config,
)
from zulip.media import resolve_media_max_mb
from zulip.runtime_scope import (
    PROFILE_DATA_DIR_EXTRA_KEY,
    SCOPED_SETTINGS_EXTRA_KEY,
    get_setting,
)
from zulip.workspace import BotWorkspace


class _ScopedHermes:
    """Small in-process stand-in for Hermes' ContextVar-facing API."""

    def __init__(self):
        self.scope: dict[str, str] | None = None
        self.home = ""
        self.multiplex = True

    def install(self, values: dict[str, str], home: Path) -> None:
        self.scope = values
        self.home = str(home)

    def current_secret_scope(self):
        return self.scope

    def is_multiplex_active(self):
        return self.multiplex

    def get_secret(self, name, default=None):
        if self.scope is None:
            raise RuntimeError("unscoped secret read")
        return self.scope.get(name, default)


@pytest.fixture
def scoped_hermes(monkeypatch):
    runtime = _ScopedHermes()
    agent = ModuleType("agent")
    secret_scope = ModuleType("agent.secret_scope")
    for name in ("current_secret_scope", "is_multiplex_active", "get_secret"):
        setattr(secret_scope, name, getattr(runtime, name))
    agent.secret_scope = secret_scope
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(runtime.home)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    return runtime


@pytest.fixture
def adapter_sdk(monkeypatch):
    class Client:
        def __init__(self, email=None, api_key=None, site=None):
            self.email = email
            self.api_key = api_key
            self.site = site

    sdk = SimpleNamespace(Client=Client)
    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setattr(adapter_module, "zulip", sdk)
    return sdk


def _config():
    return SimpleNamespace(extra={})


def _profile(email: str, key: str, *, chatmode: str, media: str) -> dict[str, str]:
    return {
        "ZULIP_SITE": "https://zulip.example.test",
        "ZULIP_EMAIL": email,
        "ZULIP_API_KEY": key,
        "ZULIP_CHATMODE": chatmode,
        "ZULIP_MEDIA_MAX_MB": media,
        "ZULIP_REACTIONS_ENABLED": "false",
        "ZULIP_DM_POLICY": "allowlist",
        "ZULIP_ALLOWED_USERS": f"{email},operator@example.test",
    }


def test_adapters_snapshot_distinct_profile_credentials_and_behavior(
    scoped_hermes, adapter_sdk, tmp_path
):
    first = _profile("same-bot@example.test", "first-key", chatmode="oncall", media="3")
    second = _profile("same-bot@example.test", "second-key", chatmode="onchar", media="9")
    scoped_hermes.install(first, tmp_path / "first")
    first_adapter = ZulipAdapter(_config())
    scoped_hermes.install(second, tmp_path / "second")
    second_adapter = ZulipAdapter(_config())

    assert first_adapter.client is not second_adapter.client
    assert first_adapter.api_key == "first-key"
    assert first_adapter.api_token == "first-key"
    assert second_adapter.api_key == "second-key"
    assert first_adapter._settings["ZULIP_CHATMODE"] == "oncall"
    assert second_adapter._settings["ZULIP_CHATMODE"] == "onchar"
    assert _resolve_chatmode(settings=first_adapter._settings)[0] == "oncall"
    assert _resolve_chatmode(settings=second_adapter._settings)[0] == "onchar"
    assert first_adapter._reaction_cfg.enabled is False
    assert first_adapter._policy.allowlist == {"same-bot@example.test", "operator@example.test"}

    # Switching the active ContextVar after construction cannot alter adapter A.
    assert first_adapter._settings["ZULIP_MEDIA_MAX_MB"] == "3"
    assert second_adapter._settings["ZULIP_MEDIA_MAX_MB"] == "9"


def test_scoped_adapter_keeps_default_reactions_when_unset(
    scoped_hermes, adapter_sdk, tmp_path
):
    values = _profile("defaults@example.test", "defaults-key", chatmode="onmessage", media="5")
    values.pop("ZULIP_REACTIONS_ENABLED")
    scoped_hermes.install(values, tmp_path / "profile")

    adapter = ZulipAdapter(_config())
    assert adapter._reaction_cfg.enabled is True
    assert adapter._reaction_cfg.clear_on_finish is True


def test_profile_homes_isolate_all_persistent_paths(scoped_hermes, adapter_sdk, tmp_path):
    values = _profile("same-bot@example.test", "shared-key", chatmode="onmessage", media="5")
    first_home, second_home = tmp_path / "first", tmp_path / "second"
    scoped_hermes.install(values, first_home)
    first = ZulipAdapter(_config())
    first_workspace = BotWorkspace()
    scoped_hermes.install(values, second_home)
    second = ZulipAdapter(_config())
    second_workspace = BotWorkspace()

    assert first._queue_mgr._persistence_path() != second._queue_mgr._persistence_path()
    assert first._dedupe._persistence_path() != second._dedupe._persistence_path()
    assert first._policy._persistence_path() != second._policy._persistence_path()
    assert first._audit_logger._log_path != second._audit_logger._log_path
    assert first_workspace.root != second_workspace.root
    assert str(first_home) in str(first._queue_mgr._persistence_path())
    assert str(second_home) in str(second_workspace.root)


def test_unscoped_primary_adapter_uses_config_load_snapshot(
    scoped_hermes, adapter_sdk, tmp_path, monkeypatch
):
    """The primary adapter is constructed after Hermes clears its load scope."""
    values = _profile("primary@example.test", "primary-key", chatmode="oncall", media="7")
    scoped_hermes.install(values, tmp_path / "default")
    extra = _env_enablement()
    assert extra is not None
    scoped_hermes.scope = None
    monkeypatch.setenv("ZULIP_API_KEY", "wrong-process-key")
    config = SimpleNamespace(extra=extra)

    assert validate_config(config) is True
    adapter = ZulipAdapter(config)
    assert adapter.api_key == "primary-key"
    assert adapter._settings["ZULIP_CHATMODE"] == "oncall"
    assert adapter._data_dir == str(tmp_path / "default")


def test_unscoped_multiplexer_accepts_explicit_config_without_env(
    scoped_hermes, adapter_sdk, tmp_path, monkeypatch
):
    scoped_hermes.home = str(tmp_path / "default")
    scoped_hermes.scope = None
    monkeypatch.setenv("ZULIP_API_KEY", "wrong-process-key")
    config = SimpleNamespace(extra={
        "site": "https://configured.example.test",
        "email": "configured@example.test",
        "api_key": "configured-key",
    })

    assert validate_config(config) is True
    adapter = ZulipAdapter(config)
    assert (adapter.site, adapter.email, adapter.api_key) == (
        "https://configured.example.test", "configured@example.test", "configured-key"
    )
    assert adapter._reaction_cfg.enabled is True


def test_env_enablement_retains_snapshot_without_environment_credentials(
    scoped_hermes, tmp_path, monkeypatch
):
    for name in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    scoped_hermes.install({}, tmp_path / "profile")

    extra = _env_enablement()
    assert extra is not None
    assert extra[SCOPED_SETTINGS_EXTRA_KEY]["ZULIP_API_KEY"] == ""
    assert extra[PROFILE_DATA_DIR_EXTRA_KEY] == str(tmp_path / "profile")
    assert not {"api_key", "email", "site"} & extra.keys()


@pytest.mark.asyncio
async def test_validation_and_standalone_sender_use_active_scope(scoped_hermes, tmp_path, monkeypatch):
    values = _profile("scoped@example.test", "scoped-key", chatmode="onmessage", media="11")
    scoped_hermes.install(values, tmp_path / "profile")
    monkeypatch.setenv("ZULIP_API_KEY", "global-key")
    monkeypatch.setenv("ZULIP_EMAIL", "global@example.test")
    monkeypatch.setenv("ZULIP_SITE", "https://global.example.test")

    assert validate_config(_config()) is True
    assert _resolve_standalone_credentials(_config()) == (
        "https://zulip.example.test", "scoped@example.test", "scoped-key"
    )
    client = MagicMock()
    client.send_message.return_value = {"result": "success", "id": 1}
    with patch.object(adapter_module, "_get_cached_client", return_value=client) as get_client:
        result = await _standalone_send(_config(), "1", "hello")
    assert result["success"] is True
    get_client.assert_called_once_with("https://zulip.example.test", "scoped@example.test", "scoped-key")


def test_active_scope_miss_never_leaks_process_environment(scoped_hermes, tmp_path, monkeypatch):
    scoped_hermes.install({"ZULIP_EMAIL": "only-scoped@example.test"}, tmp_path / "profile")
    monkeypatch.setenv("ZULIP_API_KEY", "global-secret")
    assert get_setting("ZULIP_API_KEY", "") == ""
    assert validate_config(_config()) is False


def test_legacy_unscoped_environment_behavior_remains(monkeypatch):
    monkeypatch.setenv("ZULIP_MEDIA_MAX_MB", "12")
    monkeypatch.setenv("ZULIP_API_KEY", "legacy-key")
    monkeypatch.setenv("ZULIP_EMAIL", "legacy@example.test")
    monkeypatch.setenv("ZULIP_SITE", "https://legacy.example.test")
    assert resolve_media_max_mb() == 12
    assert _resolve_standalone_credentials(_config()) == (
        "https://legacy.example.test", "legacy@example.test", "legacy-key"
    )
    assert BotWorkspace().root.name == "hermes_bot_workspace"
