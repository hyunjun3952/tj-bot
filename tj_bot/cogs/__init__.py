from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from discord import app_commands
from discord.ext import commands

from tj_bot.errors import TJMediaError, reply_error

if TYPE_CHECKING:
    import discord

    from tj_bot.client import TJMediaClient

log = logging.getLogger(__name__)


class TJCog(commands.Cog):
    def __init__(self, client: TJMediaClient) -> None:
        self._client = client

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await reply_error(interaction, TJMediaError(f"너무 자주 검색했어요. {math.ceil(error.retry_after)}초 뒤에 다시 시도해 주세요."))
            return
        command_name = interaction.command.qualified_name if interaction.command is not None else "?"
        log.error("Error while handling command: /%s", command_name, exc_info=error)
        await reply_error(interaction, error.original if isinstance(error, app_commands.CommandInvokeError) else error)
