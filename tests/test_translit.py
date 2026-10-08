from tj_bot.translit import hangul_candidates


def test_lone_long_vowel_mark_does_not_crash() -> None:
    assert hangul_candidates("ー") == []


def test_long_vowel_mark_before_nasal_does_not_crash() -> None:
    # Regression: "ー" maps to "" in the mora table, and _assemble used to call
    # ord("") when the following "ん" tried to attach a jongseong to it.
    hangul_candidates("ーん")
    hangul_candidates("ヴーン")


def test_romaji_query_yields_hangul_transliteration() -> None:
    assert "사쿠라" in hangul_candidates("sakura")


def test_kana_query_yields_hangul_transliteration() -> None:
    assert "사쿠라" in hangul_candidates("サクラ")


def test_shortened_ou_form_is_not_starved_by_budget() -> None:
    assert "토쿄" in hangul_candidates("toukyou")


def test_loanword_small_vowel_moras_are_transliterated() -> None:
    assert "파티" in hangul_candidates("パーティー")


def test_english_query_yields_no_transliteration() -> None:
    # Unmatched letters used to be dropped, turning "love story" into 오베토 and wasting TJ requests.
    for query in ("hello", "the beatles", "love story", "don't"):
        assert hangul_candidates(query) == []


def test_hepburn_m_before_labial_is_nasal() -> None:
    assert "신분" in hangul_candidates("shimbun")
    assert hangul_candidates("yoasobi")[0] == "요아소비"


def test_kana_long_vowel_gets_the_shortened_spelling_too() -> None:
    assert "토쿄" in hangul_candidates("とうきょう")
    assert "오키나" in hangul_candidates("おおきな")
    assert hangul_candidates("とうきょう")[0] == "토우쿄우"


def test_typographic_apostrophe_marks_the_n_boundary() -> None:
    assert hangul_candidates("shin\u2019ichi") == hangul_candidates("shin'ichi") != []


def test_fullwidth_and_halfwidth_input_is_normalized() -> None:
    fullwidth = "".join(chr(ord(char) + 0xFEE0) for char in "YOASOBI")
    assert hangul_candidates(fullwidth) == hangul_candidates("yoasobi") != []
    assert hangul_candidates("ｻｸﾗ") == hangul_candidates("サクラ") != []


def test_kunrei_romaji_is_accepted() -> None:
    assert "쇼" in hangul_candidates("syo")
    assert "초토" in hangul_candidates("tyoto")
    assert "자" in hangul_candidates("dya")
    assert hangul_candidates("jya") == hangul_candidates("ja")


def test_doubled_m_is_a_nasal_not_a_sokuon() -> None:
    assert "칸무리" in hangul_candidates("kammuri") or "캄무리" in hangul_candidates("kammuri")
    assert all("캇" not in candidate for candidate in hangul_candidates("kammuri"))


def test_each_ou_is_read_independently() -> None:
    # The first ou is a word boundary and the second a long vowel; an all-or-nothing reading can't produce this.
    assert "소우모" in hangul_candidates("soumou", limit=8)


def test_vowel_collapse_does_not_cross_word_boundaries() -> None:
    assert hangul_candidates("kai ie") != hangul_candidates("kaiie")
    assert "카이이에" in hangul_candidates("kai ie")


def test_unattachable_nasal_or_sokuon_yields_nothing() -> None:
    assert hangul_candidates("nasa") != []
    assert hangul_candidates("n") == []
    assert hangul_candidates("ッカ") == []
    assert hangul_candidates("こんんにちは") == []


def test_ime_style_small_vowel_romaji_is_accepted() -> None:
    assert "파티" in hangul_candidates("paathi")
    assert "디" in hangul_candidates("dhi")
    assert "투" in hangul_candidates("twu")


def test_kana_particles_get_their_spoken_reading() -> None:
    assert "콘니치와" in hangul_candidates("こんにちは")
    assert "키미에" in hangul_candidates("きみへ")
    assert "키미노나와" in hangul_candidates("きみのなは")
    assert "하나" in hangul_candidates("はな")  # a word-initial は is never a particle


def test_macron_and_circumflex_long_vowels_are_accepted() -> None:
    assert "토쿄" in hangul_candidates("Tōkyō")
    assert hangul_candidates("Tôkyô") == hangul_candidates("Tōkyō")
    assert "오사카" in hangul_candidates("Ōsaka")


def test_loanword_moras_without_a_hepburn_spelling_are_accepted() -> None:
    assert "차" in hangul_candidates("tsa")
    assert "콰" in hangul_candidates("kwa")
    assert "차" in hangul_candidates("ツァ")
    assert "퓨" in hangul_candidates("フュ")
    assert "지" in hangul_candidates("ズィ")


def test_punctuation_is_ignored_and_digits_pass_through() -> None:
    assert hangul_candidates("sakura!") == hangul_candidates("sakura")
    assert "사쿠라2" in hangul_candidates("sakura 2")
    assert hangul_candidates("2024") == []
    assert hangul_candidates("!!!") == []


def test_ime_nn_is_a_single_nasal_before_a_consonant() -> None:
    assert "센세이" in hangul_candidates("sennsei")
    assert "콘니치와" in hangul_candidates("konnichiwa")
    assert "콘니치와" in hangul_candidates("konnnichiwa")
    assert "온나" in hangul_candidates("onna")
