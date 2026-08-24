"""Tests for zulip.recovery — interrupted message recovery after restart."""

import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# Mirrors the message-related surface of ``zulip.Client`` (python-zulip-api
# 0.9.x). ``spec_set`` makes any other attribute access raise AttributeError,
# exactly like the real SDK does — a plain ``MagicMock()`` silently invents
# methods, which is how ``get_private_messages`` went unnoticed.
_SDK_CLIENT_METHODS = [
    "get_messages",
    "get_raw_message",
    "send_message",
    "update_message",
    "update_message_flags",
    "add_reaction",
    "remove_reaction",
]


def _sdk_like_client(messages):
    client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
    client.get_messages.return_value = {"result": "success", "messages": messages}
    return client


async def _real_sdk_call(fn, *args, timeout, **kwargs):
    """Same shape as ``ZulipAdapter._sdk_call``: run the sync SDK fn in a thread."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)


class TestRecoverInterruptedMessages:
    """Tests for recover_interrupted_messages()."""

    @pytest.mark.asyncio
    async def test_fetches_dms_via_sdk_get_messages(self, caplog):
        """Regression: recovery must use ``Client.get_messages`` (GET /messages).

        Before the fix it called ``client.get_private_messages``, which does
        not exist on the zulip SDK, so every gateway start logged
        ``zulip recovery: failed [error='Client' object has no attribute
        'get_private_messages']`` and recovery silently never ran.
        """
        from zulip.recovery import (
            RECOVERY_NARROW,
            RECOVERY_SCAN_LIMIT,
            recover_interrupted_messages,
        )

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = _sdk_like_client(messages)
        handle_message = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="zulip.recovery"):
            count = await recover_interrupted_messages(
                client=client,
                bot_email="bot@test.com",
                bot_user_id="1",
                reaction_start="eyes",
                reaction_success="check_mark",
                reaction_error="warning",
                handle_message=handle_message,
                sdk_call=_real_sdk_call,
                send_timeout=30,
            )

        assert count == 1
        handle_message.assert_awaited_once()
        assert "zulip recovery: failed" not in caplog.text
        client.get_messages.assert_called_once_with(
            {
                "anchor": "newest",
                "num_before": RECOVERY_SCAN_LIMIT,
                "num_after": 0,
                "narrow": RECOVERY_NARROW,
            }
        )

    def test_recovery_narrow_targets_direct_messages(self):
        from zulip.recovery import RECOVERY_NARROW, RECOVERY_SCAN_LIMIT

        assert RECOVERY_NARROW == [{"operator": "is", "operand": "dm"}]
        assert 1 <= RECOVERY_SCAN_LIMIT <= 1000  # Zulip caps num_before at 1000

    @pytest.mark.asyncio
    async def test_sdk_call_receives_message_filters(self):
        """The filter dict is passed positionally, matching ``get_messages(message_filters)``."""
        from zulip.recovery import recover_interrupted_messages

        client = _sdk_like_client([])
        sdk_call = AsyncMock(return_value={"result": "success", "messages": []})

        await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )

        sdk_call.assert_awaited_once()
        args, kwargs = sdk_call.await_args
        assert args[0] is client.get_messages
        assert args[1]["narrow"] == [{"operator": "is", "operand": "dm"}]
        assert args[1]["num_after"] == 0
        assert kwargs == {"timeout": 30}

    @pytest.mark.asyncio
    async def test_no_messages_returns_zero(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": []})

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_bot_own_messages(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {"id": 1, "sender_email": "bot@test.com", "sender_id": 1, "reactions": []},
            {"id": 2, "sender_email": "user@test.com", "sender_id": 2, "reactions": []},
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_without_start_reaction(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {"id": 1, "sender_email": "user@test.com", "sender_id": 2, "reactions": []},
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_with_end_reaction(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                    {"emoji_name": "check_mark", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_with_bot_response(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
            {
                "id": 2,
                "sender_email": "bot@test.com",
                "sender_id": 1,
                "reactions": [],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovers_interrupted_message(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 1
        handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sets_recovery_session_key(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )

        # Verify the message was tagged with a recovery session key
        called_msg = handle_message.await_args[0][0]
        assert "_recovery_session_key" in called_msg
        assert "recovery" in called_msg["_recovery_session_key"]

    @pytest.mark.asyncio
    async def test_handles_api_failure_gracefully(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "error", "msg": "API error"})

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(side_effect=RuntimeError("connection lost"))

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
