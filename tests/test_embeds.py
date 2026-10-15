import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import discord

from tj_bot.cogs.recent import _build_embed as build_recent_embed
from tj_bot.cogs.recent import _filter_songs
from tj_bot.cogs.search import SearchPaginationView, _parse_select, _selectable_categories
from tj_bot.cogs.search import _build_embed as build_search_embed
from tj_bot.cogs.search import _ViewState as _SearchViewState
from tj_bot.models import MediaBadge, NationType, RecentSong, RecentSongPage, SearchResult, SearchSection, SearchType, Song
from tj_bot.ui import EMBED_TOTAL_LIMIT, FIELD_VALUE_LIMIT, BasePaginationView, badge_suffix, enforce_embed_limit


def test_badge_suffix_lists_every_flag_in_order() -> None:
    assert badge_suffix(MediaBadge.MEDLEY | MediaBadge.MV) == " `MV` `MEDLEY`"
    assert badge_suffix(MediaBadge.NONE) == ""


def test_enforce_embed_limit_stays_under_total() -> None:
    embed = discord.Embed(title="t")
    for index in range(10):
        embed.add_field(name=f"field {index}", value="x" * FIELD_VALUE_LIMIT, inline=False)
    enforce_embed_limit(embed)
    assert len(embed) <= EMBED_TOTAL_LIMIT
    assert embed.fields[0].name == "field 0"


def test_recent_filter_query_is_escaped() -> None:
    embed = build_recent_embed(RecentSongPage(year=2026, month=9, songs=()), (), 1, filter_query="**x**")
    assert "\\*\\*x\\*\\*" in (embed.description or "")


def test_combined_search_embed_previews_each_section() -> None:
    sections = (
        SearchSection(category="곡 제목", songs=(Song(song_number=1, title="좋은 날", artist="아이유"),), search_type=SearchType.TITLE),
        SearchSection(category="가수", songs=(Song(song_number=2, title="밤편지", artist="아이유"),), search_type=SearchType.ARTIST),
    )
    result = SearchResult(sections=sections, page=1, total_pages=1)
    embed = build_search_embed(result, display_query="아이유", resolved_query="아이유", search_type=SearchType.ALL, nation=NationType.ALL)
    assert [field.name for field in embed.fields] == ["곡 제목", "가수"]
    assert "미리보기" in (embed.description or "")
    assert _selectable_categories(result) == (SearchType.TITLE, SearchType.ARTIST)
    assert _selectable_categories(SearchResult(sections=sections[:1], page=1, total_pages=1)) == ()


def test_combined_search_with_one_full_category_still_says_it_is_a_preview() -> None:
    songs = tuple(Song(song_number=number, title=f"곡{number}", artist="아이유") for number in range(1, 6))
    result = SearchResult(sections=(SearchSection(category="가수", songs=songs, search_type=SearchType.ARTIST),), page=1, total_pages=1)
    embed = build_search_embed(result, display_query="아이유", resolved_query="아이유", search_type=SearchType.ALL, nation=NationType.ALL)
    assert len(embed.fields) == 5
    assert "미리보기" in (embed.description or "")
    narrowed = build_search_embed(result, display_query="아이유", resolved_query="아이유", search_type=SearchType.ARTIST, nation=NationType.ALL)
    assert "미리보기" not in (narrowed.description or "")


def _recent(number: int, title: str, artist: str) -> RecentSong:
    return RecentSong.model_validate({"pro": number, "indexTitle": title, "indexSong": artist})


def test_recent_filter_matches_initial_consonants() -> None:
    songs = (_recent(1, "좋은 날", "아이유"), _recent(2, "Love Poem", "IU"), _recent(3, "사쿠라", "케이"))
    assert [song.song_number for song in _filter_songs(songs, "ㅇㅇㅇ")] == [1]
    assert [song.song_number for song in _filter_songs(songs, "ㅈㅇ ㄴ")] == [1]  # whitespace-insensitive, matches the title
    assert [song.song_number for song in _filter_songs(songs, "ㅅㅋㄹ")] == [3]
    assert _filter_songs(songs, "ㅎㅎㅎ") == ()


def test_recent_filter_keeps_plain_substring_matching() -> None:
    songs = (_recent(1, "좋은 날", "아이유"), _recent(2, "Love Poem", "IU"))
    assert [song.song_number for song in _filter_songs(songs, "love")] == [2]
    assert [song.song_number for song in _filter_songs(songs, "아이")] == [1]
    assert _filter_songs(songs, "") == songs


class _StubView(BasePaginationView):
    async def go_to_page(self, interaction: discord.Interaction, page: int, *, relative: bool = False) -> None: ...

    def _apply_state(self, state: object) -> None: ...

    def render(self) -> discord.Embed: ...

    def fork(self, origin: discord.Interaction) -> BasePaginationView: ...


def test_resolve_page_clamps_to_range() -> None:
    view = object.__new__(_StubView)
    view._page, view._total_pages = 2, 3  # noqa: SLF001
    assert view._resolve_page(-5, relative=True) == 1  # noqa: SLF001
    assert view._resolve_page(5, relative=True) == 3  # noqa: SLF001
    assert view._resolve_page(0, relative=False) == 1  # noqa: SLF001
    assert view._resolve_page(9, relative=False) == 3  # noqa: SLF001


def test_parse_select_rejects_unknown_value() -> None:
    interaction: Any = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))

    async def run() -> None:
        assert await _parse_select(interaction, "유형", "999", SearchType, SearchType.ALL) is None
        interaction.followup.send.assert_awaited_once()
        assert await _parse_select(interaction, "유형", "__all__", SearchType, SearchType.ALL) is SearchType.ALL
        assert await _parse_select(interaction, "유형", "2", SearchType, SearchType.ALL) is SearchType.ARTIST

    asyncio.run(run())


def test_fallback_month_is_explained() -> None:
    page = RecentSongPage(year=2026, month=9, songs=(_recent(1, "좋은 날", "아이유"),), is_fallback=True)
    assert "지난달" in (build_recent_embed(page, page.songs, 1, filter_query="").description or "")
    assert build_recent_embed(page.model_copy(update={"is_fallback": False}), page.songs, 1, filter_query="").description is None


def test_search_view_can_start_narrowed() -> None:
    songs = tuple(Song(song_number=number, title=f"곡{number}", artist="아이유") for number in range(1, 11))
    result = SearchResult(sections=(SearchSection(category="가수", songs=songs, search_type=SearchType.ARTIST),), page=1, total_pages=3)
    state = _SearchViewState(
        query="아이유",
        display_query="아이유",
        search_type=SearchType.ARTIST,
        nation=NationType.KOREAN,
        result=result,
        categories=(SearchType.TITLE, SearchType.ARTIST),
    )
    origin: Any = SimpleNamespace(created_at=discord.utils.utcnow(), user=SimpleNamespace(id=1))

    async def run() -> None:
        view = SearchPaginationView(origin=origin, client=AsyncMock(), origin_query="아이유", state=state)
        assert [option.default for option in view.category_select.options] == [False, False, True]  # reset, 곡제목, 가수명
        assert next(option for option in view.nation_select.options if option.default).label == "가요"
        assert view.jump_button in view.children
        assert view.render().footer.text == "페이지 1 / 3 · 가수명 · 가요"
        fork = view.fork(origin)
        assert fork is not view
        assert fork.render().footer.text == view.render().footer.text

    asyncio.run(run())
