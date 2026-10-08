"""Turn a romaji/kana query into Hangul-transliteration candidates that TJ has indexed.

TJ matches Japanese titles and their Hangul transliteration but never romaji, so a
zero-result query gets retried with candidates from here. TJ's Hangul spellings are
hand-entered and inconsistent, so ambiguous spots keep several candidates and try each.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# Assembly markers for moras that attach to the previous syllable as a jongseong.
_SOKUON = "\x00"
_N_NIEUN = "\x01"
_N_MIEUM = "\x02"
_N_IEUNG = "\x03"

# Hangul syllable = 0xAC00 + (choseong*21 + jungseong)*28 + jongseong.
_HANGUL_BASE = 0xAC00
_HANGUL_COUNT = 11172
_JONGSEONG_COUNT = 28
_JONGSEONG_INDEX = {_N_NIEUN: 4, _N_MIEUM: 16, _SOKUON: 19, _N_IEUNG: 21}

_ROMAJI_TO_KANA = {
    "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
    "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
    "sa": "さ", "shi": "し", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "za": "ざ", "ji": "じ", "zi": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
    "ta": "た", "chi": "ち", "ti": "ち", "tsu": "つ", "tu": "つ", "te": "て", "to": "と",
    "da": "だ", "di": "ぢ", "du": "づ", "de": "で", "do": "ど",
    "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
    "ha": "は", "hi": "ひ", "fu": "ふ", "hu": "ふ", "he": "へ", "ho": "ほ",
    "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
    "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
    "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
    "ya": "や", "yu": "ゆ", "yo": "よ",
    "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wo": "を", "n": "ん",
    "kya": "きゃ", "kyu": "きゅ", "kyo": "きょ",
    "gya": "ぎゃ", "gyu": "ぎゅ", "gyo": "ぎょ",
    "sha": "しゃ", "shu": "しゅ", "sho": "しょ",
    "ja": "じゃ", "ju": "じゅ", "jo": "じょ",
    "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ",
    "nya": "にゃ", "nyu": "にゅ", "nyo": "にょ",
    "hya": "ひゃ", "hyu": "ひゅ", "hyo": "ひょ",
    "bya": "びゃ", "byu": "びゅ", "byo": "びょ",
    "pya": "ぴゃ", "pyu": "ぴゅ", "pyo": "ぴょ",
    "mya": "みゃ", "myu": "みゅ", "myo": "みょ",
    "rya": "りゃ", "ryu": "りゅ", "ryo": "りょ",
    # Loanword moras, so romaji like `faito` still converts instead of being rejected as non-romaji.
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
    "va": "ゔぁ", "vi": "ゔぃ", "vu": "ゔ", "ve": "ゔぇ", "vo": "ゔぉ",
    "she": "しぇ", "je": "じぇ", "che": "ちぇ", "ye": "いぇ",
    # Kunrei-style spellings (tyotto, syasin) that Hepburn-only input would reject outright.
    "sya": "しゃ", "syu": "しゅ", "syo": "しょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ",
    "zya": "じゃ", "zyu": "じゅ", "zyo": "じょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
    "dya": "ぢゃ", "dyu": "ぢゅ", "dyo": "ぢょ",
    "wi": "うぃ", "we": "うぇ", "who": "うぉ",
    "tsa": "つぁ", "tsi": "つぃ", "tse": "つぇ", "tso": "つぉ",
    "kwa": "くぁ", "kwi": "くぃ", "kwe": "くぇ", "kwo": "くぉ",
    "gwa": "ぐぁ", "gwi": "ぐぃ", "gwe": "ぐぇ", "gwo": "ぐぉ",
    "fyu": "ふゅ", "vyu": "ゔゅ",
    # IME-style spellings for the small-vowel moras that plain `ti`/`di`/`tu`/`du` can't reach.
    "thi": "てぃ", "dhi": "でぃ", "twu": "とぅ", "dwu": "どぅ", "dhu": "でゅ",
    # Marks a ん boundary (shin'ichi = しんいち); dropping it would merge into `ni`.
    "n'": "ん",
}  # fmt: skip

# Parsing tries the longest window first, or "shi" would split into "si"+"hi".
_MAX_ROMAJI_LEN = max(map(len, _ROMAJI_TO_KANA))

# A tuple value marks a spot where TJ's spelling genuinely varies; the first is the default.
_KANA_TO_HANGUL: dict[str, str | tuple[str, ...]] = {
    "あ": "아", "い": "이", "う": "우", "え": "에", "お": "오",
    "か": ("카", "가"), "き": ("키", "기"), "く": ("쿠", "구"),
    "け": ("케", "게"), "こ": ("코", "고"),
    "が": "가", "ぎ": "기", "ぐ": "구", "げ": "게", "ご": "고",
    "さ": "사", "し": "시", "す": ("스", "수"), "せ": "세", "そ": "소",
    "ざ": "자", "じ": "지", "ず": "즈", "ぜ": "제", "ぞ": "조",
    "た": ("타", "다"), "ち": ("치", "지"), "つ": ("츠", "쓰"),
    "て": ("테", "데"), "と": ("토", "도"),
    "だ": "다", "ぢ": "지", "づ": "즈", "で": "데", "ど": "도",
    "な": "나", "に": "니", "ぬ": "누", "ね": "네", "の": "노",
    "は": "하", "ひ": "히", "ふ": "후", "へ": "헤", "ほ": "호",
    "ば": "바", "び": "비", "ぶ": "부", "べ": "베", "ぼ": "보",
    "ぱ": "파", "ぴ": "피", "ぷ": "푸", "ぺ": "페", "ぽ": "포",
    "ま": "마", "み": "미", "む": "무", "め": "메", "も": "모",
    "や": "야", "ゆ": "유", "よ": "요",
    "ら": "라", "り": "리", "る": "루", "れ": "레", "ろ": "로",
    "わ": "와", "を": "오",
    "きゃ": ("캬", "갸"), "きゅ": ("큐", "규"), "きょ": ("쿄", "교"),
    "ぎゃ": "갸", "ぎゅ": "규", "ぎょ": "교",
    "しゃ": "샤", "しゅ": "슈", "しょ": "쇼",
    "じゃ": ("자", "쟈"), "じゅ": ("주", "쥬"), "じょ": ("조", "죠"),
    "ぢゃ": "자", "ぢゅ": "주", "ぢょ": "조",
    "ちゃ": ("차", "쟈"), "ちゅ": ("추", "츄"), "ちょ": ("초", "쵸"),
    "にゃ": "냐", "にゅ": "뉴", "にょ": "뇨",
    "ひゃ": "햐", "ひゅ": "휴", "ひょ": "효",
    "びゃ": "뱌", "びゅ": "뷰", "びょ": "뵤",
    "ぴゃ": "퍄", "ぴゅ": "퓨", "ぴょ": "표",
    "みゃ": "먀", "みゅ": "뮤", "みょ": "묘",
    "りゃ": "랴", "りゅ": "류", "りょ": "료",
    "ふぁ": "파", "ふぃ": "피", "ふぇ": "페", "ふぉ": "포",
    "ゔ": "부", "ゔぁ": "바", "ゔぃ": "비", "ゔぇ": "베", "ゔぉ": "보",
    "しぇ": ("셰", "세"), "じぇ": "제", "ちぇ": "체",
    "うぃ": "위", "うぇ": "웨", "うぉ": "워", "いぇ": "예",
    "てぃ": "티", "でぃ": "디", "とぅ": "투", "どぅ": "두", "でゅ": "듀",
    "つぁ": "차", "つぃ": "치", "つぇ": "체", "つぉ": "초",
    "くぁ": "콰", "くぃ": "퀴", "くぇ": "퀘", "くぉ": "쿼",
    "ぐぁ": "과", "ぐぃ": "귀", "ぐぇ": "궤", "ぐぉ": "궈",
    "ふゅ": "퓨", "ゔゅ": "뷰", "ずぃ": "지", "すぃ": "시",
    "っ": _SOKUON,
    "ん": (_N_NIEUN, _N_MIEUM, _N_IEUNG),
    "ー": "",
}  # fmt: skip

# Katakana sits 0x60 codepoints above hiragana, so derive it; writing it out by hand again would drift.
_KANA_TO_HANGUL.update(
    {"".join(chr(ord(char) + 0x60) for char in mora): value for mora, value in _KANA_TO_HANGUL.items() if all("ぁ" <= char <= "ゖ" for char in mora)}
)

_DIGITS = "0123456789"
# Digits pass through untouched ("kimi no na wa 2"), so they need a table entry to not read as unmapped kana.
_KANA_TO_HANGUL.update({digit: digit for digit in _DIGITS})

_MAX_MORA_LEN = 2
# Each `ou` doubles the forms to try, so past this many only the all-or-nothing readings are kept.
_MAX_OU_SEAMS = 3
_PRIMARY_FORMS = 2

_ROMAJI_PATTERN = re.compile(r"[A-Za-z0-9 '\-]+")
# Skip conversion if kanji is mixed in — TJ already finds that kind of input as-is.
_KANA_ONLY_PATTERN = re.compile(r"[ぁ-ゖァ-ヺー・\s0-9]+")
# A query of only digits or punctuation has nothing to transliterate and would just repeat the original search.
_HAS_LETTER_PATTERN = re.compile(r"[A-Za-z]")
_HAS_KANA_PATTERN = re.compile(r"[ぁ-ゖァ-ヺ]")
_PUNCTUATION_PATTERN = re.compile(r"[!?.,:;~…\"()\[\]{}&/*+。、〜「」『』]")
# NFKC leaves typographic apostrophes (mobile keyboards) and macron/circumflex long vowels alone. ō is ambiguous between おう and おお, so it
# becomes `ou`, which the `ou` handling tries both ways.
_FOLD = str.maketrans(
    {ord(char): "'" for char in "\u2019\u2018\u02bc`\u00b4"}
    | {ord(long): short for longs, short in (("āâĀÂ", "aa"), ("īîĪÎ", "ii"), ("ūûŪÛ", "uu"), ("ēêĒÊ", "ee"), ("ōôŌÔ", "ou")) for long in longs}
)
# A う/お after an o-row kana is usually a long vowel that TJ's Hangul spelling drops (とうきょう = 토쿄).
_LONG_O_PATTERN = re.compile(r"(?<=[おこそとのほもよろをごぞどぼぽょオコソトノホモヨロヲゴゾドボポョ])[うおウオ]")


def _romaji_to_kana(text: str) -> str:
    """Return "" when a letter can't be read as romaji, so English queries yield no candidates."""
    kana: list[str] = []
    index = 0
    while index < len(text):
        if text[index] in _DIGITS:
            kana.append(text[index])
            index += 1
            continue
        # IME-style `nn` is a single ん when no vowel or y follows (sennsei); before one it's n + a na-row mora (konnichiwa).
        if text.startswith("nn", index) and text[index + 2 : index + 3] not in tuple("aiueoy"):
            kana.append("ん")
            index += 2
            continue
        for size in range(_MAX_ROMAJI_LEN, 0, -1):
            if (key := text[index : index + size]) in _ROMAJI_TO_KANA:
                kana.append(_ROMAJI_TO_KANA[key])
                index += len(key)
                break
        else:
            char, following = text[index], text[index + 1 : index + 2]
            # Hepburn writes ん as `m` before labials (shimbun, kammuri, tempura); this must win over the doubled-`m` sokuon below.
            if char == "m" and following in ("b", "m", "p"):
                kana.append("ん")
            # A doubled consonant is a sokuon (happi -> はっぴ); ち uses `t` instead (matcha).
            elif char not in "aiueo" and (following == char or (char == "t" and text.startswith("ch", index + 1))):
                kana.append("っ")
            else:
                return ""
            index += 1
    return "".join(kana)


