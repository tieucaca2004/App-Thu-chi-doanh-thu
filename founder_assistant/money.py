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
        return None
    return int(v)


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
