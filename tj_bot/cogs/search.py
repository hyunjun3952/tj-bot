from __future__ import annotations

import dataclasses
import functools
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Self

import discord
from discord import app_commands
from discord.utils import escape_markdown

from tj_bot.cogs import TJCog
from tj_bot.models import NationType, SearchType
from tj_bot.ui import (
    FIELD_NAME_LIMIT,
    FIELD_VALUE_LIMIT,
    QUERY_LIMIT,
    UNTITLED,
    BasePaginationView,
    badge_suffix,
    enforce_embed_limit,
    escape_or,
    song_field_name,
    song_field_value,
    truncate,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tj_bot.client import TJMediaClient
    from tj_bot.models import SearchResult, SearchSection, Song

log = logging.getLogger(__name__)

_PREVIEW_ROWS = 5
_RESET_VALUE = "__all__"


def _song_field(song: Song) -> tuple[str, str]:
    parts = [f"가수: {escape_or(song.artist)}"]
    if song.lyricist:
        parts.append(f"작사: {escape_markdown(song.lyricist)}")
    if song.composer:
        parts.append(f"작곡: {escape_markdown(song.composer)}")
    return song_field_name(song.song_number, song.title, song.badge), song_field_value(" | ".join(parts))


def _section_summary(section: SearchSection) -> str:
    lines = [f"`{song.song_number}` {escape_or(song.title, UNTITLED)} — {escape_or(song.artist)}{badge_suffix(song.badge)}" for song in section.songs]
    return truncate("\n".join(lines), FIELD_VALUE_LIMIT)


def _empty_description(nation: NationType) -> str:
    blocks = ["검색 결과가 없습니다."]
    if nation is not NationType.ALL:
        blocks.append(f"곡 분류가 “{nation.label}”로 설정돼 있어요. 아래 메뉴에서 “전체”로 바꿔 보세요.")
    if nation in (NationType.ALL, NationType.JAPANESE):
        blocks.append("일본곡은 한글 발음으로도 검색할 수 있어요. 예) 요루니카케루\n가수는 영문명으로도 검색돼요. 예) YOASOBI")
    return "\n\n".join(blocks)


def _build_embed(result: SearchResult, *, display_query: str, resolved_query: str, search_type: SearchType, nation: NationType) -> discord.Embed:
    embed = discord.Embed(title="TJ 검색 결과", color=0x00BFFF)
    embed.set_footer(text=f"페이지 {result.page} / {result.total_pages} · {search_type.label} · {nation.label}")
    notes = [f"결과가 없어서 “{resolved_query}”(으)로 찾았어요."] if resolved_query != display_query else []

    if result.is_empty:
        embed.description = "\n\n".join([*notes, _empty_description(nation)])
        return embed

    # TJ returns only a few rows per category until one is chosen, so a lone full category looks like the complete list without the note.
    if search_type is SearchType.ALL and _selectable_categories(result):
        notes.append("유형별 미리보기는 5곡까지만 보여줘요. 아래 **유형** 메뉴에서 고르면 전체를 볼 수 있어요.")
    # A combined search collapses each category into one field to stay under Discord's 25-field limit.
    if len(result.sections) > 1:
        for section in result.sections:
            embed.add_field(name=truncate(escape_or(section.category), FIELD_NAME_LIMIT), value=_section_summary(section), inline=False)
    else:
        for song in result.sections[0].songs:
            name, value = _song_field(song)
            embed.add_field(name=name, value=value, inline=False)
    embed.description = "\n\n".join(notes) or None
    return enforce_embed_limit(embed)


def _selectable_categories(result: SearchResult) -> tuple[SearchType, ...]:
    """Return only the search types that have actual results and aren't already fully shown by the single preview."""
    categories = tuple(section.search_type for section in result.sections if section.search_type is not None)
    if len(categories) == 1:
        song_count = sum(len(section.songs) for section in result.sections if section.search_type is not None)
        if song_count < _PREVIEW_ROWS:
            return ()
    return categories


async def _parse_select[E: StrEnum](interaction: discord.Interaction, kind: str, value: str, enum: type[E], default: E, /) -> E | None:
    """Map a select value back to its enum member, replying with an error (and returning None) when a stale menu sends an unknown one."""
    try:
        return default if value == _RESET_VALUE else enum(value)
    except ValueError:
        log.warning("Unknown %s selection value: %r", kind, value)
        await interaction.followup.send("이 메뉴는 더 이상 사용할 수 없어요. 명령어를 다시 실행해 주세요.", ephemeral=True)
        return None


@dataclasses.dataclass(frozen=True, kw_only=True)
class _ViewState:
    """The search view's state, rolled back all at once if rendering fails."""

    query: str
    display_query: str
    search_type: SearchType
    nation: NationType
    result: SearchResult
    categories: tuple[SearchType, ...]


class SearchPaginationView(BasePaginationView):
    def __init__(self, *, origin: discord.Interaction, client: TJMediaClient, query: str, display_query: str, result: SearchResult) -> None:
        super().__init__(origin=origin, page=result.page, total_pages=result.total_pages)
        self._client = client
        self._origin_query = query
        self._origin_display_query = display_query
        self._apply_state(
            _ViewState(
                query=query,
                display_query=display_query,
                search_type=SearchType.ALL,
                nation=NationType.ALL,
                result=result,
                categories=_selectable_categories(result),
            )
        )

    # Options are filled in by the _sync_* methods; Discord rejects a select that is sent without any, so they're hidden until then.
    @discord.ui.select(cls=discord.ui.Select, options=[], row=2)
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select[Self]) -> None:
        if select.values:
            await self.switch_category(interaction, select.values[0])

    @discord.ui.select(cls=discord.ui.Select, placeholder="곡분류 바꾸기", options=[], row=3)
    async def nation_select(self, interaction: discord.Interaction, select: discord.ui.Select[Self]) -> None:
        if select.values:
            await self.switch_nation(interaction, select.values[0])

    def _sync_category_select(self) -> None:
        needed = bool(self._state.categories) or (self._state.result.is_empty and self._state.search_type is not SearchType.ALL)
        if not needed:
            self._toggle_item(self.category_select, show=False)
            return

        narrowed = self._state.search_type is not SearchType.ALL
        self.category_select.options = [
            *([discord.SelectOption(label="통합검색으로 돌아가기", value=_RESET_VALUE)] if narrowed else []),
            *(
                discord.SelectOption(label=search_type.label, value=search_type.value, default=search_type is self._state.search_type)
                for search_type in self._state.categories
            ),
        ]
        self.category_select.placeholder = "다른 유형 보기" if narrowed else "유형을 골라 전체 결과 보기"
        self._toggle_item(self.category_select, show=True)

    def _sync_nation_select(self) -> None:
        # Discord rejects an empty option value.
        self.nation_select.options = [
            discord.SelectOption(label=nation.label, value=nation.value or _RESET_VALUE, default=nation is self._state.nation)
            for nation in NationType
        ]
        self._toggle_item(self.nation_select, show=True)

    def _capture_state(self) -> Callable[[], None]:
        return functools.partial(self._apply_state, self._state)

    def _apply_state(self, state: _ViewState) -> None:
        self._state = state
        self._page = state.result.page
        self._total_pages = state.result.total_pages
        self._update_buttons()
        self._sync_category_select()
        self._sync_nation_select()

    async def _categories_for(self, search_type: SearchType, nation: NationType, result: SearchResult) -> tuple[SearchType, ...]:
        if search_type is SearchType.ALL:
            return _selectable_categories(result)
        if nation is self._state.nation:
            return self._state.categories
        # A narrowed view has no combined result for the new nation, so fetch one (cached) or the menu goes stale.
        return _selectable_categories(await self._client.search(self._origin_query, nation=nation))

    async def _reload(self, interaction: discord.Interaction, *, query: str, display_query: str, search_type: SearchType, nation: NationType) -> None:
        result = await self._client.search(query, search_type=search_type, nation=nation, page=1)
        categories = await self._categories_for(search_type, nation, result)
        state = _ViewState(
            query=result.matched_query or query,
            display_query=display_query,
            search_type=search_type,
            nation=nation,
            result=result,
            categories=categories,
        )
        await self._show(interaction, state)

    async def _show(self, interaction: discord.Interaction, state: _ViewState) -> None:
        if self.is_finished():  # expired while this render waited for the lock; don't re-enable its buttons
            return
        self._apply_state(state)
        embed = _build_embed(
            state.result, display_query=state.display_query, resolved_query=state.query, search_type=state.search_type, nation=state.nation
        )
        await interaction.edit_original_response(embed=embed, view=self)

    async def go_to_page(self, interaction: discord.Interaction, page: int, *, relative: bool = False) -> None:
        async with self._guarded_render(interaction):
            state = self._state
            result = await self._client.search(
                state.query, search_type=state.search_type, nation=state.nation, page=self._resolve_page(page, relative=relative)
            )
            await self._show(interaction, dataclasses.replace(state, result=result))

    async def switch_category(self, interaction: discord.Interaction, value: str) -> None:
        async with self._guarded_render(interaction):
            if (search_type := await _parse_select(interaction, "유형", value, SearchType, SearchType.ALL)) is None:
                return
            await self._reload(
                interaction, query=self._origin_query, display_query=self._origin_display_query, search_type=search_type, nation=self._state.nation
            )

    async def switch_nation(self, interaction: discord.Interaction, value: str) -> None:
        async with self._guarded_render(interaction):
            if (nation := await _parse_select(interaction, "곡분류", value, NationType, NationType.ALL)) is None:
                return
            await self._reload(
                interaction, query=self._state.query, display_query=self._state.display_query, search_type=self._state.search_type, nation=nation
            )


