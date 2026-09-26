import pytest

from founder_assistant.money import fmt_k, fmt_pct, fmt_vnd, parse_vnd, pct_change
from founder_assistant.units import comparable, normalize_unit


@pytest.mark.parametrize("raw,expected", [
    ("450 ngàn", 450_000), ("450k", 450_000), ("450.000", 450_000), ("450,000", 450_000),
    ("930.000đ", 930_000), ("1tr2", 1_200_000), ("1 triệu 200", 1_200_000), ("1,5 triệu", 1_500_000),
    ("88 nghìn", 88_000), ("2tr", 2_000_000), ("120000", 120_000), ("3.850.000 VND", 3_850_000),
])
def test_parse_vnd(raw, expected):
    assert parse_vnd(raw) == expected


@pytest.mark.parametrize("raw", ["bốn trăm", "", None, "abc", "khoảng 400"])
def test_parse_vnd_refuses_to_guess(raw):
    assert parse_vnd(raw) is None


def test_formatting():
    assert fmt_vnd(930000) == "930.000đ"
    assert fmt_vnd(None) == "UNKNOWN"
    assert fmt_k(92000) == "92k"
    assert fmt_pct(pct_change(88000, 92000)) == "+4,55%"
    assert fmt_pct(pct_change(118000, 125000)) == "+5,93%"
    assert fmt_pct(pct_change(28000, 24000)) == "-14,29%"


def test_units_normalized():
    for raw in ("kg", "ký", "kí", "Kg", "kilo", "cân"):
        assert normalize_unit(raw).canonical == "kg", raw
    assert normalize_unit("gram").base == "kg" and normalize_unit("gram").factor == 0.001
    assert normalize_unit("lạng").factor == 0.1
    assert normalize_unit("thùng").base == "thùng"


def test_units_never_mixed():
    assert not comparable(normalize_unit("kg"), normalize_unit("thùng"))
    assert not comparable(normalize_unit("chai"), normalize_unit("lon"))
    assert comparable(normalize_unit("g"), normalize_unit("kg"))
    assert normalize_unit("xyz") is None
    assert normalize_unit(None) is None
