from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Self

import discord
from discord import app_commands
from discord.utils import escape_markdown

from tj_bot.cogs import TJCog
from tj_bot.models import ROWS_PER_PAGE, choseong, compact, is_choseong_query
from tj_bot.ui import QUERY_LIMIT, BasePaginationView, enforce_embed_limit, escape_or, song_field_name, song_field_value

if TYPE_CHECKING:
    from tj_bot.models import RecentSong, RecentSongPage


def _total_pages(count: int) -> int:
    return max(1, (count + ROWS_PER_PAGE - 1) // ROWS_PER_PAGE)


def _filter_songs(songs: tuple[RecentSong, ...], query: str) -> tuple[RecentSong, ...]:
    """Filter by a case- and whitespace-insensitive substring match on artist name or song title; initial consonants (ㅇㅇㅇ) match too."""
    if not (needle := compact(query)):
        return songs
    form = choseong if is_choseong_query(needle) else compact
    return tuple(song for song in songs if needle in form(song.title) or needle in form(song.artist))


def _build_embed(result: RecentSongPage, songs: tuple[RecentSong, ...], page: int, *, filter_query: str) -> discord.Embed:
    embed = discord.Embed(title=f"TJ 최신곡 · {result.year}년 {result.month}월", color=0xFF6B6B)
    embed.set_footer(text=f"페이지 {page} / {_total_pages(len(songs))} · 전체 {len(result.songs)}곡")

    if not songs:
        embed.description = f"“{escape_markdown(filter_query)}”에 맞는 곡이 없어요." if filter_query else "이번 달 최신곡이 아직 없어요."
        return embed

    if filter_query:
        embed.description = f"“{escape_markdown(filter_query)}” 검색 결과 {len(songs)}곡"

    start = (page - 1) * ROWS_PER_PAGE
    for song in songs[start : start + ROWS_PER_PAGE]:
        embed.add_field(
            name=song_field_name(song.song_number, song.title, song.badge),
            value=song_field_value(f"가수: {escape_or(song.artist)} | 등록일: {escape_or(song.publish_date)}"),
            inline=False,
        )

    return enforce_embed_limit(embed)


async def _reject_expired(interaction: discord.Interaction, owner: RecentSongPaginationView, /) -> bool:
    """Modals have no timeout, so one opened before the view expired can still be submitted after."""
    if not owner.is_finished():
        return False
    await interaction.response.send_message("시간이 지나 만료됐어요. 명령어를 다시 실행해 주세요.", ephemeral=True)
    return True


class RecentSongFilterModal(discord.ui.Modal):
    """The input form that narrows the recent-songs list by artist name or title."""

    def __init__(self, owner: RecentSongPaginationView) -> None:
        super().__init__(title="가수·곡명으로 찾기")
        self._owner = owner
        self._query_input: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label="가수명 또는 곡명 (비우면 전체)",
            placeholder="예: 아이유, 좋은 날, ㅇㅇㅇ (초성)",
            required=False,
            max_length=QUERY_LIMIT,
            default=owner.filter_query,
        )
        self.add_item(self._query_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _reject_expired(interaction, self._owner):
            return
        await self._owner.apply_filter(interaction, self._query_input.value)


class RecentSongJumpModal(discord.ui.Modal):
    """The input form for jumping straight to a page number. Recent songs has enough pages that buttons alone are slow."""

    def __init__(self, owner: RecentSongPaginationView) -> None:
        super().__init__(title="페이지 이동")
        self._owner = owner
        self._page_input: discord.ui.TextInput[Self] = discord.ui.TextInput(
            label=f"이동할 페이지 (1~{owner.total_pages})", placeholder=str(owner.page), required=True, max_length=6
        )
        self.add_item(self._page_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if await _reject_expired(interaction, self._owner):
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


@dataclasses.dataclass(frozen=True, kw_only=True)
class _ViewState:
    """View state to roll back together if rendering fails."""

    songs: tuple[RecentSong, ...]
    filter_query: str
    page: int


class RecentSongPaginationView(BasePaginationView):
    def __init__(self, *, origin: discord.Interaction, result: RecentSongPage) -> None:
        super().__init__(origin=origin, page=1, total_pages=_total_pages(len(result.songs)))
        self._result = result
        self._state = _ViewState(songs=result.songs, filter_query="", page=1)
        self._sync_jump_button()

    @discord.ui.button(label="페이지 이동", style=discord.ButtonStyle.secondary, row=1)
    async def jump_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await interaction.response.send_modal(RecentSongJumpModal(self))

    @discord.ui.button(label="가수·곡명 검색", style=discord.ButtonStyle.secondary, row=1)
    async def filter_button(self, interaction: discord.Interaction, _button: discord.ui.Button[Self]) -> None:
        await interaction.response.send_modal(RecentSongFilterModal(self))

    @property
    def filter_query(self) -> str:
        return self._state.filter_query

    def _sync_jump_button(self) -> None:
        # Disabled rather than removed: add_item appends, so re-adding would move it after the filter button.
        self.jump_button.disabled = self._total_pages <= 1

    def _apply_state(self, state: _ViewState) -> None:
        self._state = state
        self._page = state.page
        self._total_pages = _total_pages(len(state.songs))
        self._update_buttons()
        self._sync_jump_button()

    async def _show(self, interaction: discord.Interaction, state: _ViewState) -> None:
        if self.is_finished():  # expired while this render waited for the lock; don't re-enable its buttons
            return
        self._apply_state(state)
        embed = _build_embed(self._result, state.songs, state.page, filter_query=state.filter_query)
        await interaction.edit_original_response(embed=embed, view=self)

    async def go_to_page(self, interaction: discord.Interaction, page: int, *, relative: bool = False) -> None:
        async with self._guarded_render(interaction):
            await self._show(interaction, dataclasses.replace(self._state, page=self._resolve_page(page, relative=relative)))

    async def apply_filter(self, interaction: discord.Interaction, query: str) -> None:
        query = query.strip()
        async with self._guarded_render(interaction):
            await self._show(interaction, _ViewState(songs=_filter_songs(self._result.songs, query), filter_query=query, page=1))


class RecentSongCog(TJCog):
    @app_commands.command(name="최신곡", description="TJ 미디어의 이번 달 최신곡을 보여줘요")
    @app_commands.guild_only()
    async def recent_songs(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        result = await self._client.get_recent_songs()
        embed = _build_embed(result, result.songs, page=1, filter_query="")

        if not result.songs:
            await interaction.followup.send(embed=embed)
            return

        await interaction.followup.send(embed=embed, view=RecentSongPaginationView(origin=interaction, result=result))
