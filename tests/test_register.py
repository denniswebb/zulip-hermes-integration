"""Tests for the plugin entry point ``register(ctx)``."""

from zulip import adapter as adapter_module


class _CaptureCtx:
    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)


def _registration():
    ctx = _CaptureCtx()
    adapter_module.register(ctx)
    assert len(ctx.calls) == 1
    return ctx.calls[0]


def test_register_declares_zulip_platform():
    kwargs = _registration()
    assert kwargs["name"] == "zulip"
    assert kwargs["required_env"] == ["ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"]


def test_register_declares_cron_delivery_env_var():
    """Hermes cron only accepts ``deliver: zulip`` when the PlatformEntry
    names a home-channel env var (cron/scheduler.py:_is_known_delivery_platform).
    """
    kwargs = _registration()
    assert kwargs["cron_deliver_env_var"] == "ZULIP_HOME_CHANNEL"