def _ou_forms(text: str) -> list[str]:
    """Every long-vowel (`o`) / word-boundary (`ou`) reading of each `ou`, with all-`ou` and all-`o` first since they're the likeliest."""
    parts = text.split("ou")
    if len(parts) == 1:
        return [text]
    seams = len(parts) - 1
    combos = [("ou",) * seams, ("o",) * seams]
    if seams <= _MAX_OU_SEAMS:
        combos += [c for c in itertools.product(("ou", "o"), repeat=seams) if c not in combos]
    return list(dict.fromkeys(parts[0] + "".join(seam + part for seam, part in zip(combo, parts[1:], strict=True)) for combo in combos))


def _particle_form(kana: str) -> str | None:
    """Read the last non-initial は/へ as the particle わ/え (こんにちは = 콘니치와), since TJ spells particles by sound."""
    if (index := max(kana.rfind("は"), kana.rfind("へ"))) < 1:
        return None
    return kana[:index] + ("わ" if kana[index] == "は" else "え") + kana[index + 1 :]


def _split_moras(kana: str) -> list[str]:
    moras: list[str] = []
    index = 0
    while index < len(kana):
        pair = kana[index : index + _MAX_MORA_LEN]
        if len(pair) == _MAX_MORA_LEN and pair in _KANA_TO_HANGUL:
            moras.append(pair)
            index += _MAX_MORA_LEN
        else:
            moras.append(kana[index])
            index += 1
    return moras


