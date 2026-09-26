"""Unit normalization. Never compare quantities/prices across incompatible units.

Every raw unit maps to (canonical_unit, base_unit, factor_to_base).
Prices are compared per base unit (e.g. 1 lạng = 0.1 kg -> price/kg).
Count-like units (thùng, chai, con, ...) are their own base: 'thùng' is never compared to 'kg'.
"""
from __future__ import annotations

from dataclasses import dataclass

from .textnorm import norm_key


@dataclass(frozen=True)
class Unit:
    canonical: str  # what we store/display: 'kg', 'g', 'thùng', ...
    base: str       # comparison unit: 'kg', 'l', 'thùng', ...
    factor: float   # quantity_in_base = quantity * factor


_MASS = {
    "kg": Unit("kg", "kg", 1.0),
    "g": Unit("g", "kg", 0.001),
    "lạng": Unit("lạng", "kg", 0.1),
    "tấn": Unit("tấn", "kg", 1000.0),
    "yến": Unit("yến", "kg", 10.0),
}
_VOLUME = {
    "l": Unit("l", "l", 1.0),
    "ml": Unit("ml", "l", 0.001),
}
_COUNT = [
    "thùng", "chai", "lon", "con", "cái", "phần", "bó", "gói", "hộp", "túi", "vỉ",
    "quả", "bịch", "két", "bao", "can", "bình", "cây", "tô", "dĩa", "ly", "chén", "khay", "miếng",
]

# raw spelling (accent-free key) -> canonical unit name
_ALIASES: dict[str, str] = {
    "kg": "kg", "kgs": "kg", "ki": "kg", "ky": "kg", "kilo": "kg", "kilogram": "kg", "ki lo": "kg", "ki lo gam": "kg", "ki lo gram": "kg", "kilogam": "kg",
    "g": "g", "gr": "g", "gram": "g", "gam": "g",
    "lang": "lạng", "tan": "tấn", "yen": "yến",
    "l": "l", "lit": "l", "liter": "l", "litre": "l", "ml": "ml", "mililit": "ml",
    "thung": "thùng", "chai": "chai", "lon": "lon", "con": "con", "cai": "cái", "phan": "phần",
    "bo": "bó", "goi": "gói", "hop": "hộp", "tui": "túi", "vi": "vỉ", "qua": "quả", "trai": "quả",
    "bich": "bịch", "binh": "bình", "ket": "két", "bao": "bao", "can": "can", "cay": "cây", "to": "tô",
    "dia": "dĩa", "dia com": "dĩa", "ly": "ly", "coc": "ly", "chen": "chén", "khay": "khay", "mieng": "miếng",
    "suat": "phần", "xuat": "phần",
}

_ACCENTED: dict[str, str] = {"cân": "kg", "ký": "kg", "kí": "kg", "can": "can"}

_UNITS: dict[str, Unit] = {**_MASS, **_VOLUME, **{u: Unit(u, u, 1.0) for u in _COUNT}}


def normalize_unit(raw: str | None) -> Unit | None:
    """Map a raw unit string to a Unit, or None when it can't be identified with confidence."""
    if not raw:
        return None
    stripped = raw.strip().lower()
    # accented spellings first: 'cân' (= kg) must not collide with 'can' (jerrycan)
    if stripped in _ACCENTED:
        return _UNITS[_ACCENTED[stripped]]
    if stripped in _UNITS:
        return _UNITS[stripped]
    key = norm_key(raw)
    if key in _ALIASES:
        return _UNITS[_ALIASES[key]]
    return None


def comparable(a: Unit | None, b: Unit | None) -> bool:
    return a is not None and b is not None and a.base == b.base
