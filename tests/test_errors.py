import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord
import pytest

from tj_bot.errors import TJMediaError, reply_error
from tj_bot.ui import BasePaginationView


def _interaction(*, done: bool, user_id: int = 0, response_type: discord.InteractionResponseType | None = None, loading: bool = False) -> Any:  # noqa: ANN401
    response = SimpleNamespace(is_done=lambda: done, send_message=AsyncMock(), type=response_type)
    original = SimpleNamespace(flags=SimpleNamespace(loading=loading))
    return SimpleNamespace(
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id=user_id),
        original_response=AsyncMock(return_value=original),
        delete_original_response=AsyncMock(),
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [(TJMediaError("사용자용"), "사용자용"), (RuntimeError("secret token"), "처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.")],
)
def test_reply_error_masks_internal_errors(exc: Exception, expected: str) -> None:
    interaction = _interaction(done=True)
    asyncio.run(reply_error(interaction, exc))
    interaction.followup.send.assert_awaited_once_with(expected, ephemeral=True)


@pytest.mark.parametrize(("loading", "deleted"), [(True, True), (False, False)])
def test_reply_error_drops_public_thinking_placeholder(*, loading: bool, deleted: bool) -> None:
    interaction = _interaction(done=True, response_type=discord.InteractionResponseType.deferred_channel_message, loading=loading)
    asyncio.run(reply_error(interaction, TJMediaError("x")))
    assert interaction.delete_original_response.await_count == deleted
    interaction.followup.send.assert_awaited_once_with("x", ephemeral=True)


def test_reply_error_uses_initial_response_when_not_done() -> None:
    interaction = _interaction(done=False)
    asyncio.run(reply_error(interaction, TJMediaError("x")))
    interaction.response.send_message.assert_awaited_once_with("x", ephemeral=True)


@pytest.mark.parametrize(("user_id", "allowed"), [(1, True), (2, False)])
def test_interaction_check_gives_others_a_private_copy(user_id: int, *, allowed: bool) -> None:
    copy = SimpleNamespace(render=lambda: "embed")
    view = SimpleNamespace(_origin=SimpleNamespace(user=SimpleNamespace(id=1)), fork=lambda _interaction: copy)
    interaction = _interaction(done=False, user_id=user_id)
    assert asyncio.run(BasePaginationView.interaction_check(view, interaction)) is allowed  # pyrefly: ignore[bad-argument-type]
    if allowed:
        interaction.response.send_message.assert_not_awaited()
        return
    kwargs = interaction.response.send_message.await_args.kwargs
    assert (kwargs["embed"], kwargs["view"], kwargs["ephemeral"]) == ("embed", copy, True)