def _cooldown_key(interaction: discord.Interaction) -> int | None:
    # A blank query is rejected without touching TJ, so it shouldn't cost the user the cooldown (None means exempt).
    return interaction.user.id if str(getattr(interaction.namespace, "검색어", "")).strip() else None


class SearchCog(TJCog):
    @app_commands.command(name="검색", description="TJ 미디어에서 노래를 검색해요")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 5, key=_cooldown_key)  # one zero-hit search fans out to ~6 TJ requests behind a shared semaphore
    @app_commands.describe(검색어="곡명, 가수명, 곡번호 (초성 ㅇㅇㅇ, 일본곡은 한글 발음·로마자도 가능)")
    async def search(self, interaction: discord.Interaction, 검색어: app_commands.Range[str, 1, QUERY_LIMIT]) -> None:
        # A public defer makes the first followup ignore ephemeral, so finish input validation first.
        if not 검색어.strip():
            await interaction.response.send_message("검색어를 입력해 주세요.", ephemeral=True)
            return

        await interaction.response.defer()
        result = await self._client.search(검색어)
        resolved_query = result.matched_query or 검색어  # pagination uses the query that hit
        embed = _build_embed(result, display_query=검색어, resolved_query=resolved_query, search_type=SearchType.ALL, nation=NationType.ALL)
        view = SearchPaginationView(origin=interaction, client=self._client, query=resolved_query, display_query=검색어, result=result)
        await interaction.followup.send(embed=embed, view=view)