def _attach_jongseong(syllable: str, marker: str) -> str | None:
    """Attach a jongseong to the previous syllable. None if it already has one or isn't Hangul."""
    offset = ord(syllable) - _HANGUL_BASE
    if not 0 <= offset < _HANGUL_COUNT or offset % _JONGSEONG_COUNT:
        return None
    return chr(ord(syllable) + _JONGSEONG_INDEX[marker])


def _assemble(pieces: tuple[str, ...]) -> str:
    """Join the pieces, or return "" when a ん/っ has no open syllable to attach to (silently dropping it would search for the wrong word)."""
    result: list[str] = []
    for piece in pieces:
        if not piece:
            # "ー" maps to "", meaning it carries no Hangul syllable of its own to attach to.
            continue
        if piece in _JONGSEONG_INDEX:
            if not result or (attached := _attach_jongseong(result[-1], piece)) is None:
                return ""
            result[-1] = attached
            continue
        result.append(piece)
    return "".join(result)


def _slot_priority(mora: str, index: int) -> int:
    """Priority for spending the candidate budget (lower = spent first, palatalized moras vary most)."""
    if mora in ("ん", "ン"):
        return 3
    if len(mora) > 1:
        return 0
    return 1 if index == 0 else 2  # word-initial voiceless consonants vary more often than mid-word ones


