"""Deterministic validation of AI extractions. The model reads; this module checks and computes.

Each item/document ends up with:
  * computed fields (unit price, amount) marked `computed` vs `source`
  * a list of Issues. `critical` issues keep the record OUT of reports until the Founder confirms.
"""
from __future__ import annotations

import hashlib
import re
import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from .extraction import GOODS_CATEGORIES, ExtractedItem, Extraction
from .money import amounts_match, fmt_vnd, money_text_matches
from .textnorm import compact, norm_key
from .units import Unit, normalize_unit


APPROX_RE = re.compile(r"(?<!\w)(khoảng|tầm|chừng|ước chừng|ước tính|xấp xỉ|gần|hơn)(?!\w)")


@dataclass
class Issue:
    severity: str   # critical | warning | info
    code: str
    message: str


@dataclass
class CheckedItem:
    raw: ExtractedItem
    quantity: float | None
    unit: Unit | None
    unit_price: float | None
    unit_price_source: str | None
    amount: float | None
    amount_source: str | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "critical" for i in self.issues) and not self.unit_problem

    @property
    def unit_problem(self) -> bool:
        return any(i.code == "UNIT_UNKNOWN" for i in self.issues)

    @property
    def quantity_base(self) -> float | None:
        if self.quantity is None or self.unit is None:
            return None
        return self.quantity * self.unit.factor

    @property
    def unit_price_base(self) -> float | None:
        if self.unit_price is None or self.unit is None:
            return None
        return self.unit_price / self.unit.factor


@dataclass
class CheckedDocument:
    doc_date: str
    date_source: str
    items: list[CheckedItem]
    items_total: float | None          # sum of item amounts if ALL known
    known_items_total: float           # sum of known item amounts
    stated_total: float | None
    issues: list[Issue] = field(default_factory=list)

    @property
    def critical(self) -> list[Issue]:
        out = [i for i in self.issues if i.severity == "critical"]
        for it in self.items:
            out += [i for i in it.issues if i.severity == "critical"]
        return out

    def all_issues(self) -> list[Issue]:
        out = list(self.issues)
        for it in self.items:
            out += it.issues
        return out


def _fmt_qty(q: float | None) -> str:
    if q is None:
        return "?"
    return (f"{q:.3f}".rstrip("0").rstrip(".")).replace(".", ",")


def grounded(snippet: str | None, source: str | None) -> bool:
    """True when the snippet appears (accent/space-insensitive) in the source text."""
    if not snippet or not source:
        return False
    return compact(snippet) in compact(source)


