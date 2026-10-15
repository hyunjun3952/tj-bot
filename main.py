import asyncio
import contextlib
import logging
import os
import signal

import discord
from discord.ext import commands
from dotenv import load_dotenv

from tj_bot.client import TJMediaClient
from tj_bot.cogs.recent import RecentSongCog
from tj_bot.cogs.search import SearchCog


class TJBot(commands.Bot):
    def __init__(self, tj_client: TJMediaClient, guild_id: int | None) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self._tj_client = tj_client
        self._guild_id = guild_id

    async def setup_hook(self) -> None:
        await self.add_cog(SearchCog(self._tj_client))
        await self.add_cog(RecentSongCog(self._tj_client))
        if self._guild_id is None:
            await self.tree.sync()
            return
        # A dev guild gets the commands immediately; global propagation can take up to an hour.
        guild = discord.Object(self._guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    # discord.py is chatty at INFO; only our own session-refresh and transliteration logs are worth that level.
    logging.getLogger("tj_bot").setLevel(logging.INFO)

    load_dotenv()
    token = os.environ["DISCORD_TOKEN"]  # read before the TJ session fetch so a missing token fails fast
    guild_id = os.getenv("GUILD_ID")
    # systemd restarts send SIGTERM, which would skip the finally below; cancelling this task routes it through the same path as Ctrl+C.
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)  # pyrefly: ignore[missing-attribute]

    async with TJMediaClient() as tj_client:
        bot = TJBot(tj_client, int(guild_id) if guild_id else None)
        try:
            await bot.start(token)
        finally:
            # Ctrl+C makes start() exit via exception; without closing here, the
            # gateway socket and aiohttp session are left unclosed and only warn.
            await bot.close()


if __name__ == "__main__":
    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(main())
