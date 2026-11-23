"""Korean numeral normalization: compact Arabic digits for subtitles/display and natural Korean readings for TTS."""

from __future__ import annotations

import re

_SINO_DIGITS = ("", "일", "이", "삼", "사", "오", "육", "칠", "팔", "구")
_SINO_SMALL_UNITS = ("", "십", "백", "천")
_SINO_LARGE_UNITS = ("", "만", "억", "조")

_NATIVE_TENS = ("", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔")
_NATIVE_ONES_NOUN = ("", "하나", "둘", "셋", "넷", "다섯", "여섯", "일곱", "여덟", "아홉")
_NATIVE_ONES_MOD = ("", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉")

NATIVE_COUNTER_UNITS = (
    "작은술",
    "티스푼",
    "숟가락",
    "군데",
    "사람",
    "그릇",
    "접시",
    "조각",
    "스푼",
    "큰술",
    "마리",
    "켤레",
    "봉지",
    "바퀴",
    "시간",
    "가지",
    "곳",
    "개",
    "권",
    "달",
    "명",
    "번",
    "벌",
    "병",
    "살",
    "시",
    "알",
    "잔",
    "장",
    "줄",
    "채",
    "캔",
    "컵",
    "톨",
    "팩",
)

UNIT_TTS_SPOKEN_MAP: dict[str, str] = {
    "kcal": "킬로칼로리",
    "kg": "킬로그램",
    "mg": "밀리그램",
    "ml": "밀리리터",
    "g": "그램",
    "l": "리터",
    "cm": "센티미터",
    "mm": "밀리미터",
    "km": "킬로미터",
    "m": "미터",
    "%": "퍼센트",
    "°c": "도",
    "℃": "도",
    "°": "도",
    "fps": "프레임",
    "gb": "기가바이트",
    "mb": "메가바이트",
    "kb": "킬로바이트",
    "tb": "테라바이트",
    "hz": "헤르츠",
    "w": "와트",
    "v": "볼트",
}

UNIT_DISPLAY_SYMBOL_MAP: dict[str, str] = {
    "밀리리터": "ml",
    "킬로그램": "kg",
    "밀리그램": "mg",
    "그램": "g",
    "리터": "L",
    "퍼센트": "%",
    "킬로칼로리": "kcal",
    "센티미터": "cm",
    "밀리미터": "mm",
    "킬로미터": "km",
}

_ALL_UNIT_PATTERN = (
    r"(?:"
    + "|".join(re.escape(u) for u in NATIVE_COUNTER_UNITS)
    + r"|개월|학년|단계|시간|밀리리터|킬로그램|밀리그램|킬로칼로리|센티미터|밀리미터|킬로미터|퍼센트|그램|리터|미터|"
    + r"kcal|fps|gb|mb|kb|tb|hz|kg|mg|ml|cm|mm|km|°c|℃|%|°|g|l|m|w|v|"
    + r"년|월|일|주|분|초|배|원|달러|위|등|층|호|화|편|회|차|점|대"
    + r")"
)


def number_to_sino_korean(value: int) -> str:
    """@brief Convert a non-negative integer into Sino-Korean words (e.g. 450 -> '사백오십')."""
    if value < 0:
        return "마이너스 " + number_to_sino_korean(-value)
    if value == 0:
        return "영"
    groups: list[str] = []
    group_idx = 0
    remaining = value
    while remaining > 0 and group_idx < len(_SINO_LARGE_UNITS):
        chunk = remaining % 10000
        remaining //= 10000
        if chunk > 0:
            chunk_parts: list[str] = []
            for pos in range(3, -1, -1):
                digit = (chunk // (10**pos)) % 10
                if digit == 0:
                    continue
                if pos > 0 and digit == 1:
                    chunk_parts.append(_SINO_SMALL_UNITS[pos])
                else:
                    chunk_parts.append(_SINO_DIGITS[digit] + _SINO_SMALL_UNITS[pos])
            chunk_str = "".join(chunk_parts)
            if group_idx > 0:
                if group_idx == 1 and chunk == 1:
                    chunk_str = "만"
                else:
                    chunk_str = f"{chunk_str}{_SINO_LARGE_UNITS[group_idx]}"
            groups.append(chunk_str)
        group_idx += 1
    return " ".join(reversed(groups)) if len(groups) > 1 else groups[0]


def number_to_native_korean(value: int, *, modifier: bool = True) -> str:
    """@brief Convert integers 1..99 to native Korean numerals; >=100 falls back to Sino-Korean."""
    if value <= 0 or value >= 100:
        return number_to_sino_korean(value)
    if modifier and value == 20:
        return "스무"
    tens, ones = divmod(value, 10)
    tens_str = _NATIVE_TENS[tens]
    ones_str = (_NATIVE_ONES_MOD if modifier else _NATIVE_ONES_NOUN)[ones]
    return f"{tens_str}{ones_str}"


def _read_decimal_sino(raw_num: str) -> str:
    cleaned = raw_num.replace(",", "")
    if "." not in cleaned:
        return number_to_sino_korean(int(cleaned))
    int_part, frac_part = cleaned.split(".", 1)
    head = number_to_sino_korean(int(int_part or "0"))
    tail = "".join("영" if ch == "0" else _SINO_DIGITS[int(ch)] for ch in frac_part if ch.isdigit())
    return f"{head} 점 {tail}" if tail else head


def _spoken_unit(unit: str) -> str:
    if not unit:
        return ""
    return UNIT_TTS_SPOKEN_MAP.get(unit.lower(), unit)


def _format_number_with_unit_for_tts(raw_num: str, unit: str) -> str:
    cleaned = raw_num.replace(",", "")
    spoken_u = _spoken_unit(unit)
    if "." in cleaned:
        num_str = _read_decimal_sino(cleaned)
        return f"{num_str} {spoken_u}".strip()
    n = int(cleaned)
    if unit == "월":
        if n == 6:
            return "유월"
        if n == 10:
            return "시월"
        return f"{number_to_sino_korean(n)}월"
    if unit in NATIVE_COUNTER_UNITS and 1 <= n <= 99:
        num_str = number_to_native_korean(n, modifier=True)
        return f"{num_str} {spoken_u}".strip()
    num_str = number_to_sino_korean(n)
    if spoken_u in {"년", "일", "주", "개월", "원", "층", "호", "위", "등"}:
        return f"{num_str}{spoken_u}"
    return f"{num_str} {spoken_u}".strip()


def korean_text_to_tts_speech(text: str) -> str:
    """@brief Convert display numbers, ratios, ranges, and unit symbols into natural spoken Korean for TTS."""
    src = str(text or "")
    if not src.strip():
        return src

    # 1. Currency prefix like "$10", "$1.5" -> "십 달러", "일 점 오 달러"
    def _repl_dollar(match: re.Match[str]) -> str:
        return f"{_read_decimal_sino(match.group(1))} 달러"

    src = re.sub(r"\$\s*(\d+(?:,\d{3})*(?:\.\d+)?)", _repl_dollar, src)

    # 2. Ratios like "1:9", "16:9", or "1 대 9" -> "일 대 구"
    def _repl_ratio(match: re.Match[str]) -> str:
        return f"{_read_decimal_sino(match.group(1))} 대 {_read_decimal_sino(match.group(2))}"

    src = re.sub(
        r"(?<![A-Za-z0-9\-])(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?::|대)\s*(\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9])",
        _repl_ratio,
        src,
    )

    # 3. Ranges with explicit unit: "12~16시간" -> "열두 시간에서 열여섯 시간"
    range_unit_re = re.compile(
        rf"(?<![A-Za-z0-9])(\d+(?:,\d{{3}})*(?:\.\d+)?)\s*(?:~|-)\s*(\d+(?:,\d{{3}})*(?:\.\d+)?)\s*({_ALL_UNIT_PATTERN})(?![A-Za-z0-9가-힣])",
        flags=re.IGNORECASE,
    )

    def _repl_range_unit(match: re.Match[str]) -> str:
        n1, n2, unit = match.group(1), match.group(2), match.group(3)
        return f"{_format_number_with_unit_for_tts(n1, unit)}에서 {_format_number_with_unit_for_tts(n2, unit)}"

    src = range_unit_re.sub(_repl_range_unit, src)

    # 4. Ranges without unit: "1~3" -> "일에서 삼"
    src = re.sub(
        r"(?<![A-Za-z0-9])(\d+(?:,\d{3})*(?:\.\d+)?)\s*~\s*(\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9])",
        lambda m: f"{_read_decimal_sino(m.group(1))}에서 {_read_decimal_sino(m.group(2))}",
        src,
    )

    # 5. Ordinals like "1번째", "2번째", "3번째" -> "첫 번째", "두 번째", "세 번째"
    def _repl_ordinal(match: re.Match[str]) -> str:
        n = int(match.group(1))
        first_map = {1: "첫", 2: "두", 3: "세", 4: "네"}
        word = first_map.get(n) if n in first_map else (number_to_native_korean(n, modifier=True) if 1 <= n <= 99 else number_to_sino_korean(n))
        return f"{word} 번째"

    src = re.sub(r"(?<![A-Za-z0-9])(\d{1,3})\s*번째", _repl_ordinal, src)

    # 6. Number + unit: "12시간" -> "열두 시간", "50g" -> "오십 그램", "450ml" -> "사백오십 밀리리터"
    num_unit_re = re.compile(
        rf"(?<![A-Za-z0-9.\-])(\d+(?:,\d{{3}})*(?:\.\d+)?)\s*({_ALL_UNIT_PATTERN})(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )
    src = num_unit_re.sub(lambda m: _format_number_with_unit_for_tts(m.group(1), m.group(2)), src)

    # 7. Remaining standalone Arabic numbers (avoiding alphanumeric codes like MP4, H264, 4K)
    src = re.sub(
        r"(?<![A-Za-z0-9.\-])(\d+(?:,\d{3})*(?:\.\d+)?)(?![A-Za-z0-9])",
        lambda m: _read_decimal_sino(m.group(1)),
        src,
    )
    return re.sub(r"\s{2,}", " ", src).strip()


# --- Reverse helper: spelled-out Korean numerals -> compact Arabic digits for subtitles ---

_HANGUL_SINO_DIGIT = {"영": 0, "공": 0, "일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "유": 6, "칠": 7, "팔": 8, "구": 9}
_HANGUL_SINO_SMALL = {"십": 10, "백": 100, "천": 1000}
_HANGUL_NATIVE_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50, "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
_HANGUL_NATIVE_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}


def _parse_spelled_korean_int(word: str) -> int | None:
    cleaned = word.strip()
    if not cleaned:
        return None
    if cleaned in _HANGUL_NATIVE_TENS:
        return _HANGUL_NATIVE_TENS[cleaned]
    for ten_w, ten_v in _HANGUL_NATIVE_TENS.items():
        if cleaned.startswith(ten_w):
            rest = cleaned[len(ten_w) :]
            if rest in _HANGUL_NATIVE_ONES:
                return ten_v + _HANGUL_NATIVE_ONES[rest]
            return None
    if cleaned in _HANGUL_NATIVE_ONES:
        return _HANGUL_NATIVE_ONES[cleaned]

    total = 0
    current = 0
    seen = False
    idx = 0
    while idx < len(cleaned):
        ch = cleaned[idx]
        if ch in _HANGUL_SINO_DIGIT:
            current = _HANGUL_SINO_DIGIT[ch]
            seen = True
        elif ch in _HANGUL_SINO_SMALL:
            unit = _HANGUL_SINO_SMALL[ch]
            total += (current if current > 0 else 1) * unit
            current = 0
            seen = True
        elif ch == "만":
            total = (total + current if (total + current) > 0 else 1) * 10000
            current = 0
            seen = True
        else:
            return None
        idx += 1
    return total + current if seen else None


_NATIVE_TEENS_OR_HIGHER_WORDS = (
    r"(?:열|스물|스무|서른|마흔|쉰|예순|일흔|여든|아흔)"
    r"(?:하나|한|둘|두|셋|세|넷|네|다섯|여섯|일곱|여덟|아홉)?"
    r"|다섯|여섯|일곱|여덟|아홉"
)
_SINO_MULTI_OR_SINGLE_WORDS = r"[일이삼사오육칠팔구]?천(?:[일이삼사오육칠팔구]?백)?(?:[일이삼사오육칠팔구]?십)?[일이삼사오육칠팔구]?|[일이삼사오육칠팔구]?백(?:[일이삼사오육칠팔구]?십)?[일이삼사오육칠팔구]?|[일이삼사오육칠팔구]?십[일이삼사오육칠팔구]?|[일이삼사오육칠팔구]"


def normalize_korean_display_text(text: str) -> str:
    """@brief Convert spelled-out Hangul numbers attached to clear units into clean Arabic digits for subtitles."""
    src = str(text or "")
    if not src.strip():
        return src

    # 1. Spelled ratio like "일 대 구" or "일 대 십" -> "1:9", "1:10" (only single-word numerals directly around '대')
    sino_single = r"(?:[일이삼사오육칠팔구]?십[일이삼사오육칠팔구]?|[일이삼사오육칠팔구])"
    src = re.sub(
        rf"(?<![가-힣])({sino_single})\s+대\s+({sino_single})(?![가-힣])",
        lambda m: (
            f"{_parse_spelled_korean_int(m.group(1))}:{_parse_spelled_korean_int(m.group(2))}"
            if _parse_spelled_korean_int(m.group(1)) is not None and _parse_spelled_korean_int(m.group(2)) is not None
            else m.group(0)
        ),
        src,
    )

    # 2. Spelled numerals + explicit technical/measurement units -> Arabic digits + compact symbols
    unit_pattern = r"밀리리터|킬로그램|밀리그램|킬로칼로리|센티미터|밀리미터|킬로미터|퍼센트|그램|리터|시간|분|초|도"
    num_word_pattern = rf"(?:{_NATIVE_TEENS_OR_HIGHER_WORDS}|{_SINO_MULTI_OR_SINGLE_WORDS})"

    def _repl_spelled_unit(match: re.Match[str]) -> str:
        num_word, unit_word = match.group(1), match.group(2)
        # Keep ambiguous single-syllable Sino words unless paired with unambiguous technical metric units.
        metric_units = {"밀리리터", "킬로그램", "밀리그램", "킬로칼로리", "센티미터", "밀리미터", "킬로미터", "퍼센트", "그램", "리터"}
        parsed = _parse_spelled_korean_int(num_word)
        if parsed is None:
            return match.group(0)
        if unit_word not in metric_units:
            # Avoid rewriting idiomatic words like "이분", "일초", "삼도", "구분", "사분" unless >= 2 chars or compound numeral.
            if num_word in {"일", "이", "삼", "사", "오", "육", "칠", "팔", "구", "십", "백", "천"} and unit_word in {"분", "초", "도", "시간"}:
                if len(num_word) == 1 and unit_word != "시간":
                    return match.group(0)
        compact_unit = UNIT_DISPLAY_SYMBOL_MAP.get(unit_word, unit_word)
        return f"{parsed}{compact_unit}"

    unit_end_boundary = (
        r"(?=$|[^가-힣A-Za-z0-9]|(?:에서|에게|으로|부터|까지|만큼|정도|씩이나|이나|라고|이라는|에는|에도|과는|와는|에|을|를|이|가|은|는|의|와|과|로|만|씩|도|쯤)(?![가-힣]))"
    )
    src = re.sub(
        rf"(?<![가-힣])({num_word_pattern})\s*({unit_pattern}){unit_end_boundary}",
        _repl_spelled_unit,
        src,
    )
    return src
