"""Aggregations over CONFIRMED records only. Every figure carries references to its sources.

Revenue source rule (documented in the report):
  PRIMARY_REVENUE_SOURCE (confirmed by the Founder) is the authoritative revenue source:
    pos_closing -> revenue = POS / chốt ca reports; single sales bills only reconcile & feed item stats
    sales_bills -> revenue = sum of sales bills; POS reports only reconcile
  Only one kind present that day -> that kind is the revenue.
  Both present and no primary source configured -> revenue UNKNOWN + alert (never pick one silently).
  Primary vs evidence mismatch -> alert with both figures; numbers are never adjusted.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import os

from .db import DB
from .money import amounts_match, fmt_vnd
from .pricing import PriceChange, change_for
from .textnorm import norm_key

CATEGORY_LABEL = {
    "ingredient": "Nguyên liệu", "packaging": "Bao bì", "gas": "Gas", "transport": "Vận chuyển",
    "platform_fee": "Phí nền tảng", "salary": "Lương", "utilities": "Điện nước", "other": "Chi khác",
    "CHUA_PHAN_LOAI": "Chưa phân loại",
}
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


SOURCE_LABEL = {"pos_closing": "báo cáo chốt ca/POS", "sales_bills": "bill bán lẻ"}


def primary_revenue_source() -> str:
    v = os.environ.get("PRIMARY_REVENUE_SOURCE", "").strip().lower()
    return v if v in SOURCE_LABEL else ""


def _split(db: DB, day: str) -> tuple[list, list]:
    rows = db.all("SELECT * FROM sales WHERE sale_date = ? AND status = 'confirmed' ORDER BY id", (day,))
    return ([r for r in rows if r["doc_type"] == "REVENUE_REPORT"], [r for r in rows if r["doc_type"] == "SALES_BILL"])


def counted_sales(db: DB, day: str) -> tuple[list, list, str | None]:
    """Return (sales rows counted in revenue, rows used only as evidence, note)."""
    reports, bills = _split(db, day)
    if not (reports and bills):
        return reports + bills, [], None
    primary = primary_revenue_source()
    if primary == "pos_closing":
        return reports, bills, (f"Doanh thu theo nguồn chính: báo cáo chốt ca/POS ({', '.join(_ref('B', r) for r in reports)}); "
                                f"{len(bills)} bill lẻ chỉ dùng để đối chiếu, không cộng thêm.")
    if primary == "sales_bills":
        return bills, reports, (f"Doanh thu theo nguồn chính: {len(bills)} bill bán lẻ; "
                                f"báo cáo chốt ca ({', '.join(_ref('B', r) for r in reports)}) chỉ dùng để đối chiếu.")
    return [], reports + bills, ("Có cả báo cáo chốt ca và bill lẻ nhưng chưa xác định nguồn doanh thu chính "
                                 "(PRIMARY_REVENUE_SOURCE) — doanh thu để UNKNOWN, cần Founder xác nhận.")


def reconciliation(db: DB, day: str, tol_vnd: float = 1000.0, tol_pct: float = 0.5) -> dict | None:
    """Compare POS closing vs sum of single bills. Returns None when only one source exists."""
    reports, bills = _split(db, day)
    if not (reports and bills):
        return None
    pos = [r["revenue"] for r in reports if r["revenue"] is not None]
    bil = [r["revenue"] for r in bills if r["revenue"] is not None]
    pos_total, bill_total = (float(sum(pos)) if pos else None), (float(sum(bil)) if bil else None)
    matched = (pos_total is not None and bill_total is not None
               and amounts_match(pos_total, bill_total, tol_vnd, tol_pct))
    out = {"primary": primary_revenue_source() or None, "pos_total": pos_total, "bill_total": bill_total,
           "difference": (pos_total - bill_total) if pos_total is not None and bill_total is not None else None,
           "matched": matched, "pos_refs": [_ref("B", r) for r in reports], "bill_refs": [_ref("B", r) for r in bills]}
    return out


def reconciliation_alerts(rec: dict | None) -> list[dict]:
    if rec is None:
        return []
    alerts = []
    if not rec["primary"]:
        alerts.append({"severity": "critical", "code": "REVENUE_SOURCE_UNSET", "message_id": None,
                       "message": (f"Chưa xác định nguồn doanh thu chính. POS: {fmt_vnd(rec['pos_total'])} "
                                   f"({', '.join(rec['pos_refs'])}); bill lẻ: {fmt_vnd(rec['bill_total'])} "
                                   f"({', '.join(rec['bill_refs'])}). Cần Founder xác nhận.")})
    if not rec["matched"]:
        alerts.append({"severity": "warning", "code": "REVENUE_RECONCILIATION_MISMATCH", "message_id": None,
                       "message": (f"Doanh thu không khớp khi đối chiếu — POS: {fmt_vnd(rec['pos_total'])}; "
                                   f"Bill evidence: {fmt_vnd(rec['bill_total'])}; "
                                   f"Chênh lệch: {fmt_vnd(rec['difference'])}. Cần Founder xác nhận "
                                   f"({', '.join(rec['pos_refs'] + rec['bill_refs'])}).")})
    return alerts


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
    rec = reconciliation(db, day)
    alerts = reconciliation_alerts(rec) + [dict(r) for r in db.all(
        "SELECT * FROM alerts WHERE alert_date = ? AND resolved = 0 ORDER BY "
        "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id", (day,))]
    pending = [dict(r) | {"ref": "M" + str(r["id"]), "date": r["purchase_date"], "amount": purchase_total(r)}
               for r in db.all("SELECT * FROM purchases WHERE status = 'needs_confirmation' AND purchase_date <= ?", (day,))]
    pending += [dict(r) | {"ref": "B" + str(r["id"]), "date": r["sale_date"], "amount": r["revenue"]}
                for r in db.all("SELECT * FROM sales WHERE status = 'needs_confirmation' AND sale_date <= ?", (day,))]

    missing = []
    if revenue is None and not rec:
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
           WHERE p.status = 'confirmed' AND p.purchase_date BETWEEN ? AND ? AND pi.category IN ('utilities', 'other')""",
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
        pending=pending, missing=missing, extra={"reconciliation": rec},
    )
