"""Parse and format Vietnamese money amounts.

`parse_vnd` is deliberately conservative: it returns None when unsure instead of guessing.
It is used to cross-check the AI's numeric reading against the raw text it cited.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .textnorm import strip_accents

_THOUSAND = r"(?:k|nghin|ngan|ng|n)"
_MILLION = r"(?:tr|trieu|m|cu)"


def _num(s: str) -> Decimal | None:
    """'450.000' / '450,000' -> 450000 ; '1,5' / '1.5' -> 1.5 ; '88' -> 88."""
    s = s.strip()
    if not s:
        return None
    # thousand separators: groups of exactly 3 digits after . or ,
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        return Decimal(re.sub(r"[.,]", "", s))
    if re.fullmatch(r"\d+([.,]\d+)?", s):
        try:
            return Decimal(s.replace(",", "."))
        except InvalidOperation:
            return None
    return None


def parse_vnd(text: str | None) -> int | None:
    """Parse an amount phrase into VND. Returns None if it cannot be parsed with confidence.

    Examples: '450 ngàn' -> 450000, '88k' -> 88000, '1tr2' -> 1200000, '1,5 triệu' -> 1500000,
    '930.000đ' -> 930000, '1 triệu 200' -> 1200000.
    """
    if text is None:
        return None
    t = strip_accents(str(text)).lower().strip()
    t = re.sub(r"(vnd|dong|d)\s*$", "", t).strip()
    # per-unit suffix of a unit price: '90k/kg', 'chín mươi lăm ngàn một ký'
    t = re.sub(r"\s*(/|\bmot\b|\b1\b)\s*(kg|ky|ki|kilo|can)\s*$", "", t).strip()
    t = t.replace("/kg", "").strip()
    if not t:
        return None

    # 1tr2 / 1tr200 / 1 trieu 200 / 1 trieu 2
    m = re.fullmatch(rf"(\d+)\s*{_MILLION}\s*(\d{{1,3}})?\s*(?:{_THOUSAND})?", t)
    if m:
        millions = int(m.group(1)) * 1_000_000
        rest = m.group(2)
        if rest:
            # '1tr2' means 1.2M, '1tr200' / '1 trieu 200' means 1.2M
            rest_val = int(rest) * (100_000 if len(rest) == 1 else 10_000 if len(rest) == 2 else 1_000)
            return millions + rest_val
        return millions

    m = re.fullmatch(rf"([\d.,]+)\s*{_MILLION}", t)
    if m:
        v = _num(m.group(1))
        return int(v * 1_000_000) if v is not None else None

    m = re.fullmatch(rf"([\d.,]+)\s*{_THOUSAND}", t)
    if m:
        v = _num(m.group(1))
        return int(v * 1_000) if v is not None else None

    v = _num(t)
    if v is None:
        return parse_vnd_words(t)
    return int(v)


_DIGITS = {"khong": 0, "mot": 1, "hai": 2, "ba": 3, "bon": 4, "tu": 4, "nam": 5, "lam": 5, "sau": 6,
           "bay": 7, "tam": 8, "chin": 9}
_SCALES = {"nghin": 1_000, "ngan": 1_000, "trieu": 1_000_000, "ty": 1_000_000_000}
_FILLER = {"dong", "vnd", "d", "chan"}


def parse_vnd_words(text: str) -> int | None:
    """Spoken Vietnamese amounts (accent-free input), incl. colloquial forms:
    'bon tram nam chuc ngan' -> 450000, 'mot trieu hai' -> 1200000, 'mot tram hai' -> 120,
    'chin muoi lam ngan' -> 95000, 'hai trieu ruoi' -> 2500000. Unknown words -> None (never guess)."""
    tokens = re.findall(r"[a-z]+|\d+", text)
    if not tokens:
        return None
    total, group, digit = 0, 0, None
    prev: str | None = None          # previous meaningful token kind: 'tram' | 'scale' | 'le' | 'tens' | None
    last_scale = 1
    for tok in tokens:
        if tok in _FILLER:
            continue
        if tok.isdigit():
            n = int(tok)
            if n < 10 and digit is None:
                digit = n
            else:
                group += n
            continue
        if tok in _DIGITS:
            if digit is not None:
                return None
            digit = _DIGITS[tok]
        elif tok in ("muoi", "chuc"):
            group += (digit if digit is not None else 1) * 10
            digit, prev = None, "tens"
        elif tok == "tram":
            group += (digit if digit is not None else 1) * 100
            digit, prev = None, "tram"
        elif tok in ("le", "linh"):
            prev = "le"
        elif tok == "ruoi":
            unit = 100 if prev == "tram" else last_scale if prev == "scale" else None
            if unit is None:
                return None
            if prev == "tram":
                group += 50
            else:
                total += unit // 2
        elif tok in _SCALES:
            scale = _SCALES[tok]
            group += digit if digit is not None else 0
            total += (group or 1) * scale
            group, digit, prev, last_scale = 0, None, "scale", scale
        else:
            return None
    if digit is not None:
        if prev == "tram":
            group += digit * 10                      # 'một trăm hai' = 120
        elif prev == "scale":
            total += digit * last_scale // 10        # 'một triệu hai' = 1.200.000
        else:
            group += digit
    return total + group


def money_text_matches(raw: str | None, value: float) -> bool | None:
    """Does the raw money text support `value`? None = cannot verify (unparseable).

    Accepts the Vietnamese convention of omitting thousands ('450' on a handwritten bill, 'một trăm hai'
    spoken) only as EXACTLY x1000 — so '90.000' read as 900.000 is still a mismatch."""
    parsed = parse_vnd(raw)
    if parsed is None:
        return None
    if amounts_match(parsed, value, 1, 0):
        return True
    return 0 < parsed < 1000 and amounts_match(parsed * 1000, value, 1, 0)


def fmt_vnd(amount: float | int | None, suffix: str = "đ") -> str:
    """930000 -> '930.000đ'. None -> 'UNKNOWN'."""
    if amount is None:
        return "UNKNOWN"
    sign = "-" if amount < 0 else ""
    s = f"{abs(round(amount)):,}".replace(",", ".")
    return f"{sign}{s}{suffix}"


def fmt_signed_vnd(amount: float | int | None, suffix: str = "đ") -> str:
    if amount is None:
        return "UNKNOWN"
    return ("+" if amount > 0 else "") + fmt_vnd(amount, suffix)


def fmt_k(amount: float | None) -> str:
    """92000 -> '92k', 92500 -> '92,5k' (compact price display)."""
    if amount is None:
        return "UNKNOWN"
    if abs(amount) >= 1000:
        k = amount / 1000
        s = f"{k:.1f}".rstrip("0").rstrip(".")
        return s.replace(".", ",") + "k"
    return fmt_vnd(amount)


def fmt_pct(pct: float | None) -> str:
    """4.5454 -> '+4,55%'."""
    if pct is None:
        return "UNKNOWN"
    s = f"{pct:+.2f}".replace(".", ",")
    return f"{s}%"


def pct_change(old: float, new: float) -> float | None:
    if not old:
        return None
    return (new - old) / old * 100.0


def amounts_match(expected: float, actual: float, tol_vnd: float = 1000.0, tol_pct: float = 0.5) -> bool:
    diff = abs(expected - actual)
    return diff <= tol_vnd or (actual and diff / abs(actual) * 100 <= tol_pct)