def number_grounded(value: float, raw_text: str | None, source: str | None) -> bool:
    """The number must be traceable to the source: its raw text appears in the source and parses to it."""
    if raw_text and grounded(raw_text, source):
        return money_text_matches(raw_text, value) is not False
    # fall back: the digits themselves are present in the source
    digits = str(int(round(value)))
    src = compact(source or "")
    variants = {digits}
    if value >= 1000 and value % 1000 == 0:
        variants.add(str(int(value // 1000)))  # written as '450k' / '450 ngàn'
    return any(v in src for v in variants)


def resolve_date(raw: str | None, received: date, max_backdate_days: int) -> tuple[str, str, list[Issue]]:
    if not raw:
        return received.isoformat(), "message", []
    try:
        d = date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return received.isoformat(), "message", [
            Issue("critical", "DATE_INVALID", f"Ngày trên chứng từ không hợp lệ ('{raw}').")
        ]
    if d > received:
        return d.isoformat(), "document", [
            Issue("critical", "DATE_FUTURE", f"Ngày trên chứng từ ({d:%d/%m/%Y}) nằm trong tương lai.")
        ]
    if d < received - timedelta(days=max_backdate_days):
        return d.isoformat(), "document", [
            Issue("critical", "DATE_TOO_OLD", f"Ngày trên chứng từ ({d:%d/%m/%Y}) cũ hơn {max_backdate_days} ngày.")
        ]
    return d.isoformat(), "document", []


def check_item(it: ExtractedItem, source_text: str | None, tol_vnd: float, tol_pct: float,
               require_unit: bool = True) -> CheckedItem:
    issues: list[Issue] = []
    label = it.name

    # 1. grounding — the line must exist in the source
    if source_text is not None and not grounded(it.evidence, source_text):
        issues.append(Issue("critical", "NOT_GROUNDED",
                            f"'{label}': không tìm thấy dòng tương ứng trong nguồn gốc."))

    # 2. numbers must be traceable to the source text
    amount, unit_price = it.amount, it.unit_price
    for value, raw, what in ((amount, it.amount_text, "thành tiền"), (unit_price, it.unit_price_text, "đơn giá")):
        if value is None:
            continue
        if value < 0:
            issues.append(Issue("critical", "NEGATIVE", f"'{label}': {what} âm ({fmt_vnd(value)})."))
        if raw:
            if money_text_matches(raw, value) is False:
                issues.append(Issue("critical", "NUMBER_MISMATCH",
                                    f"'{label}': {what} đọc là {fmt_vnd(value)} nhưng nguồn ghi '{raw}'."))
        if source_text is not None and not number_grounded(value, raw, source_text):
            issues.append(Issue("critical", "NUMBER_NOT_GROUNDED",
                                f"'{label}': {what} {fmt_vnd(value)} không có trong nguồn gốc."))

    # an estimate said/written by the Founder is not an exact fact -> hold for confirmation
    approx_in = " ".join(filter(None, [it.evidence, it.amount_text, it.unit_price_text])).lower()
    if (amount is not None or unit_price is not None) and APPROX_RE.search(approx_in):
        issues.append(Issue("critical", "APPROXIMATE",
                            f"'{label}': số tiền là ước lượng ('{APPROX_RE.search(approx_in).group(0).strip()}'), cần xác nhận số chính xác."))

    qty = it.quantity
    if qty is not None and qty <= 0:
        issues.append(Issue("critical", "QTY_INVALID", f"'{label}': số lượng không hợp lệ ({qty})."))
        qty = None

    unit = normalize_unit(it.unit)
    # only goods are price-tracked, so only goods need a known unit (gas 'bình', 'tháng' lương... do not)
    if require_unit and it.category in GOODS_CATEGORIES and qty is not None and unit is None:
        issues.append(Issue("warning", "UNIT_UNKNOWN",
                            f"'{label}': không xác định được đơn vị ('{it.unit or 'trống'}') — cần xác nhận, chưa so sánh giá."))

    # 3. arithmetic (done here, never by the model)
    up_src = "source" if unit_price is not None else None
    am_src = "source" if amount is not None else None
    if qty is not None and unit_price is not None and amount is not None:
        expected = qty * unit_price
        if not amounts_match(expected, amount, tol_vnd, tol_pct):
            u = unit.canonical if unit else (it.unit or "")
            issues.append(Issue("critical", "LINE_MISMATCH",
                                f"Bill ghi {label} {_fmt_qty(qty)}{u} × {fmt_vnd(unit_price)} = {fmt_vnd(amount)}. "
                                f"Tổng tiền không khớp phép tính (đúng ra {fmt_vnd(expected)})."))
    elif qty is not None and amount is not None and unit_price is None:
        unit_price, up_src = amount / qty, "computed"
    elif qty is not None and unit_price is not None and amount is None:
        amount, am_src = qty * unit_price, "computed"

    if amount is None:
        issues.append(Issue("warning", "AMOUNT_MISSING", f"'{label}': nguồn không ghi tiền — để UNKNOWN."))

    return CheckedItem(it, qty, unit, unit_price, up_src, amount, am_src, issues)


def check_document(ex: Extraction, source_text: str | None, received: date, *,
                   tol_vnd: float, tol_pct: float, max_backdate_days: int,
                   require_unit: bool = True) -> CheckedDocument:
    doc_date, date_source, issues = resolve_date(ex.document_date, received, max_backdate_days)
    items = [check_item(it, source_text, tol_vnd, tol_pct, require_unit) for it in ex.items]

    known = [i.amount for i in items if i.amount is not None]
    known_total = float(sum(known))
    items_total = known_total if items and len(known) == len(items) else None

    stated = ex.stated_total
    if stated is not None:
        if ex.stated_total_text:
            if money_text_matches(ex.stated_total_text, stated) is False:
                issues.append(Issue("critical", "NUMBER_MISMATCH",
                                    f"Tổng đọc là {fmt_vnd(stated)} nhưng nguồn ghi '{ex.stated_total_text}'."))
        if source_text is not None and not number_grounded(stated, ex.stated_total_text, source_text):
            issues.append(Issue("critical", "NUMBER_NOT_GROUNDED", f"Tổng {fmt_vnd(stated)} không có trong nguồn gốc."))
        if items_total is not None and not amounts_match(items_total, stated, tol_vnd, tol_pct):
            issues.append(Issue("critical", "TOTAL_MISMATCH",
                                f"Tổng tiền không khớp: bill ghi {fmt_vnd(stated)} nhưng cộng các dòng được {fmt_vnd(items_total)}."))
        elif items_total is None and known and known_total > stated + tol_vnd:
            issues.append(Issue("critical", "TOTAL_MISMATCH",
                                f"Tổng tiền không khớp: các dòng đã cộng {fmt_vnd(known_total)}, vượt tổng bill {fmt_vnd(stated)}."))

    if ex.unreadable_parts:
        issues.append(Issue("warning", "UNREADABLE", "Phần đọc không rõ: " + "; ".join(ex.unreadable_parts[:5])))

    return CheckedDocument(doc_date, date_source, items, items_total, known_total, stated, issues)


def fingerprint(kind: str, doc_date: str, supplier: str | None, total: float | None, items: list[CheckedItem]) -> str:
    payload = {
        "k": kind, "d": doc_date, "s": norm_key(supplier or ""),
        "t": round(total) if total is not None else None,
        "i": sorted([norm_key(i.raw.name), i.quantity, round(i.amount) if i.amount is not None else None]
                    for i in items),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