def _hangul_variants(kana: str, limit: int) -> list[str]:
    moras = _split_moras(kana)
    slots: list[tuple[str, ...]] = []
    for mora in moras:
        value = _KANA_TO_HANGUL.get(mora)
        if value is None:
            # Bail out on kana with no Hangul mapping (e.g. ゐ or a lone small ぁ); kanji never reaches here.
            return []
        slots.append(value if isinstance(value, tuple) else (value,))

    # Cap the combinatorial explosion: spend the budget on the highest-priority slots first.
    budget = 1
    branching: set[int] = set()
    for index in sorted(range(len(slots)), key=lambda i: (_slot_priority(moras[i], i), i)):
        if budget * len(slots[index]) <= limit:
            budget *= len(slots[index])
            branching.add(index)

    limited = [options if index in branching else options[:1] for index, options in enumerate(slots)]
    return [_assemble(combo) for combo in itertools.product(*limited)]


def _interleave(kana_forms: list[str], limit: int) -> Iterator[str | None]:
    """Alternate between the forms so the first one's variants can't use up the whole budget (toukyou -> 토쿄)."""
    return itertools.chain.from_iterable(itertools.zip_longest(*(_hangul_variants(kana, limit) for kana in kana_forms if kana)))


def hangul_candidates(query: str, *, limit: int = 4) -> list[str]:
    """Return up to `limit` Hangul candidates, most likely first, or [] if not convertible."""
    stripped = _PUNCTUATION_PATTERN.sub(" ", unicodedata.normalize("NFKC", query).translate(_FOLD)).strip()
    if not stripped:
        return []

    if _ROMAJI_PATTERN.fullmatch(stripped) and _HAS_LETTER_PATTERN.search(stripped):
        # Don't strip the apostrophe — it's the only clue marking a ん boundary (see `n'` above).
        # A doubled vowel collapses to one since Hangul spellings drop vowel length; per word, so it can't merge across a boundary.
        # `ou` can be a word boundary, so it's handled separately.
        base = "".join(re.sub(r"([aiueo])\1", r"\1", word) for word in re.split(r"[\s\-]+", stripped.lower()))
        kana_forms = [_romaji_to_kana(source) for source in _ou_forms(base)]
    elif _KANA_ONLY_PATTERN.fullmatch(stripped) and _HAS_KANA_PATTERN.search(stripped):
        kana = re.sub(r"[\s・]", "", stripped)
        forms = [kana, *filter(None, [_particle_form(kana)])]
        kana_forms = list(dict.fromkeys([*forms, *(_LONG_O_PATTERN.sub("", form) for form in forms)]))
    else:
        return []

    # Mixed `ou` readings come only after the all-or-nothing pair's variants, or they'd crowd out likelier ones.
    ordered = itertools.chain(_interleave(kana_forms[:_PRIMARY_FORMS], limit), _interleave(kana_forms[_PRIMARY_FORMS:], limit))
    return list(dict.fromkeys(filter(None, ordered)))[:limit]
