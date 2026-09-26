"""Price history & price-change detection. Pure arithmetic over stored FACTS.

V1 compares with the most recent earlier price of the same product in the same base unit.
7/30-day averages are provided as extra context. Trend wording (an INFERENCE) is only
produced when there are enough data points; otherwise we say so explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .db import DB
from .money import pct_change

MIN_POINTS_FOR_TREND = 4
NOT_ENOUGH_DATA = "Không đủ dữ liệu để kết luận xu hướng."


@dataclass
class PriceChange:
    product_id: int
    product_name: str
    base_unit: str
    old_price: float
    new_price: float
    diff: float
    pct: float
    old_date: str
    new_date: str
    old_ref: int          # price_history.id
    new_ref: int
    old_source: str
    new_source: str

    @property
    def direction(self) -> str:
        return "UP" if self.diff > 0 else "DOWN" if self.diff < 0 else "SAME"


def previous_price(db: DB, product_id: int, base_unit: str, price_date: str, before_id: int):
    return db.one(
        """SELECT * FROM price_history
           WHERE product_id = ? AND base_unit = ?
             AND (price_date < ? OR (price_date = ? AND id < ?))
           ORDER BY price_date DESC, id DESC LIMIT 1""",
        (product_id, base_unit, price_date, price_date, before_id),
    )


def change_for(db: DB, ph_id: int) -> PriceChange | None:
    cur = db.one(
        "SELECT ph.*, p.name AS product_name FROM price_history ph JOIN products p ON p.id = ph.product_id WHERE ph.id = ?",
        (ph_id,),
    )
    if cur is None:
        return None
    prev = previous_price(db, cur["product_id"], cur["base_unit"], cur["price_date"], cur["id"])
    if prev is None:
        return None
    pct = pct_change(prev["unit_price_base"], cur["unit_price_base"])
    if pct is None:
        return None
    return PriceChange(
        product_id=cur["product_id"], product_name=cur["product_name"], base_unit=cur["base_unit"],
        old_price=prev["unit_price_base"], new_price=cur["unit_price_base"],
        diff=cur["unit_price_base"] - prev["unit_price_base"], pct=pct,
        old_date=prev["price_date"], new_date=cur["price_date"], old_ref=prev["id"], new_ref=cur["id"],
        old_source=prev["source"], new_source=cur["source"],
    )


def average_price(db: DB, product_id: int, base_unit: str, until: str, days: int) -> tuple[float | None, int]:
    start = (date.fromisoformat(until) - timedelta(days=days - 1)).isoformat()
    r = db.one(
        """SELECT AVG(unit_price_base) AS avg, COUNT(*) AS n FROM price_history
           WHERE product_id = ? AND base_unit = ? AND price_date BETWEEN ? AND ?""",
        (product_id, base_unit, start, until),
    )
    return (r["avg"], r["n"]) if r and r["n"] else (None, 0)


def last_price(db: DB, product_id: int):
    return db.one(
        """SELECT * FROM price_history WHERE product_id = ?
           ORDER BY price_date DESC, id DESC LIMIT 1""",
        (product_id,),
    )


def trend_statement(db: DB, product_id: int, base_unit: str, until: str) -> str:
    """INFERENCE, only with >= MIN_POINTS_FOR_TREND points and strictly monotonic recent prices."""
    rows = db.all(
        """SELECT unit_price_base FROM price_history WHERE product_id = ? AND base_unit = ? AND price_date <= ?
           ORDER BY price_date DESC, id DESC LIMIT ?""",
        (product_id, base_unit, until, MIN_POINTS_FOR_TREND),
    )
    prices = [r["unit_price_base"] for r in reversed(rows)]
    if len(prices) < MIN_POINTS_FOR_TREND:
        return NOT_ENOUGH_DATA
    ups = all(b > a for a, b in zip(prices, prices[1:]))
    downs = all(b < a for a, b in zip(prices, prices[1:]))
    n = len(prices) - 1
    if ups:
        return f"(Nhận định) Giá tăng liên tiếp {n} lần mua gần nhất."
    if downs:
        return f"(Nhận định) Giá giảm liên tiếp {n} lần mua gần nhất."
    return "(Nhận định) Giá biến động lên xuống, chưa thấy xu hướng rõ."


def quantity_is_unusual(db: DB, product_id: int, base_unit: str, quantity_base: float, exclude_id: int) -> float | None:
    """Return the typical (median) quantity if this one is > 5x or < 1/5 of it (needs >= 3 history points)."""
    rows = db.all(
        """SELECT quantity_base FROM price_history WHERE product_id = ? AND base_unit = ?
           AND source = 'purchase' AND quantity_base IS NOT NULL AND id != ?""",
        (product_id, base_unit, exclude_id),
    )
    qs = sorted(r["quantity_base"] for r in rows if r["quantity_base"])
    if len(qs) < 3 or not quantity_base:
        return None
    median = qs[len(qs) // 2]
    if quantity_base > median * 5 or quantity_base < median / 5:
        return median
    return None
