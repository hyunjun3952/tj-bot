import pytest

from tj_bot.errors import TJMediaError
from tj_bot.models import MediaBadge, SearchType
from tj_bot.parser import parse_search_page


def test_parse_search_page_raises_without_result_block() -> None:
    with pytest.raises(TJMediaError):
        parse_search_page("<html><body>no results here</body></html>")


def test_parse_search_page_extracts_song_fields() -> None:
    html = """
<div class="title-select-area"><h2>곡 제목</h2></div>
<div class="chart-list-area">
  <div class="grid-container list ico">
    <li class="grid-item center pos-type"><span class="num2">12345</span></li>
    <li class="grid-item title3"><div class="flex-box"><p>테스트곡</p></div></li>
    <li class="grid-item title4 singer"><p><span>테스트가수</span></p></li>
  </div>
</div>
"""
    result = parse_search_page(html)
    assert len(result.sections) == 1
    song = result.sections[0].songs[0]
    assert song.song_number == 12345
    assert song.title == "테스트곡"
    assert song.artist == "테스트가수"
    # No pager in the HTML: keep the requested page rather than snapping back to 1.
    assert parse_search_page(html, page=3).page == 3


def test_highlighted_title_keeps_spaces() -> None:
    html = """
<div class="title-select-area"><h2>곡 제목</h2></div>
<div class="chart-list-area">
  <div class="grid-container list ico">
    <li class="grid-item center pos-type"><span class="num2">17837</span></li>
    <li class="grid-item title3"><div class="flex-box"><p><span>고백하기 <span class='highlight'>좋은</span> 날</span></p></div></li>
  </div>
</div>
"""
    assert parse_search_page(html).sections[0].songs[0].title == "고백하기 좋은 날"


def test_medley_block_is_parsed() -> None:
    html = """
<div class="title-select-area"><h2>메들리</h2></div>
<div class="chart-list-area medley">
  <div class="grid-container list ico">
    <li class="grid-item center pos-type"><span class="num2">98765</span></li>
    <li class="grid-item group"><span class="medley-str">메들리곡</span><p class="ico medley"></p></li>
    <li class="grid-item title">가수A, 가수B</li>
  </div>
</div>
"""
    section = parse_search_page(html).sections[0]
    assert section.search_type is SearchType.MEDLEY
    song = section.songs[0]
    assert (song.song_number, song.title, song.artist, song.badge) == (98765, "메들리곡", "가수A, 가수B", MediaBadge.MEDLEY)


def test_pager_sets_page_and_total() -> None:
    block = '<div class="chart-list-area"></div>'
    result = parse_search_page(block + '<div class="mo-pagenation-wrap">2 / 5</div>')
    assert (result.page, result.total_pages) == (2, 5)
    zero = parse_search_page(block + '<div class="mo-pagenation-wrap">0 / 0</div>')
    assert (zero.page, zero.total_pages) == (1, 1)


def test_full_page_without_pager_warns(caplog: pytest.LogCaptureFixture) -> None:
    row = '<div class="grid-container list ico"><li class="grid-item center pos-type"><span class="num2">{n}</span></li></div>'
    block = '<div class="title-select-area"><h2>곡 제목</h2></div><div class="chart-list-area">{rows}</div>'

    def html(count: int, pager: str = "") -> str:
        return block.replace("{rows}", "".join(row.replace("{n}", str(n)) for n in range(1, count + 1))) + pager

    parse_search_page(html(9))
    parse_search_page(html(10, '<div class="mo-pagenation-wrap">1 / 2</div>'))
    assert not caplog.records
    parse_search_page(html(10))
    assert "without a pager" in caplog.text


def test_song_row_collects_every_badge() -> None:
    row = """
<div class="title-select-area"><h2>곡 제목</h2></div>
<div class="chart-list-area">
  <div class="grid-container list ico">
    <li class="grid-item center pos-type"><span class="num2">1</span></li>
    <li class="grid-item title3"><div class="flex-box"><p>곡</p></div><ul><li><p class="mv"></p></li><li><p class="mr"></p></li></ul></li>
  </div>
</div>
"""
    song = parse_search_page(row).sections[0].songs[0]
    assert song.badge == MediaBadge.MV | MediaBadge.MR


def test_oversized_song_number_is_skipped() -> None:
    html = (
        '<div class="chart-list-area"><div class="grid-container list ico"><li class="grid-item center pos-type"><span class="num2">'
        + "9" * 5000
        + "</span></li></div></div>"
    )
    assert parse_search_page(html).is_empty
