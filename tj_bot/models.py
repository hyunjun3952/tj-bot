from __future__ import annotations

from enum import Flag, StrEnum, auto

import pydantic

# Shared by search (server page size) and recent songs (client page width).
# Careful raising this: more rows per embed can hit Discord's 6000-char total limit.
ROWS_PER_PAGE = 10


def strip_spaces(text: str) -> str:
    """Drop every kind of whitespace, incl. the U+3000 a Japanese IME types."""
    return "".join(text.split())


def compact(text: str) -> str:
    """Whitespace- and case-insensitive form, for matching."""
    return strip_spaces(text).casefold()


_CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_SYLLABLES_PER_CHOSEONG = 21 * 28  # jungseong * jongseong combinations that share one choseong


def is_choseong_query(text: str) -> bool:
    """True for input made only of initial consonants (ㅇㅇㅇ), which only makes sense matched against `choseong` forms."""
    return bool(text) and all(char in _CHOSEONG for char in text)


def choseong(text: str) -> str:
    """The compact form with each Hangul syllable reduced to its initial consonant (아이유 -> ㅇㅇㅇ)."""
    return "".join(_CHOSEONG[(ord(char) - 0xAC00) // _SYLLABLES_PER_CHOSEONG] if "가" <= char <= "힣" else char for char in compact(text))


class SearchType(StrEnum):
    ALL = "0"
    TITLE = "1"
    ARTIST = "2"
    LYRICIST = "4"
    COMPOSER = "8"
    SONG_NUMBER = "16"
    MEDLEY = "32"

    @property
    def label(self) -> str:
        return _SEARCH_TYPE_LABELS[self]


class NationType(StrEnum):
    ALL = ""
    KOREAN = "KOR"
    POP = "ENG"
    JAPANESE = "JPN"

    @property
    def label(self) -> str:
        return _NATION_LABELS[self]


_SEARCH_TYPE_LABELS: dict[SearchType, str] = {
    SearchType.ALL: "통합검색",
    SearchType.TITLE: "곡제목",
    SearchType.ARTIST: "가수명",
    SearchType.LYRICIST: "작사가",
    SearchType.COMPOSER: "작곡가",
    SearchType.SONG_NUMBER: "곡번호",
    SearchType.MEDLEY: "메들리",
}

_NATION_LABELS: dict[NationType, str] = {NationType.ALL: "전체", NationType.KOREAN: "가요", NationType.POP: "팝송", NationType.JAPANESE: "일본곡"}


class MediaBadge(Flag):
    """Media markers on a song. Flag, not StrEnum — a song can carry both MV and MR."""

    NONE = 0
    MV = auto()
    MR = auto()
    MEDLEY = auto()


class Song(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    song_number: int
    title: str
    artist: str
    lyricist: str = ""
    composer: str = ""
    badge: MediaBadge = MediaBadge.NONE


class SearchSection(pydantic.BaseModel):
    """One category block; a combined search returns up to 6 (empty ones are dropped), a single-type search returns 1."""

    model_config = pydantic.ConfigDict(frozen=True)

    category: str
    songs: tuple[Song, ...]
    # Which search type isolates this block on its own. None if the heading was unrecognized.
    search_type: SearchType | None = None


class SearchResult(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    sections: tuple[SearchSection, ...]
    page: int
    total_pages: int

    # The Hangul transliteration that hit, if the original query was retried; shown as "실제 검색어" and reused for paging.
    matched_query: str = ""

    @property
    def is_empty(self) -> bool:
        return all(not section.songs for section in self.sections)


class RecentSong(pydantic.BaseModel):
    """One item of the recent-songs API response; validated since it's untrusted external JSON."""

    model_config = pydantic.ConfigDict(frozen=True)

    song_number: int = pydantic.Field(validation_alias="pro", gt=0)  # TJ song numbers are always positive
    title: str = pydantic.Field(validation_alias="indexTitle", default="")
    artist: str = pydantic.Field(validation_alias="indexSong", default="")
    publish_date: str = pydantic.Field(validation_alias="publishdate", default="")
    badge: MediaBadge = MediaBadge.NONE

    @pydantic.model_validator(mode="before")
    @classmethod
    def _badge_from_api(cls, raw: object) -> object:
        """The API reports MV and MR as separate string flags."""
        if not isinstance(raw, dict):
            return raw
        mv = MediaBadge.MV if str(raw.get("mv_yn", "")).upper() == "Y" else MediaBadge.NONE
        mr = MediaBadge.MR if str(raw.get("icongubun", "")).upper() == "MR" else MediaBadge.NONE
        return {**raw, "badge": mv | mr}

    @pydantic.field_validator("title", "artist", "publish_date", mode="before")
    @classmethod
    def _blank_when_not_str(cls, value: object) -> object:
        """A type mismatch on these optional fields blanks the value instead of failing."""
        return value if isinstance(value, str) else ""


class RecentSongPage(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    year: int
    month: int
    songs: tuple[RecentSong, ...]
    # True when this month had nothing yet and last month's list stands in.
    is_fallback: bool = False
