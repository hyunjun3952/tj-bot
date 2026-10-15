from __future__ import annotations

import abc
import asyncio
import contextlib
import functools
import logging
from typing import TYPE_CHECKING, Any, Self

import discord

from tj_bot.errors import reply_error

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from tj_bot.models import MediaBadge

log = logging.getLogger(__name__)

# Discord embed limits. Exceeding these gets the whole embed rejected with HTTP 400.
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
# Combined total across title/description/fields/footer (what discord.Embed.__len__ counts).
EMBED_TOTAL_LIMIT = 6000
# Search and filter inputs share it: a query that long only turns into an oversized GET that TJ rejects.
QUERY_LIMIT = 100
# Interaction tokens expire 15 min after the command; expiring views just before keeps the final disable edit usable.
_VIEW_LIFETIME = 14 * 60
UNTITLED = "(제목 없음)"


def truncate(text: str, limit: int, /) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def enforce_embed_limit(embed: discord.Embed, /) -> discord.Embed:
    """Shrink field values from the last one up, then drop trailing fields, until the embed fits Discord's total-length limit."""
    if len(embed) <= EMBED_TOTAL_LIMIT:
        return embed

    log.warning("Embed total length %d chars exceeds the limit, truncating", len(embed))

    # Shrink values first so the song number and title survive.
    for index in reversed(range(len(embed.fields))):
        overflow = len(embed) - EMBED_TOTAL_LIMIT
        if overflow <= 0:
            break
        field = embed.fields[index]
        value = field.value or ""
        embed.set_field_at(index, name=field.name, value=truncate(value, max(1, len(value) - overflow)), inline=field.inline)

    while len(embed) > EMBED_TOTAL_LIMIT and embed.fields:
        embed.remove_field(-1)

    return embed


def badge_suffix(badge: MediaBadge) -> str:
    return "".join(f" `{flag.name}`" for flag in badge)


def escape_or(text: str, fallback: str = "-", /) -> str:
    return discord.utils.escape_markdown(text) or fallback


def song_field_name(song_number: int, title: str, badge: MediaBadge, /) -> str:
    """The field title shared by both the search and recent-songs embeds."""
    return truncate(f"**{song_number}** · {escape_or(title, UNTITLED)}{badge_suffix(badge)}", FIELD_NAME_LIMIT)


def song_field_value(text: str) -> str:
    return truncate(text or "-", FIELD_VALUE_LIMIT)


