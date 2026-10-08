from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import TYPE_CHECKING, Literal, Self

import httpx
import pydantic

from tj_bot.errors import TJMediaError
from tj_bot.models import ROWS_PER_PAGE, NationType, RecentSong, RecentSongPage, SearchResult, SearchType, compact, strip_spaces
from tj_bot.parser import parse_search_page
from tj_bot.translit import hangul_candidates

if TYPE_CHECKING:
    from types import TracebackType

    from tj_bot.models import Song

log = logging.getLogger(__name__)

# "This month" is anchored to KST — UTC would query the previous month near midnight.
_KST = datetime.timezone(datetime.timedelta(hours=9))
_DEFAULT_TIMEOUT = 15.0
_MAIN_URL = "https://www.tjmedia.com/"
_RECENT_SONGS_TTL = 300.0
# The month's song list is one large response, so it gets far longer than _DEFAULT_TIMEOUT.
_RECENT_SONGS_TIMEOUT = 60.0
# Waiters queued behind a failed fetch would otherwise each sit through another _RECENT_SONGS_TIMEOUT.
_RECENT_FAILURE_TTL = 30.0
# Absorbs page round-trips and repeated identical searches.
_SEARCH_TTL = 45.0
# A zero-hit search this soon after a cookie refresh is a genuine miss, not an expired session.
_SESSION_FRESH = 60.0
# Caps in-flight TJ requests so a burst of searches (each up to ~6 requests) can't get the bot's IP blocked.
_MAX_CONCURRENT_FETCHES = 4
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    # No brotli decoder installed, so don't advertise br or httpx will fail decoding it.
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Referer": _MAIN_URL,
}

type _SearchKey = tuple[str, SearchType, NationType, int]


def _searchable_values(song: Song, kind: SearchType) -> tuple[str, ...]:
    match kind:
        case SearchType.TITLE | SearchType.MEDLEY:
            return (song.title,)
        case SearchType.ARTIST:
            return (song.artist,)
        case SearchType.LYRICIST:
            return (song.lyricist,)
        case SearchType.COMPOSER:
            return (song.composer,)
        case SearchType.SONG_NUMBER:
            return (str(song.song_number),)
        case SearchType.ALL:
            return (song.title, song.artist, song.lyricist, song.composer, str(song.song_number))


def _match_rank(value: str, needle: str) -> tuple[int, int, int]:
    """(tier, position, extra length): exact < prefix < substring < miss, then earlier and tighter matches first."""
    if (position := value.find(needle)) < 0:
        return (3, 0, 0)
    return (0 if value == needle else 1 if position == 0 else 2, position, len(value) - len(needle))


def _score(song: Song, kind: SearchType, needle: str) -> tuple[int, int, int]:
    return min(_match_rank(compact(value), needle) for value in _searchable_values(song, kind))


def _rank_search_result(result: SearchResult, query: str, search_type: SearchType) -> SearchResult:
    """Sort the current page: exact match, then prefix match, then substring match."""
    # ponytail: page-local only, not a global sort.
    needle = compact(query)
    if not needle:
        return result

    # model_copy skips validation, so pass tuples to keep the frozen models free of mutable lists.
    sections = [
        section.model_copy(update={"songs": tuple(sorted(section.songs, key=lambda song: _score(song, section.search_type or search_type, needle)))})
        for section in result.sections
    ]

    if search_type is SearchType.ALL:
        sections.sort(key=lambda section: _score(section.songs[0], section.search_type or SearchType.ALL, needle) if section.songs else (4, 0, 0))
    return result.model_copy(update={"sections": tuple(sections)})


class _RecentData(pydantic.BaseModel):
    # Items are validated one by one so a single bad item is skipped instead of failing the whole month.
    items: list[object]


class _RecentEnvelope(pydantic.BaseModel):
    result_code: Literal["99"] = pydantic.Field(alias="resultCode")
    result_data: _RecentData = pydantic.Field(alias="resultData")


def _parse_recent_item(raw: object) -> RecentSong | None:
    """Validate one recent-songs API item, skipping it if invalid."""
    try:
        return RecentSong.model_validate(raw)
    except pydantic.ValidationError as exc:
        song_number = raw.get("pro") if isinstance(raw, dict) else None
        log.warning("Could not parse recent-song item, skipping (song_number=%r): %s", song_number, exc)
        return None


