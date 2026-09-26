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


@pytest.mark.parametrize("raw", ["", None, "abc", "khoảng 400", "hai ba", "rưỡi"])
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


# ---------------------------------------------------------------- §11 money accuracy (BUG-007)
@pytest.mark.parametrize("raw,expected", [
    ("1.000", 1_000), ("10.000", 10_000), ("100.000", 100_000), ("1.000.000", 1_000_000),
    ("10.500", 10_500), ("10.500.000", 10_500_000), ("10,500", 10_500), ("90.000", 90_000),
    ("10,5 triệu", 10_500_000), ("2,5tr", 2_500_000), ("1.5 triệu", 1_500_000),
    ("bốn trăm năm chục ngàn", 450_000), ("ba trăm sáu chục", 360), ("một trăm hai", 120),
    ("hai trăm ngàn", 200_000), ("chín mươi lăm ngàn", 95_000), ("một triệu hai", 1_200_000),
    ("một triệu hai trăm ngàn", 1_200_000), ("hai triệu rưỡi", 2_500_000), ("mười lăm nghìn", 15_000),
    ("hai mươi mốt nghìn", 21_000), ("một trăm lẻ năm nghìn", 105_000), ("năm trăm", 500),
    ("một nghìn rưỡi", 1_500), ("ba trăm ngàn đồng", 300_000), ("chín mươi lăm ngàn một ký", 95_000),
    ("90k/kg", 90_000), ("mười triệu năm trăm ngàn", 10_500_000),
])
def test_money_matrix(raw, expected):
    assert parse_vnd(raw) == expected


def test_implicit_thousand_only_exact():
    from founder_assistant.money import money_text_matches
    assert money_text_matches("450", 450_000) is True            # handwritten bill '450'
    assert money_text_matches("ba trăm sáu chục", 360_000) is True
    assert money_text_matches("một trăm hai", 120_000) is True
    assert money_text_matches("90.000", 900_000) is False        # the classic x10 misread
    assert money_text_matches("90.000", 90_000) is True
    assert money_text_matches("450 ngàn", 4_500_000) is False
    assert money_text_matches("bốn trăm năm chục ngàn", 45_000) is False
    assert money_text_matches("khoảng bốn trăm", 400_000) is None  # cannot verify -> never "match"
