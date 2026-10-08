from __future__ import annotations

import functools
import logging
import operator
import re

from bs4 import BeautifulSoup, Tag

from tj_bot.errors import TJMediaError
from tj_bot.models import ROWS_PER_PAGE, MediaBadge, SearchResult, SearchSection, SearchType, Song

log = logging.getLogger(__name__)

# A category block. A combined search returns several; a single-type search returns one.
# Must be a partial class match — a regular search block is `music`, a medley search is `medley`.
_BLOCK_SELECTOR = ".chart-list-area"

_BADGE_CLASSES: dict[str, MediaBadge] = {"mv": MediaBadge.MV, "mr": MediaBadge.MR, "medley": MediaBadge.MEDLEY}

# TJ's own heading wording, matched exactly.
_HEADINGS: dict[str, SearchType] = {
    "곡 제목": SearchType.TITLE,
    "가수": SearchType.ARTIST,
    "작사가": SearchType.LYRICIST,
    "작곡가": SearchType.COMPOSER,
    "곡 번호": SearchType.SONG_NUMBER,
    "메들리": SearchType.MEDLEY,
}

_SONG_NUMBER_DIGITS = 9

_PAGINATION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    # get_text(strip=True) strips each fragment, eating the spaces around TJ's highlight spans.
    return " ".join(el.get_text().split())


def _badges_of(els: list[Tag]) -> MediaBadge:
    """Some songs carry several icons side by side (e.g. MV and MR) — looking at only the first misses the rest."""
    classes = [cls for el in els for cls in el.get("class") or []]
    return functools.reduce(operator.or_, (badge for cls, badge in _BADGE_CLASSES.items() if cls in classes), MediaBadge.NONE)


def _song_number(row: Tag) -> int | None:
    num_text = _text(row.select_one("li.grid-item.center.pos-type span.num2"))
    # The length cap keeps int() clear of Python's digit limit on hostile input; real song numbers are far shorter.
    return int(num_text) if num_text.isdecimal() and len(num_text) <= _SONG_NUMBER_DIGITS else None


def _parse_music_row(row: Tag) -> Song | None:
    number = _song_number(row)
    if number is None:
        return None

    return Song(
        song_number=number,
        title=_text(row.select_one("li.grid-item.title3 div.flex-box > p")),
        artist=_text(row.select_one("li.grid-item.title4.singer p span")),
        lyricist=_text(row.select_one("li.grid-item.title5 p span")),
        composer=_text(row.select_one("li.grid-item.title6 p span")),
        badge=_badges_of(row.select("li.grid-item.title3 ul li p")),
    )


def _parse_medley_row(row: Tag) -> Song | None:
    """A medley block's row has a different structure from a regular song row.

    It's made up of the song number + medley name (`span.medley-str`) + list of
    performing artists (`li.grid-item.title`).
    """
    number = _song_number(row)
    if number is None:
        return None

    return Song(
        song_number=number,
        title=_text(row.select_one("li.grid-item.group span.medley-str")),
        artist=_text(row.select_one("li.grid-item.title")),
        badge=_badges_of(row.select("li.grid-item.group p.ico")),
    )


def _parse_block(block: Tag) -> SearchSection:
    is_medley = "medley" in (block.get("class") or [])
    parse_row = _parse_medley_row if is_medley else _parse_music_row

    heading = block.find_previous_sibling(class_="title-select-area")
    category = _text(heading.select_one("h2")) if isinstance(heading, Tag) else ""
    search_type = _HEADINGS.get(category)
    if search_type is None:
        log.warning("Unknown category heading (TJ wording may have changed): %r", category)

    songs = tuple(song for row in block.select(".grid-container.list.ico") if (song := parse_row(row)) is not None)
    return SearchSection(category=category, songs=songs, search_type=search_type)


def _parse_pagination(soup: BeautifulSoup, page: int) -> tuple[int, int]:
    # Without a pager, trust the requested page so "previous" stays reachable; a combined search is always page 1.
    mo_pag = soup.find(class_="mo-pagenation-wrap")
    if not isinstance(mo_pag, Tag) or not (m := _PAGINATION_RE.search(mo_pag.get_text())):
        return page, page
    # Guard against parsing 0 when the search has no results.
    return max(1, int(m.group(1))), max(1, int(m.group(2)))


def parse_search_page(html: str, page: int = 1) -> SearchResult:
    soup = BeautifulSoup(html, "html.parser")

    blocks = soup.select(_BLOCK_SELECTOR)
    if not blocks:
        log.warning("Could not find %s block in search response", _BLOCK_SELECTOR)
        raise TJMediaError("검색 결과를 불러오지 못했어요. 잠시 후 다시 시도해 주세요.")

    # Empty category blocks always show up mixed into a combined search, so drop them.
    sections = tuple(s for block in blocks if (s := _parse_block(block)).songs)
    current_page, total_pages = _parse_pagination(soup, page)
    # A combined search legitimately has no pager, but a single full page without one means paging would silently vanish.
    if len(sections) == 1 and len(sections[0].songs) >= ROWS_PER_PAGE and not soup.find(class_="mo-pagenation-wrap"):
        log.warning("Full page of results without a pager (TJ markup may have changed)")
    return SearchResult(sections=sections, page=current_page, total_pages=total_pages)
