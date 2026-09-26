"""Founder Q&A. The AI only classifies the question (intent/period/product);
every number in the answer comes from a SQL query over confirmed records, with its sources."""
from __future__ import annotations

from datetime import date, timedelta

from .analytics import expense_in_range, revenue_in_range, top_items_in_range
from .db import DB
from .extraction import QuestionIntent
from .money import fmt_pct, fmt_signed_vnd, fmt_vnd, pct_change
from .pricing import last_price
from .products import ProductMaster
from .report import render_text
from .analytics import daily_summary


def period_range(period: str, today: date, specific: str | None = None) -> tuple[str, str, str]:
    if period == "YESTERDAY":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat(), f"hôm qua ({d:%d/%m})"
    if period == "THIS_WEEK":
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), f"tuần này ({start:%d/%m}–{today:%d/%m})"
    if period == "LAST_7_DAYS":
        start = today - timedelta(days=6)
        return start.isoformat(), today.isoformat(), f"7 ngày qua ({start:%d/%m}–{today:%d/%m})"
    if period == "THIS_MONTH":
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), f"tháng {today:%m/%Y} (đến {today:%d/%m})"
    if period == "LAST_MONTH":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1).isoformat(), end.isoformat(), f"tháng {end:%m/%Y}"
    if period == "LAST_30_DAYS":
        start = today - timedelta(days=29)
        return start.isoformat(), today.isoformat(), "30 ngày qua"
    if period == "DATE" and specific:
        try:
            d = date.fromisoformat(specific)
            return d.isoformat(), d.isoformat(), f"ngày {d:%d/%m/%Y}"
        except ValueError:
            pass
    return today.isoformat(), today.isoformat(), f"hôm nay ({today:%d/%m})"


def _refs(refs: list[str], limit: int = 8) -> str:
    if not refs:
        return ""
    more = f" +{len(refs) - limit}" if len(refs) > limit else ""
    return "\nNguồn: " + ", ".join(refs[:limit]) + more