class BasePaginationView(discord.ui.View, abc.ABC):
    """Pagination buttons and the state shared across all of them.

    Subclasses each keep their own frozen-dataclass state in `_state`, set only through `_apply_state`.
    """

    def __init__(self, *, origin: discord.Interaction, page: int, total_pages: int) -> None:
        # A sliding timeout restarts on every click and can outlive the origin interaction's token; a fixed lifetime can't.
        super().__init__(timeout=None)
        self._origin = origin
        self._interaction_lock = asyncio.Lock()  # ponytail: per-view lock, generation counter if slow
        self._expire_task: asyncio.Task[None] | None = None
        # Count from the command, not from here: a slow TJ fetch before the view exists eats into the token's lifetime.
        elapsed = (discord.utils.utcnow() - origin.created_at).total_seconds()
        asyncio.get_running_loop().call_later(max(0.0, _VIEW_LIFETIME - elapsed), self._schedule_expire)
        self._state: Any = None  # set by the subclass's _apply_state
        self._page = page
        self._total_pages = total_pages
        self._update_buttons()

    @abc.abstractmethod
    async def go_to_page(self, interaction: discord.Interaction, page: int, *, relative: bool = False) -> None: ...

    @abc.abstractmethod
    def _apply_state(self, state: Any) -> None: ...  # noqa: ANN401 — each subclass types its own state

    @abc.abstractmethod
    def render(self) -> discord.Embed:
        """The embed for the current state."""

    @abc.abstractmethod
    def fork(self, origin: discord.Interaction) -> BasePaginationView:
        """A copy at the current state, owned by `origin`'s user."""

    def _capture_state(self) -> Callable[[], None]:
        """Capture the current state; calling the result restores it."""
        return functools.partial(self._apply_state, self._state)

    @property
    def page(self) -> int:
        return self._page

    @property
    def total_pages(self) -> int:
        return self._total_pages

    def _resolve_page(self, page: int, *, relative: bool) -> int:
        target = self._page + page if relative else page
        return min(max(1, target), self._total_pages)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._origin.user.id:
            return True
        # Anyone else gets a private copy at the current state, so nobody overwrites the shared message.
        view = self.fork(interaction)
        await interaction.response.send_message(
            "내 화면에서만 보이는 복사본이에요. 여기서 자유롭게 넘겨 보세요.", embed=view.render(), view=view, ephemeral=True
        )
        return False

    def _toggle_item(self, item: discord.ui.Item[Self], *, show: bool) -> None:
        if show and item not in self.children:
            self.add_item(item)
        elif not show and item in self.children:
            self.remove_item(item)

    @contextlib.asynccontextmanager
    async def _guarded_render(self, interaction: discord.Interaction) -> AsyncGenerator[None]:
        """Defer, serialize via the interaction lock, and roll back + reply_error on any exception."""
        await interaction.response.defer()
        async with self._interaction_lock:
            restore = self._capture_state()
            try:
                yield
            except Exception as exc:
                restore()
                log.exception("Error while handling result interaction")  # view callbacks skip cog_command_error
                await reply_error(interaction, exc)

    async def _show(self, interaction: discord.Interaction, state: Any) -> None:  # noqa: ANN401 — the subclass's own state
        if self.is_finished():  # expired while this render waited for the lock; don't re-enable its buttons
            return
        self._apply_state(state)
        await interaction.edit_original_response(embed=self.render(), view=self)

    def _update_buttons(self) -> None:
        show = self._total_pages > 1
        # The jump button shares row 0 and is toggled with the others, so re-adding keeps their order.
        buttons: tuple[discord.ui.Button[Self], ...] = (self.first_button, self.prev_button, self.next_button, self.last_button, self.jump_button)
        for button in buttons:
            self._toggle_item(button, show=show)
        if not show:
            return

        self.first_button.disabled = self.prev_button.disabled = self._page <= 1
        self.next_button.disabled = self.last_button.disabled = self._page >= self._total_pages

    @discord.ui.button(label="처음으로", style=discord.ButtonStyle.secondary, row=0)
    async def first_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await self.go_to_page(interaction, 1)

    @discord.ui.button(label="이전", style=discord.ButtonStyle.primary, row=0)
    async def prev_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await self.go_to_page(interaction, -1, relative=True)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.primary, row=0)
    async def next_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await self.go_to_page(interaction, 1, relative=True)

    @discord.ui.button(label="마지막으로", style=discord.ButtonStyle.secondary, row=0)
    async def last_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await self.go_to_page(interaction, self._total_pages)

    @discord.ui.button(label="페이지 이동", style=discord.ButtonStyle.secondary, row=0)
    async def jump_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await interaction.response.send_modal(JumpModal(self))

    def _schedule_expire(self) -> None:
        self._expire_task = asyncio.create_task(self._expire())  # kept so the task isn't garbage-collected mid-run

    async def _expire(self) -> None:
        # stop() first so new clicks and late modal submits see is_finished(); the lock waits out a render already holding it.
        self.stop()
        async with self._interaction_lock:
            for child in self.children:
                if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                    child.disabled = True
            with contextlib.suppress(discord.HTTPException):
                await self._origin.edit_original_response(view=self)


async def reject_expired(interaction: discord.Interaction, owner: BasePaginationView, /) -> bool:
    """Modals have no timeout, so one opened before the view expired can still be submitted after."""
    if not owner.is_finished():
        return False
    await interaction.response.send_message("시간이 지나 만료됐어요. 명령어를 다시 실행해 주세요.", ephemeral=True)
    return True


class JumpModal(discord.ui.Modal):
    """The input form for jumping straight to a page number; long lists make the buttons alone slow."""

    def __init__(self, owner: BasePaginationView) -> None:
        super().__init__(title="페이지 이동")
        self._owner = owner
        self._page_input: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=f"이동할 페이지 (1~{owner.total_pages})", placeholder=str(owner.page), required=True, max_length=6
        )
        self.add_item(self._page_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await reject_expired(interaction, self._owner):
            return
        raw = self._page_input.value.strip()
        if not raw.isdecimal():
            await interaction.response.send_message("숫자만 입력해 주세요.", ephemeral=True)
            return
        page = int(raw)
        if not 1 <= page <= self._owner.total_pages:
            await interaction.response.send_message(f"1 ~ {self._owner.total_pages} 사이 페이지를 입력해 주세요.", ephemeral=True)
            return
        await self._owner.go_to_page(interaction, page)