def _parse_recent_response(text: str) -> list[RecentSong]:
    """Parse the recent-songs API response body. CPU-bound, so only call via `asyncio.to_thread`."""
    try:
        data = _RecentEnvelope.model_validate_json(text).result_data
    except pydantic.ValidationError as exc:
        log.warning("Unexpected recent-songs response: %s", exc)
        raise TJMediaError("최신곡 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.") from exc
    return [song for item in data.items if (song := _parse_recent_item(item)) is not None]


class TJMediaClient:
    def __init__(self, *, http: httpx.AsyncClient | None = None) -> None:
        # The lifetime of an injected test client is managed by its caller.
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=_DEFAULT_TIMEOUT)
        self._fetch_slots = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        self._recent: tuple[RecentSongPage, float] | None = None
        self._recent_lock = asyncio.Lock()
        self._recent_failure: tuple[str, float] | None = None
        # -inf, not 0.0: monotonic() can be under _SESSION_FRESH on a freshly booted host, faking a fresh cookie.
        self._session_at = float("-inf")
        # Separate from _session_at, which means "cookie is fresh"; a failed refresh must not look like a fresh cookie.
        self._refresh_failed_at = float("-inf")
        self._session_lock = asyncio.Lock()
        self._search_cache: dict[_SearchKey, tuple[SearchResult, float]] = {}
        # Per-key so one slow zero-hit query doesn't stall everyone else; still dedupes identical searches.
        self._search_locks: dict[_SearchKey, asyncio.Lock] = {}
        # Entries are dropped when their last user leaves; sweeping unlocked locks could orphan a just-woken waiter and let a duplicate fetch through.
        self._search_users: dict[_SearchKey, int] = {}

    async def __aenter__(self) -> Self:
        await self._refresh_session()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _refresh_session(self) -> bool:
        """Acquire the session cookie that search requires."""
        try:
            async with self._fetch_slots:
                response = await self._http.get(_MAIN_URL)
            response.raise_for_status()
        except httpx.HTTPError:
            log.warning("Failed to acquire session cookie — subsequent searches may return empty results", exc_info=True)
            self._refresh_failed_at = time.monotonic()
            return False
        self._session_at = time.monotonic()
        return True

    async def _renew_session_for(self, started: float) -> bool:
        """Refresh the cookie behind a zero-hit search started at `started`; True means retrying is worthwhile."""
        # Serialized so concurrent zero-hit searches share one refresh instead of each hitting the main page.
        async with self._session_lock:
            if self._session_at > started:
                return True
            # Without the failure backoff, every zero-hit search would re-queue a slow refresh behind the lock while TJ's main page is down.
            if time.monotonic() - max(self._session_at, self._refresh_failed_at) < _SESSION_FRESH:
                return False
            log.info("Search returned empty, refreshing session cookie and retrying once")
            return await self._refresh_session()

    async def _fetch(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,  # noqa: ASYNC109 — let httpx manage its own timeout
    ) -> httpx.Response:
        try:
            async with self._fetch_slots:
                response = await self._http.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TJMediaError("TJ 미디어 응답이 늦어요. 잠시 후 다시 시도해 주세요.") from exc
        except httpx.HTTPStatusError as exc:
            raise TJMediaError("TJ 미디어가 요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요.") from exc
        except httpx.HTTPError as exc:
            raise TJMediaError("TJ 미디어에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.") from exc
        return response

    async def _fetch_search(self, params: dict[str, str], query: str, search_type: SearchType, page: int) -> SearchResult:
        response = await self._fetch("GET", f"{_MAIN_URL}song/accompaniment_search", params=params)
        # BeautifulSoup parsing is CPU-bound, so run it off the event loop.
        result = await asyncio.to_thread(parse_search_page, response.text, page)
        return _rank_search_result(result, query, search_type)

    def _cached_search(self, key: _SearchKey) -> SearchResult | None:
        cached = self._search_cache.get(key)
        return cached[0] if cached is not None and cached[1] > time.monotonic() else None

    async def _search_once(
        self, query: str, *, search_type: SearchType = SearchType.ALL, nation: NationType = NationType.ALL, page: int = 1, _retry: bool = False
    ) -> SearchResult:
        search_txt = strip_spaces(query)  # TJ ignores whitespace
        # _retry doesn't change the response content, so it's excluded from the cache key.
        cache_key = (search_txt, search_type, nation, page)
        if (cached := self._cached_search(cache_key)) is not None:
            return cached

        lock = self._search_locks.setdefault(cache_key, asyncio.Lock())
        self._search_users[cache_key] = self._search_users.get(cache_key, 0) + 1
        try:
            async with lock:
                return await self._search_locked(cache_key, query, search_type, nation, page, retry=_retry)
        finally:
            if (users := self._search_users[cache_key] - 1) > 0:
                self._search_users[cache_key] = users
            else:  # also on an outage (every fetch raising), so distinct queries can't pile up locks
                del self._search_users[cache_key], self._search_locks[cache_key]

    async def _search_locked(
        self, cache_key: _SearchKey, query: str, search_type: SearchType, nation: NationType, page: int, *, retry: bool
    ) -> SearchResult:
        if (cached := self._cached_search(cache_key)) is not None:
            return cached

        params = {
            "pageNo": str(page),
            "pageRowCnt": str(ROWS_PER_PAGE),
            # TJ has no relevance sort; re-sort the page ourselves after fetching.
            "strSotrGubun": "ASC",
            "strSortType": "",
            "nationType": str(nation),
            "strType": str(search_type),
            "searchTxt": cache_key[0],
        }
        started = time.monotonic()
        result = await self._fetch_search(params, query, search_type, page)
        # An expired session cookie makes TJ return empty instead of erroring; retry once.
        if result.is_empty and not retry and await self._renew_session_for(started):
            result = await self._fetch_search(params, query, search_type, page)

        now = time.monotonic()
        self._search_cache = {k: v for k, v in self._search_cache.items() if v[1] > now}  # ponytail: full sweep, switch to LRU if volume grows
        # Empty under a cookie we couldn't refresh may be the stale session talking, so don't pin it for _SEARCH_TTL.
        if not result.is_empty or now - self._session_at < _SESSION_FRESH:
            self._search_cache[cache_key] = (result, now + _SEARCH_TTL)
        return result

    async def search(
        self, query: str, *, search_type: SearchType = SearchType.ALL, nation: NationType = NationType.ALL, page: int = 1
    ) -> SearchResult:
        """Search for a song, retrying a zero-result romaji/kana query as a Hangul transliteration."""
        result = await self._search_once(query, search_type=search_type, nation=nation, page=page)
        # Page 2 onward always arrives with an already-resolved query, so no fallback is needed.
        if not result.is_empty or page != 1:
            return result

        for candidate in hangul_candidates(query):
            alternative = await self._search_once(candidate, search_type=search_type, nation=nation, page=page, _retry=True)
            if not alternative.is_empty:
                log.info("Swapped romaji/kana query %r for Hangul transliteration %r and found results", query, candidate)
                return alternative.model_copy(update={"matched_query": candidate})

        return result

    async def get_recent_songs(self) -> RecentSongPage:
        """Fetch this month's (KST) recent songs."""
        async with self._recent_lock:  # month can roll over while waiting, so compute time after
            now = datetime.datetime.now(tz=_KST)
            if self._recent is not None:
                cached, expires_at = self._recent
                if (cached.year, cached.month) == (now.year, now.month) and expires_at > time.monotonic():
                    return cached  # frozen, so safe to hand out as-is
            if self._recent_failure is not None and self._recent_failure[1] > time.monotonic():
                if self._recent is not None and (self._recent[0].year, self._recent[0].month) == (now.year, now.month):
                    return self._recent[0]
                raise TJMediaError(self._recent_failure[0])

            try:
                songs = await self._fetch_recent_songs(f"{now.year:04d}{now.month:02d}")
            except TJMediaError as exc:
                self._recent_failure = (str(exc), time.monotonic() + _RECENT_FAILURE_TTL)
                # An expired list from this month still beats an error while TJ is down.
                if self._recent is not None and (self._recent[0].year, self._recent[0].month) == (now.year, now.month):
                    return self._recent[0]
                raise
            page = RecentSongPage(year=now.year, month=now.month, songs=tuple(songs))
            self._recent = (page, time.monotonic() + _RECENT_SONGS_TTL)
            return page

    async def _fetch_recent_songs(self, search_ym: str) -> list[RecentSong]:
        response = await self._fetch(
            "POST",
            f"{_MAIN_URL}legacy/api/newSongOfMonth",
            data={"searchYm": search_ym},
            headers={"Referer": f"{_MAIN_URL}song/recent_song"},
            timeout=_RECENT_SONGS_TIMEOUT,
        )
        # Decode and validate the large response's JSON off the event loop.
        return await asyncio.to_thread(_parse_recent_response, response.text)