class Chatbot:
    def __init__(self, db: DB):
        self.db = db
        self.products = ProductMaster(db)

    def answer(self, q: QuestionIntent, today: date) -> str:
        start, end, label = period_range(q.period, today, q.date)
        handler = {
            "EXPENSE_TOTAL": self._expense, "REVENUE_TOTAL": self._revenue, "NET_TOTAL": self._net,
            "PRODUCT_LAST_PRICE": self._last_price, "PRODUCT_PRICE_CHANGE": self._price_change,
            "PRODUCT_SPEND": self._product_spend, "TOP_ITEMS": self._top_items, "REPORT": self._report,
        }.get(q.intent)
        if handler is None:
            return ("Tôi chỉ trả lời được câu hỏi về số liệu đã ghi: chi, doanh thu, thu – chi, giá hàng, "
                    "món bán chạy, báo cáo ngày.")
        return handler(q, start, end, label)

    def _expense(self, q, start, end, label):
        total, refs = expense_in_range(self.db, start, end)
        if total is None:
            return f"Chưa có dữ liệu chi {label}."
        return f"💸 Chi {label}: {fmt_vnd(total)} ({len(refs)} chứng từ đã xác nhận).{_refs(refs)}"

    def _revenue(self, q, start, end, label):
        total, days = revenue_in_range(self.db, start, end)
        if total is None:
            return f"Chưa có dữ liệu doanh thu {label}."
        return f"💰 Doanh thu {label}: {fmt_vnd(total)} (dữ liệu của {days} ngày)."

    def _net(self, q, start, end, label):
        rev, days = revenue_in_range(self.db, start, end)
        exp, _ = expense_in_range(self.db, start, end)
        if rev is None or exp is None:
            return (f"Chưa đủ dữ liệu để tính thu – chi {label} "
                    f"(doanh thu: {fmt_vnd(rev)}, chi: {fmt_vnd(exp)}).")
        return (f"📈 Thu – chi {label}: {fmt_signed_vnd(rev - exp)}\n"
                f"Doanh thu {fmt_vnd(rev)} − Chi {fmt_vnd(exp)}.\n(Chưa phải lợi nhuận ròng.)")

    def _product(self, q):
        if not q.product:
            return None, "Anh/chị hỏi mặt hàng nào?"
        pid = self.products.match(q.product)
        if pid is None:
            return None, f"Chưa có dữ liệu cho mặt hàng '{q.product}'."
        return pid, None

    def _last_price(self, q, start, end, label):
        pid, err = self._product(q)
        if err:
            return err
        r = last_price(self.db, pid)
        if r is None:
            return f"Chưa có dữ liệu giá {self.products.name(pid)}."
        src = f"M{self._purchase_of(r['purchase_item_id'])}" if r["purchase_item_id"] else "bảng giá"
        return (f"🛒 {self.products.name(pid)} lần gần nhất: {fmt_vnd(r['unit_price_base'])}/{r['base_unit']} "
                f"ngày {date.fromisoformat(r['price_date']):%d/%m/%Y}"
                + (f", NCC {r['supplier']}" if r["supplier"] else "") + f".\nNguồn: {src}")

    def _purchase_of(self, item_id):
        r = self.db.one("SELECT purchase_id FROM purchase_items WHERE id = ?", (item_id,))
        return r["purchase_id"] if r else "?"

    def _price_change(self, q, start, end, label):
        pid, err = self._product(q)
        if err:
            return err
        rows = self.db.all(
            """SELECT * FROM price_history WHERE product_id = ? AND price_date BETWEEN ? AND ?
               ORDER BY base_unit, price_date, id""", (pid, start, end))
        if not rows:
            return f"Chưa có dữ liệu giá {self.products.name(pid)} {label}."
        unit = rows[-1]["base_unit"]
        rows = [r for r in rows if r["base_unit"] == unit]
        if len(rows) < 2:
            return (f"{self.products.name(pid)} {label} mới có 1 lần ghi giá: "
                    f"{fmt_vnd(rows[0]['unit_price_base'])}/{unit}. Không đủ dữ liệu để so sánh.")
        first, last = rows[0], rows[-1]
        diff = last["unit_price_base"] - first["unit_price_base"]
        return (f"🛒 {self.products.name(pid)} {label}: {fmt_vnd(first['unit_price_base'])} ({first['price_date']}) → "
                f"{fmt_vnd(last['unit_price_base'])}/{unit} ({last['price_date']})\n"
                f"Chênh lệch: {fmt_signed_vnd(diff)}/{unit} ({fmt_pct(pct_change(first['unit_price_base'], last['unit_price_base']))}), "
                f"{len(rows)} lần ghi giá.")

    def _product_spend(self, q, start, end, label):
        pid, err = self._product(q)
        if err:
            return err
        rows = self.db.all(
            """SELECT pi.amount, pi.quantity_base, pi.base_unit, p.id AS pid FROM purchase_items pi
               JOIN purchases p ON p.id = pi.purchase_id
               WHERE pi.product_id = ? AND p.status = 'confirmed' AND p.purchase_date BETWEEN ? AND ?""",
            (pid, start, end))
        if not rows:
            return f"Chưa có dữ liệu mua {self.products.name(pid)} {label}."
        known = [r["amount"] for r in rows if r["amount"] is not None]
        qty = {}
        for r in rows:
            if r["quantity_base"] is not None:
                qty[r["base_unit"]] = qty.get(r["base_unit"], 0) + r["quantity_base"]
        q_txt = ", ".join(f"{v:g} {u}" for u, v in qty.items())
        miss = len(rows) - len(known)
        return (f"🛒 Tiền {self.products.name(pid)} {label}: {fmt_vnd(sum(known))}"
                + (f" ({q_txt})" if q_txt else "") + f", {len(rows)} lần mua."
                + (f"\n⚠️ {miss} lần mua chưa có giá, chưa cộng." if miss else "")
                + _refs(sorted({f"M{r['pid']}" for r in rows})))

    def _top_items(self, q, start, end, label):
        items = top_items_in_range(self.db, start, end)
        if not items:
            return f"Chưa có dữ liệu món bán {label}."
        lines = [f"🔥 Bán chạy {label}:"]
        lines += [f"{i}. {n} — {qty:g} phần" for i, (n, qty, _) in enumerate(items, 1)]
        return "\n".join(lines)

    def _report(self, q, start, end, label):
        return render_text(self.db, daily_summary(self.db, end))
