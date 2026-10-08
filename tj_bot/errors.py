from __future__ import annotations

import contextlib

import discord


class TJMediaError(Exception):
    pass


async def reply_error(interaction: discord.Interaction, exc: BaseException) -> None:
    """Notify the user of an error, whether or not the interaction has been responded to yet. Callers log."""
    # TJMediaError text is written for the user; anything else is masked so internal details don't leak.
    message = str(exc) if isinstance(exc, TJMediaError) else "처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
    with contextlib.suppress(discord.HTTPException):
        if interaction.response.is_done():  # followup needs an initial response to exist first
            await _clear_public_placeholder(interaction)
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def _clear_public_placeholder(interaction: discord.Interaction) -> None:
    """A public defer's "thinking…" message swallows the first followup's ephemeral flag, so drop it and let the error go out privately."""
    if interaction.response.type is not discord.InteractionResponseType.deferred_channel_message:
        return
    if (await interaction.original_response()).flags.loading:
        await interaction.delete_original_response()
