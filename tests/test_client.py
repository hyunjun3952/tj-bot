import asyncio
import json

import httpx
import pytest

from tj_bot.client import _MAX_CONCURRENT_FETCHES, TJMediaClient, _parse_recent_response, _rank_search_result
from tj_bot.errors import TJMediaError
from tj_bot.models import MediaBadge, SearchResult, SearchSection, SearchType, Song

_SAKURA_HTML = """
<div class="title-select-area"><h2>곡 제목</h2></div>
<div class="chart-list-area">
  <div class="grid-container list ico">
    <li class="grid-item center pos-type"><span class="num2">12345</span></li>
    <li class="grid-item title3"><div class="flex-box"><p>사쿠라</p></div></li>
  </div>
</div>
"""


def _search_transport(searched: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(200)
        searched.append(request.url.params["searchTxt"])
        body = _SAKURA_HTML if request.url.params["searchTxt"] == "사쿠라" else '<div class="chart-list-area"></div>'
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


def test_rank_puts_exact_then_prefix_then_substring() -> None:
    songs = tuple(Song(song_number=n, title=t, artist="") for n, t in enumerate(("고백하기 좋은 날", "좋은 날", "좋은"), 1))
    result = SearchResult(sections=(SearchSection(category="곡 제목", songs=songs),), page=1, total_pages=1)
    ranked = _rank_search_result(result, "좋은", SearchType.TITLE)
    assert [song.title for song in ranked.sections[0].songs] == ["좋은", "좋은 날", "고백하기 좋은 날"]


def test_recent_response_skips_invalid_items_and_sets_badges() -> None:
    items = [{"pro": 12345, "indexTitle": "곡", "indexSong": "가수", "mv_yn": "Y", "icongubun": "MR"}, {"indexTitle": "번호 없음"}]
    songs = _parse_recent_response(json.dumps({"resultCode": "99", "resultData": {"items": items}}))
    assert len(songs) == 1
    assert songs[0].song_number == 12345
    assert songs[0].badge == MediaBadge.MV | MediaBadge.MR


def test_recent_response_rejects_failure_code() -> None:
    with pytest.raises(TJMediaError):
        _parse_recent_response(json.dumps({"resultCode": "00"}))


def test_recent_failure_is_cached_briefly() -> None:
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(500)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            for _ in range(2):
                with pytest.raises(TJMediaError):
                    await client.get_recent_songs()

    asyncio.run(run())
    assert posts == 1


def test_recent_songs_are_parsed_and_cached() -> None:
    posts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal posts
        posts += 1
        return httpx.Response(200, json={"resultCode": "99", "resultData": {"items": [{"pro": 12345, "indexTitle": "곡", "indexSong": "가수"}]}})

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            first = await client.get_recent_songs()
            assert await client.get_recent_songs() is first
            assert [song.song_number for song in first.songs] == [12345]

    asyncio.run(run())
    assert posts == 1


def test_zero_hit_search_skips_refresh_while_session_is_fresh() -> None:
    main_hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal main_hits
        if request.url.path == "/":
            main_hits += 1
            return httpx.Response(200)
        return httpx.Response(200, text='<div class="chart-list-area"></div>')

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            await client.search("123")  # stale session: refresh once and retry
            await client.search("456")  # just refreshed: a genuine miss, no second refresh

    asyncio.run(run())
    assert main_hits == 1


def test_zero_hit_is_not_cached_when_session_refresh_fails() -> None:
    searches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.path == "/":
            return httpx.Response(500)
        searches += 1
        return httpx.Response(200, text='<div class="chart-list-area"></div>')

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            await client.search("123")
            await client.search("123")

    asyncio.run(run())
    assert searches == 2


def test_failed_session_refresh_is_not_retried_immediately() -> None:
    main_hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal main_hits
        if request.url.path == "/":
            main_hits += 1
            return httpx.Response(500)
        return httpx.Response(200, text='<div class="chart-list-area"></div>')

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            await client.search("123")
            await client.search("456")

    asyncio.run(run())
    assert main_hits == 1


def test_zero_hit_romaji_search_falls_back_to_hangul() -> None:
    searched: list[str] = []

    async def run() -> SearchResult:
        async with httpx.AsyncClient(transport=_search_transport(searched)) as http:
            return await TJMediaClient(http=http).search("sakura")

    result = asyncio.run(run())
    assert not result.is_empty
    assert result.matched_query == "사쿠라"


def test_later_pages_skip_the_hangul_fallback() -> None:
    searched: list[str] = []

    async def run() -> SearchResult:
        async with httpx.AsyncClient(transport=_search_transport(searched)) as http:
            return await TJMediaClient(http=http).search("sakura", page=2)

    assert asyncio.run(run()).is_empty
    assert set(searched) == {"sakura"}


def test_concurrent_fetches_are_capped() -> None:
    in_flight = peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, text='<div class="chart-list-area"></div>')

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            await asyncio.gather(*(client.search(f"곡{n}") for n in range(20)))

    asyncio.run(run())
    assert peak <= _MAX_CONCURRENT_FETCHES


def test_failed_searches_do_not_leak_locks() -> None:
    async def run() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))) as http:
            client = TJMediaClient(http=http)
            for n in range(5):
                with pytest.raises(TJMediaError):
                    await client.search(f"곡{n}")
            return len(client._search_locks)  # noqa: SLF001

    assert asyncio.run(run()) == 0


def test_identical_search_is_served_from_cache() -> None:
    searched: list[str] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=_search_transport(searched)) as http:
            client = TJMediaClient(http=http)
            await client.search("사쿠라")
            await client.search("사쿠라")

    asyncio.run(run())
    assert searched == ["사쿠라"]


def test_stale_recent_songs_served_when_refresh_fails() -> None:
    ok = True

    def handler(_request: httpx.Request) -> httpx.Response:
        if not ok:
            return httpx.Response(500)
        return httpx.Response(200, json={"resultCode": "99", "resultData": {"items": [{"pro": 12345, "indexTitle": "곡", "indexSong": "가수"}]}})

    async def run() -> int:
        nonlocal ok
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = TJMediaClient(http=http)
            await client.get_recent_songs()
            assert client._recent is not None  # noqa: SLF001
            client._recent = (client._recent[0], 0.0)  # noqa: SLF001
            ok = False
            return len((await client.get_recent_songs()).songs)

    assert asyncio.run(run()) == 1
