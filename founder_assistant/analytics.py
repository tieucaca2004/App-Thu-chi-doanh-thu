"""Aggregations over CONFIRMED records only. Every figure carries references to its sources.

Revenue de-duplication rule (documented in the report):
  if a day has a revenue report (chốt ca / POS), revenue = sum of revenue reports and single
  sales bills that day are NOT added on top (they are almost always already inside the report).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .db import DB
from .pricing import PriceChange, change_for
from .textnorm import norm_key

CATEGORY_LABEL = {"NGUYEN_LIEU": "Nguyên liệu", "CHI_KHAC": "Chi khác", "CHUA_PHAN_LOAI": "Chưa phân loại"}
UTILITY_KEYWORDS = {"dien": "điện", "nuoc": "nước"}


@dataclass
class DailySummary:
    day: str
    revenue: float | None
    revenue_refs: list[str]
    revenue_note: str | None
    expense: float | None
    expense_by_category: dict[str, float]
    expense_refs: list[str]
    net: float | None
    bill_count: int | None
    transaction_count: int
    payments: dict[str, float | None]
    purchase_rows: list[dict]
    sale_rows: list[dict]
    price_changes: list[PriceChange]
    first_prices: list[dict]
    top_items: list[tuple[str, float, float | None]]
    alerts: list[dict]
    pending: list[dict]
    missing: list[str]
    extra: dict = field(default_factory=dict)


def _ref(prefix: str, row) -> str:
    return f"{prefix}{row['id']}"


def counted_sales(db: DB, day: str) -> tuple[list, list, str | None]:
    """Return (sales rows counted in revenue, sales rows excluded, note)."""
    rows = db.all("SELECT * FROM sales WHERE sale_date = ? AND status = 'confirmed' ORDER BY id", (day,))
    reports = [r for r in rows if r["doc_type"] == "REVENUE_REPORT"]
    bills = [r for r in rows if r["doc_type"] == "SALES_BILL"]
    if reports and bills:
        return reports, bills, (f"Doanh thu lấy theo báo cáo chốt ca/POS ({', '.join(_ref('B', r) for r in reports)}); "
                                f"{len(bills)} bill lẻ không cộng thêm để tránh tính trùng.")
    return rows, [], None


def day_revenue(db: DB, day: str) -> float | None:
    rows, _, _ = counted_sales(db, day)
    vals = [r["revenue"] for r in rows if r["revenue"] is not None]
    return float(sum(vals)) if vals else None


def daily_revenue_history(db: DB, day: str, days: int) -> list[float]:
    d = date.fromisoformat(day)
    out = []
    for i in range(1, days + 1):
        v = day_revenue(db, (d - timedelta(days=i)).isoformat())
        if v is not None:
            out.append(v)
    return out


def purchase_total(row) -> float | None:
    return row["stated_total"] if row["stated_total"] is not None else row["computed_total"]


def expense_in_range(db: DB, start: str, end: str) -> tuple[float | None, list[str]]:
    rows = db.all("SELECT * FROM purchases WHERE purchase_date BETWEEN ? AND ? AND status = 'confirmed'", (start, end))
    vals = [(purchase_total(r), r) for r in rows]
    known = [v for v, _ in vals if v is not None]
    return (float(sum(known)) if known else None), [_ref("M", r) for _, r in vals]


def revenue_in_range(db: DB, start: str, end: str) -> tuple[float | None, int]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    total, days = 0.0, 0
    while d <= e:
        v = day_revenue(db, d.isoformat())
        if v is not None:
            total += v
            days += 1
        d += timedelta(days=1)
    return (total if days else None), days


def top_items_in_range(db: DB, start: str, end: str, limit: int = 5) -> list[tuple[str, float, float | None]]:
    d, e = date.fromisoformat(start), date.fromisoformat(end)
    qty: dict[str, float] = defaultdict(float)
    amt: dict[str, float] = defaultdict(float)
    has_amt: dict[str, bool] = defaultdict(lambda: True)
    names: dict[str, str] = {}
    while d <= e:
        for r in _item_source_rows(db, d.isoformat()):
            key = r["item_key"]
            names.setdefault(key, r["raw_name"])
            qty[key] += r["quantity"] or 0
            if r["amount"] is None:
                has_amt[key] = False
            else:
                amt[key] += r["amount"]
        d += timedelta(days=1)
    ranked = sorted(qty, key=lambda k: (-qty[k], names[k]))
    return [(names[k], qty[k], amt[k] if has_amt[k] else None) for k in ranked[:limit] if qty[k] > 0]


def _item_source_rows(db: DB, day: str):
    counted, excluded, _ = counted_sales(db, day)
    sale_ids = [r["id"] for r in counted if db.one("SELECT 1 FROM sale_items WHERE sale_id = ?", (r["id"],))]
    if not sale_ids:
        sale_ids = [r["id"] for r in excluded]
    if not sale_ids:
        return []
    qs = ",".join("?" for _ in sale_ids)
    return db.all(f"SELECT * FROM sale_items WHERE sale_id IN ({qs})", sale_ids)


def daily_summary(db: DB, day: str) -> DailySummary:
    # ---- revenue
    counted, excluded, rev_note = counted_sales(db, day)
    rev_vals = [r["revenue"] for r in counted if r["revenue"] is not None]
    revenue = float(sum(rev_vals)) if rev_vals else None
    bill_count: int | None = 0
    for r in counted:
        if r["bill_count"] is not None:
            bill_count += r["bill_count"]
        elif r["doc_type"] == "SALES_BILL":
            bill_count += 1
        else:
            bill_count = None
            break
    if not counted:
        bill_count = None
    payments = {}
    for k in ("cash", "bank_transfer", "e_wallet", "platform_fee"):
        vals = [r[k] for r in counted if r[k] is not None]
        payments[k] = float(sum(vals)) if vals else None

    # ---- expense
    purchases = db.all("SELECT * FROM purchases WHERE purchase_date = ? AND status = 'confirmed' ORDER BY id", (day,))
    by_cat: dict[str, float] = defaultdict(float)
    purchase_rows = []
    expense_known = []
    for p in purchases:
        items = db.all("SELECT pi.*, pr.name AS product_name FROM purchase_items pi "
                       "LEFT JOIN products pr ON pr.id = pi.product_id WHERE purchase_id = ? ORDER BY pi.id", (p["id"],))
        item_sum = 0.0
        for it in items:
            if it["amount"] is not None:
                by_cat[it["category"]] += it["amount"]
                item_sum += it["amount"]
            purchase_rows.append({
                "date": p["purchase_date"], "name": it["product_name"] or it["raw_name"], "raw_name": it["raw_name"],
                "category": CATEGORY_LABEL[it["category"]], "quantity": it["quantity"], "unit": it["unit"] or it["unit_raw"],
                "unit_price": it["unit_price"], "unit_price_source": it["unit_price_source"],
                "amount": it["amount"], "amount_source": it["amount_source"], "supplier": p["supplier"],
                "ref": _ref("M", p), "message_id": p["message_id"], "evidence": it["evidence"],
                "status": it["status"], "status_reason": it["status_reason"],
            })
        total = purchase_total(p)
        if total is not None:
            expense_known.append(total)
            if total - item_sum > 0.5:
                by_cat["CHUA_PHAN_LOAI"] += total - item_sum
    expense = float(sum(expense_known)) if expense_known else None
    net = revenue - expense if revenue is not None and expense is not None else None

    # ---- sales items
    sale_rows = []
    counted_ids = {r["id"] for r in counted}
    for s in counted + excluded:
        for it in db.all("SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (s["id"],)):
            sale_rows.append({
                "date": s["sale_date"], "name": it["raw_name"], "quantity": it["quantity"], "unit_price": it["unit_price"],
                "amount": it["amount"], "ref": _ref("B", s), "message_id": s["message_id"], "evidence": it["evidence"],
                "counted": s["id"] in counted_ids,
            })

    # ---- prices recorded that day
    changes, first_prices = [], []
    for ph in db.all("SELECT ph.*, p.name FROM price_history ph JOIN products p ON p.id = ph.product_id "
                     "WHERE ph.price_date = ? ORDER BY ph.id", (day,)):
        ch = change_for(db, ph["id"])
        if ch is None:
            first_prices.append({"name": ph["name"], "price": ph["unit_price_base"], "unit": ph["base_unit"]})
        elif ch.direction != "SAME":
            changes.append(ch)

    # ---- alerts, pending, missing
    alerts = [dict(r) for r in db.all(
        "SELECT * FROM alerts WHERE alert_date = ? AND resolved = 0 ORDER BY "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id", (day,))]
    pending = [dict(r) | {"ref": "M" + str(r["id"]), "date": r["purchase_date"], "amount": purchase_total(r)}
               for r in db.all("SELECT * FROM purchases WHERE status = 'needs_confirmation' AND purchase_date <= ?", (day,))]
    pending += [dict(r) | {"ref": "B" + str(r["id"]), "date": r["sale_date"], "amount": r["revenue"]}
                for r in db.all("SELECT * FROM sales WHERE status = 'needs_confirmation' AND sale_date <= ?", (day,))]

    missing = []
    if revenue is None:
        missing.append("Chưa có dữ liệu doanh thu hôm nay.")
    if not purchases:
        missing.append("Chưa có dữ liệu chi hôm nay.")
    no_price = [r["raw_name"] + f" ({r['ref']})" for r in purchase_rows if r["amount"] is None]
    if no_price:
        missing.append("Chưa có giá: " + ", ".join(no_price))
    # unknown units are already listed via their UNIT_UNKNOWN alerts
    no_supplier = [p for p in purchases if not p["supplier"] and p["doc_type"] == "PURCHASE_BILL"]
    if no_supplier:
        missing.append(f"{len(no_supplier)} bill chưa xác định được nhà cung cấp.")
    month_start = day[:8] + "01"
    other_names = [norm_key(r["raw_name"]).split() for r in db.all(
        """SELECT pi.raw_name FROM purchase_items pi JOIN purchases p ON p.id = pi.purchase_id
           WHERE p.status = 'confirmed' AND p.purchase_date BETWEEN ? AND ? AND pi.category = 'CHI_KHAC'""",
        (month_start, day))]
    for key, label in UTILITY_KEYWORDS.items():
        if not any(key in words for words in other_names):
            missing.append(f"Chưa có dữ liệu chi phí {label} tháng này.")

    return DailySummary(
        day=day, revenue=revenue, revenue_refs=[_ref("B", r) for r in counted], revenue_note=rev_note,
        expense=expense, expense_by_category=dict(by_cat), expense_refs=[_ref("M", p) for p in purchases],
        net=net, bill_count=bill_count, transaction_count=len(counted) + len(excluded) + len(purchases),
        payments=payments, purchase_rows=purchase_rows, sale_rows=sale_rows, price_changes=changes,
        first_prices=first_prices, top_items=top_items_in_range(db, day, day), alerts=alerts,
        pending=pending, missing=missing,
    )
