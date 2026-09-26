"""Message pipeline: Zalo input -> raw storage -> AI read -> validation -> records -> short reply.

Records only count in reports when status = 'confirmed'. Anything with a critical issue or a
possible duplicate is stored as 'needs_confirmation' and the Founder is asked to confirm.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import DB
from .extraction import GOODS_CATEGORIES, Extraction, ExtractionError, Extractor, PROMPT_VERSION, extraction_to_json
from .money import amounts_match, fmt_pct, fmt_vnd
from .pricing import PriceChange, change_for, quantity_is_unusual
from .products import ProductMaster
from .stt import NoTranscriber, STTUnavailable, Transcriber
from .analytics import daily_revenue_history, day_revenue
from .textnorm import norm_key
from .validation import CheckedDocument, Issue, check_document, fingerprint, number_grounded

MediaFetcher = Callable[[str], tuple[bytes, str | None]]

PURCHASE_TYPES = {"PURCHASE_BILL", "EXPENSE"}
SALES_TYPES = {"SALES_BILL", "REVENUE_REPORT"}
DOC_LABEL = {
    "PURCHASE_BILL": "bill mua hàng", "EXPENSE": "khoản chi", "SALES_BILL": "bill bán hàng",
    "REVENUE_REPORT": "báo cáo doanh thu", "PRICE_LIST": "bảng giá", "INVENTORY": "phiếu kho", "OTHER": "thông tin",
}
CONFIRM_HINT = 'Trả lời "ok {ref}" để xác nhận hoặc "hủy {ref}" để bỏ.'


@dataclass
class IncomingMessage:
    zalo_msg_id: str
    user_id: str
    kind: str                       # text | image | audio | file
    received_at: datetime
    text: str | None = None
    media_url: str | None = None
    media_bytes: bytes | None = None
    media_mime: str | None = None
    event_json: dict | None = None


@dataclass
class Result:
    reply: str | None
    message_id: int | None = None
    record_ref: str | None = None       # 'M12' (mua/chi) | 'B7' (bán/doanh thu)
    status: str | None = None
    price_changes: list[PriceChange] = field(default_factory=list)


class Pipeline:
    def __init__(self, db: DB, settings: Settings, extractor: Extractor,
                 transcriber: Transcriber | None = None, fetch_media: MediaFetcher | None = None,
                 answer_question: Callable | None = None):
        self.db = db
        self.s = settings
        self.extractor = extractor
        self.transcriber = transcriber or NoTranscriber()
        self.fetch_media = fetch_media
        self.products = ProductMaster(db)
        self.answer_question = answer_question

    # ------------------------------------------------------------------ entry point
    def process(self, msg: IncomingMessage) -> Result:
        if msg.zalo_msg_id and self.db.one("SELECT id FROM messages WHERE zalo_msg_id = ?", (msg.zalo_msg_id,)):
            return Result(reply=None)  # webhook retry — already handled, stay silent

        now = self._iso(msg.received_at)
        mid = self.db.insert("messages", {
            "zalo_msg_id": msg.zalo_msg_id, "user_id": msg.user_id, "kind": msg.kind, "text": msg.text,
            "media_url": msg.media_url, "media_mime": msg.media_mime,
            "received_at": now, "event_json": json.dumps(msg.event_json, ensure_ascii=False) if msg.event_json else None,
        })
        self.db.commit()

        try:
            result = self._process(mid, msg)
        except Exception as exc:  # never lose the raw message; tell the Founder
            self.db.update("messages", mid, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            self.db.commit()
            raise
        result.message_id = mid
        return result

    def reprocess(self, message_id: int) -> Result:
        """Re-run a stored message (e.g. after an API outage) from its raw data. Raw data is untouched."""
        m = self.db.one("SELECT * FROM messages WHERE id = ?", (message_id,))
        if m is None:
            raise KeyError(message_id)
        msg = IncomingMessage(zalo_msg_id=m["zalo_msg_id"], user_id=m["user_id"], kind=m["kind"],
                              received_at=datetime.fromisoformat(m["received_at"]), text=m["text"],
                              media_url=m["media_url"], media_mime=m["media_mime"])
        self.db.update("messages", message_id, {"status": "reprocessing", "error": None,
                                                "attempts": (m["attempts"] or 1) + 1})
        try:
            result = self._process(message_id, msg)
        except Exception as exc:
            self.db.update("messages", message_id, {"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            self.db.commit()
            raise
        result.message_id = message_id
        return result

    def _process(self, mid: int, msg: IncomingMessage) -> Result:
        received = msg.received_at.date()

        if msg.kind == "text" and msg.text:
            cmd = self._command(mid, msg.text.strip(), received)
            if cmd is not None:
                self._set_msg(mid, "command")
                return cmd

        text, image, mime, source_kind = msg.text, None, None, "text"
        if msg.kind in ("image", "audio", "file"):
            stored = self.db.one("SELECT * FROM media WHERE message_id = ?", (mid,))
            if stored:  # reprocessing: reuse the original file, never re-download
                data, mime = Path(stored["file_path"]).read_bytes(), stored["mime"]
                media_id, sha = stored["id"], stored["sha256"]
            else:
                data, mime = self._load_media(msg)
                media_id, sha = self._store_media(mid, msg, data, mime)
            dup = self._same_file_recorded(sha, media_id)
            if dup:
                self._set_msg(mid, "duplicate")
                return Result(reply=f"⚠️ Ảnh/tệp này đã gửi trước đó và đã ghi nhận ({dup}). Không cộng thêm lần nữa.",
                              record_ref=dup, status="duplicate")
            if msg.kind == "audio":
                source_kind = "voice"
                try:
                    text = self.transcriber.transcribe(data, f"voice-{mid}{self._ext(mime)}", mime)
                except (STTUnavailable, Exception) as exc:  # noqa: BLE001 - any STT failure -> ask to type
                    self.db.update("messages", mid, {"status": "needs_transcription", "error": str(exc)})
                    self.db.commit()
                    return Result(reply="🎤 Đã lưu tin nhắn thoại nhưng chưa chuyển được thành chữ. "
                                        "Anh/chị gõ lại giúp nội dung chính nhé.", status="needs_transcription")
                self.db.update("messages", mid, {"transcript": text})
            elif (mime or "").startswith("image/") or msg.kind == "image":
                image, source_kind = data, "image"
            else:
                self._set_msg(mid, "stored")
                return Result(reply="📎 Đã lưu tệp. V1 chỉ đọc được ảnh, giọng nói và tin nhắn chữ.", status="stored")

        if not text and image is None:
            self._set_msg(mid, "ignored")
            return Result(reply=None)

        try:
            ex = self.extractor.extract(text=text, image=image, image_mime=mime,
                                        received_date=received.isoformat(), source_kind=source_kind)
        except ExtractionError as exc:
            # Founder is asked to resend -> 'unreadable' is NOT auto-retried (a retry + resend would double count)
            self.db.update("messages", mid, {"status": "unreadable", "error": str(exc)})
            self.db.commit()
            return Result(reply=f"⚠️ Chưa đọc được nội dung ({exc}). Anh/chị gửi lại giúp ảnh rõ hơn nhé.",
                          status="unreadable")

        ext_id = self.db.insert("ai_extractions", {
            "message_id": mid, "model": getattr(self.extractor, "model_name", "unknown"),
            "prompt_version": PROMPT_VERSION, "input_text": text, "ocr_text": ex.ocr_text,
            "doc_type": ex.doc_type, "raw_json": extraction_to_json(ex), "created_at": self._iso(msg.received_at),
        })
        self.db.commit()

        # Ground numbers in the text the Founder actually sent/said; for images, in the OCR transcript.
        # An image with no OCR transcript grounds against "" -> every number fails and is held for confirmation.
        source_text = text if source_kind in ("text", "voice") else "\n".join(filter(None, [ex.ocr_text, text]))

        if ex.doc_type == "QUESTION":
            self._set_msg(mid, "processed")
            if self.answer_question and ex.question:
                return Result(reply=self.answer_question(ex.question, received), status="answered")
            return Result(reply="Tôi chưa hiểu câu hỏi, anh/chị hỏi lại giúp nhé.", status="answered")
        if ex.doc_type in PURCHASE_TYPES:
            res = self._record_purchase(mid, ext_id, ex, source_text, received, source_kind)
        elif ex.doc_type in SALES_TYPES:
            res = self._record_sale(mid, ext_id, ex, source_text, received)
        elif ex.doc_type == "PRICE_LIST":
            res = self._record_price_list(mid, ext_id, ex, source_text, received)
        else:
            res = Result(reply=f"📝 Đã lưu {DOC_LABEL.get(ex.doc_type, 'thông tin')}. "
                               "Loại này chưa được đưa vào báo cáo thu – chi.", status="stored")
        self._set_msg(mid, "processed")
        return res

    # ------------------------------------------------------------------ purchases / expenses
    def _record_purchase(self, mid: int, ext_id: int, ex: Extraction, source_text: str | None,
                         received: date, source_kind: str) -> Result:
        doc = self._check(ex, source_text, received)
        doc.issues += self._price_outliers(doc)
        now = self._now_iso()
        fp = fingerprint("purchase", doc.doc_date, ex.supplier, doc.stated_total or doc.items_total, doc.items)
        dup = self.db.one(
            "SELECT id FROM purchases WHERE fingerprint = ? AND status IN ('confirmed','needs_confirmation') ORDER BY id LIMIT 1",
            (fp,),
        )
        status, reason = self._decide_status(doc, dup and f"M{dup['id']}")
        if not doc.items and doc.stated_total is None:
            status, reason = "needs_confirmation", "Không đọc được mặt hàng hay số tiền nào."

        pid = self.db.insert("purchases", {
            "extraction_id": ext_id, "message_id": mid, "doc_type": ex.doc_type,
            "purchase_date": doc.doc_date, "date_source": doc.date_source, "supplier": ex.supplier,
            "stated_total": doc.stated_total, "computed_total": doc.items_total if doc.items_total is not None else (doc.known_items_total or None),
            "status": status, "status_reason": reason, "fingerprint": fp,
            "duplicate_of": dup["id"] if dup else None, "created_at": now,
        })
        new_products = []
        for ci in doc.items:
            pid_product = None
            if ci.raw.category in GOODS_CATEGORIES:
                pid_product, created = self.products.resolve(ci.raw.name, ci.unit.canonical if ci.unit else None, now)
                if created:
                    new_products.append(ci.raw.name)
            item_reason = "; ".join(i.message for i in ci.issues if i.severity != "info") or None
            self.db.insert("purchase_items", {
                "purchase_id": pid, "product_id": pid_product, "raw_name": ci.raw.name, "category": ci.raw.category,
                "quantity": ci.quantity, "unit_raw": ci.raw.unit,
                "unit": ci.unit.canonical if ci.unit else None, "base_unit": ci.unit.base if ci.unit else None,
                "quantity_base": ci.quantity_base, "unit_price": ci.unit_price,
                "unit_price_source": ci.unit_price_source, "unit_price_base": ci.unit_price_base,
                "amount": ci.amount, "amount_source": ci.amount_source, "evidence": ci.raw.evidence,
                "status": "ok" if ci.ok else "needs_confirmation", "status_reason": item_reason,
            })
        ref = f"M{pid}"
        self._store_issues(doc.all_issues(), doc.doc_date, mid, "purchases", pid, ref)
        if not ex.supplier and source_kind == "image" and ex.doc_type == "PURCHASE_BILL":
            self._alert(doc.doc_date, "info", "SUPPLIER_UNKNOWN", f"Bill {ref} chưa xác định được nhà cung cấp.", mid, "purchases", pid)
        for name in new_products:
            self._alert(doc.doc_date, "info", "NEW_PRODUCT",
                        f"Mặt hàng mới '{name}' ({ref}). Nếu trùng mặt hàng cũ, nhắn: gộp {name} = <tên cũ>", mid, "purchases", pid)

        changes = self._apply_prices(pid) if status == "confirmed" else []
        self.db.commit()
        return Result(reply=self._purchase_reply(ex, doc, ref, status, reason, changes, new_products),
                      record_ref=ref, status=status, price_changes=changes)

    OUTLIER_RATIO = 3.0

    def _price_outliers(self, doc: CheckedDocument) -> list[Issue]:
        """A unit price >= 3x or <= 1/3 of the last known price of the same product/unit is the signature of a
        misread zero (90.000 read as 900.000). Consistent misreads pass every arithmetic check, so hold them."""
        out = []
        for ci in doc.items:
            if ci.raw.category not in GOODS_CATEGORIES or ci.unit is None or ci.unit_price_base is None:
                continue
            pid = self.products.match(ci.raw.name)
            prev = pid and self.db.one(
                """SELECT unit_price_base, price_date FROM price_history WHERE product_id = ? AND base_unit = ?
                   AND price_date <= ? ORDER BY price_date DESC, id DESC LIMIT 1""", (pid, ci.unit.base, doc.doc_date))
            if not prev or not prev["unit_price_base"]:
                continue
            ratio = ci.unit_price_base / prev["unit_price_base"]
            if ratio >= self.OUTLIER_RATIO or ratio <= 1 / self.OUTLIER_RATIO:
                out.append(Issue("critical", "PRICE_OUTLIER",
                                 f"'{ci.raw.name}': giá {fmt_vnd(ci.unit_price_base)}/{ci.unit.base} gấp {ratio:.1f} lần "
                                 f"lần trước ({fmt_vnd(prev['unit_price_base'])}, {prev['price_date']}) — có thể đọc sai số 0."))
        return out

    def _apply_prices(self, purchase_id: int) -> list[PriceChange]:
        """Write price_history for a confirmed purchase and return detected price changes."""
        p = self.db.one("SELECT * FROM purchases WHERE id = ?", (purchase_id,))
        rows = self.db.all(
            """SELECT * FROM purchase_items WHERE purchase_id = ? AND product_id IS NOT NULL
               AND base_unit IS NOT NULL AND unit_price_base IS NOT NULL AND status = 'ok' ORDER BY id""",
            (purchase_id,),
        )
        changes = []
        for r in rows:
            if self.db.one("SELECT id FROM price_history WHERE purchase_item_id = ?", (r["id"],)):
                continue
            ph = self.db.insert("price_history", {
                "product_id": r["product_id"], "price_date": p["purchase_date"], "base_unit": r["base_unit"],
                "unit_price_base": r["unit_price_base"], "quantity_base": r["quantity_base"], "supplier": p["supplier"],
                "source": "purchase", "purchase_item_id": r["id"], "extraction_id": p["extraction_id"],
                "evidence": r["evidence"], "created_at": self._now_iso(),
            })
            ch = change_for(self.db, ph)
            if ch and ch.direction != "SAME":
                changes.append(ch)
                self._price_alert(ch, p["message_id"], f"M{purchase_id}")
            median = quantity_is_unusual(self.db, r["product_id"], r["base_unit"], r["quantity_base"], ph)
            if median is not None:
                self._alert(p["purchase_date"], "warning", "QTY_UNUSUAL",
                            f"{self.products.name(r['product_id'])}: số lượng {r['quantity_base']:g}{r['base_unit']} "
                            f"khác thường (thường ~{median:g}{r['base_unit']}) — M{purchase_id}.",
                            p["message_id"], "purchases", purchase_id)
        return changes

    def _price_alert(self, ch: PriceChange, message_id: int, ref: str) -> None:
        strong = abs(ch.pct) >= self.s.price_alert_pct
        verb = "tăng" if ch.diff > 0 else "giảm"
        text = (f"{ch.product_name} {verb} {fmt_vnd(abs(ch.diff))}/{ch.base_unit} ({fmt_pct(ch.pct)}): "
                f"{fmt_vnd(ch.old_price)} → {fmt_vnd(ch.new_price)}/{ch.base_unit} (so với {date.fromisoformat(ch.old_date):%d/%m}) — {ref}")
        if strong:
            text = f"Giá {verb} quá mạnh: " + text
        self._alert(ch.new_date, "warning" if strong or ch.diff > 0 else "info",
                    "PRICE_UP" if ch.diff > 0 else "PRICE_DOWN", text, message_id, "price_history", ch.new_ref)

    def _purchase_reply(self, ex: Extraction, doc: CheckedDocument, ref: str, status: str,
                        reason: str | None, changes: list[PriceChange], new_products: list[str] = ()) -> str:
        label = DOC_LABEL[ex.doc_type]
        total = doc.stated_total if doc.stated_total is not None else doc.items_total
        lines = []
        if status == "confirmed":
            lines.append(f"✅ Đã ghi nhận {label} ({ref}).")
            lines.append(f"Tổng: {fmt_vnd(total) if total is not None else 'UNKNOWN (thiếu giá)'}")
            if doc.items:
                lines.append(f"{len(doc.items)} mặt hàng.")
        else:
            lines.append(f"⚠️ Tôi đọc được {label} ({ref}) nhưng chưa đưa vào báo cáo cho đến khi xác nhận.")
            lines.append(f"Lý do: {reason}")
            if total is not None:
                lines.append(f"Tổng đọc được: {fmt_vnd(total)}")
            lines.append(CONFIRM_HINT.format(ref=ref))
        for ch in changes:
            verb = "tăng" if ch.diff > 0 else "giảm"
            icon = "⚠️" if ch.diff > 0 else "↓"
            lines.append(f"{icon} {ch.product_name} {verb} {fmt_pct(ch.pct).lstrip('+-')} so với lần mua trước "
                         f"({fmt_vnd(ch.old_price)} → {fmt_vnd(ch.new_price)}/{ch.base_unit}).")
        missing = [ci.raw.name for ci in doc.items if ci.amount is None]
        if missing:
            lines.append(f"❓ Chưa có giá: {', '.join(missing)}. Nhắn thêm giá nếu cần.")
        if new_products:
            lines.append(f"🆕 Mặt hàng mới: {', '.join(new_products)}. Nếu trùng mặt hàng cũ, nhắn: gộp {new_products[0]} = <tên cũ>")
        unit_unknown = [ci.raw.name for ci in doc.items if ci.unit_problem]
        if unit_unknown:
            lines.append(f"❓ Chưa rõ đơn vị: {', '.join(unit_unknown)} — chưa so sánh giá.")
        return "\n".join(lines)

    # ------------------------------------------------------------------ sales / revenue
    def _record_sale(self, mid: int, ext_id: int, ex: Extraction, source_text: str | None, received: date) -> Result:
        doc = self._check(ex, source_text, received, require_unit=False)
        rv = ex.revenue
        issues: list[Issue] = []
        gross = rv.gross_revenue if rv and rv.gross_revenue is not None else doc.stated_total
        if gross is not None and doc.items_total is not None and doc.items and not amounts_match(
                doc.items_total, gross, self.s.amount_tolerance_vnd, self.s.amount_tolerance_pct):
            # a revenue report may legitimately list only some items; a single bill must add up
            sev = "critical" if ex.doc_type == "SALES_BILL" else "warning"
            issues.append(Issue(sev, "TOTAL_MISMATCH",
                                f"Doanh thu ghi {fmt_vnd(gross)} nhưng cộng các món được {fmt_vnd(doc.items_total)}."))
        if rv:
            pays = [v for v in (rv.cash, rv.bank_transfer, rv.e_wallet) if v is not None]
            if gross is not None and pays:
                s = sum(pays)
                if s > gross + self.s.amount_tolerance_vnd:
                    issues.append(Issue("critical", "PAYMENT_MISMATCH",
                                        f"Tiền mặt + chuyển khoản + ví ({fmt_vnd(s)}) lớn hơn doanh thu ({fmt_vnd(gross)})."))
                elif len(pays) == 3 and not amounts_match(s, gross, self.s.amount_tolerance_vnd, self.s.amount_tolerance_pct):
                    issues.append(Issue("warning", "PAYMENT_MISMATCH",
                                        f"Tổng các hình thức thanh toán ({fmt_vnd(s)}) khác doanh thu ({fmt_vnd(gross)})."))
            if source_text is not None:
                for v, label in ((rv.gross_revenue, "Doanh thu"), (rv.cash, "Tiền mặt"),
                                 (rv.bank_transfer, "Chuyển khoản"), (rv.e_wallet, "Ví điện tử"), (rv.platform_fee, "Phí nền tảng")):
                    if v is not None and not number_grounded(v, None, source_text):
                        issues.append(Issue("critical", "NUMBER_NOT_GROUNDED", f"{label} {fmt_vnd(v)} không có trong nguồn gốc."))
        doc.issues += issues

        revenue, rsrc = (gross, "source") if gross is not None else (doc.items_total, "computed")
        now = self._now_iso()
        fp = fingerprint(ex.doc_type, doc.doc_date, None, revenue, doc.items)
        dup = self.db.one(
            "SELECT id FROM sales WHERE fingerprint = ? AND status IN ('confirmed','needs_confirmation') ORDER BY id LIMIT 1",
            (fp,),
        )
        status, reason = self._decide_status(doc, dup and f"B{dup['id']}")
        if revenue is None:
            status, reason = "needs_confirmation", "Không xác định được doanh thu (nguồn không ghi tổng và thiếu tiền món)."

        sid = self.db.insert("sales", {
            "extraction_id": ext_id, "message_id": mid, "doc_type": ex.doc_type, "sale_date": doc.doc_date,
            "date_source": doc.date_source, "gross_revenue": gross, "computed_items_total": doc.items_total,
            "revenue": revenue, "revenue_source": rsrc if revenue is not None else None,
            "bill_count": rv.bill_count if rv else None, "item_count": rv.item_count if rv else None,
            "cash": rv.cash if rv else None, "bank_transfer": rv.bank_transfer if rv else None,
            "e_wallet": rv.e_wallet if rv else None, "platform_fee": rv.platform_fee if rv else None,
            "status": status, "status_reason": reason, "fingerprint": fp,
            "duplicate_of": dup["id"] if dup else None, "created_at": now,
        })
        for ci in doc.items:
            self.db.insert("sale_items", {
                "sale_id": sid, "raw_name": ci.raw.name, "item_key": norm_key(ci.raw.name), "quantity": ci.quantity,
                "unit_price": ci.unit_price, "unit_price_source": ci.unit_price_source,
                "amount": ci.amount, "amount_source": ci.amount_source, "evidence": ci.raw.evidence,
            })
        ref = f"B{sid}"
        self._store_issues(doc.all_issues(), doc.doc_date, mid, "sales", sid, ref)
        if status == "confirmed":
            self._revenue_anomaly(doc.doc_date, mid, sid)
        self.db.commit()

        label = DOC_LABEL[ex.doc_type]
        if status == "confirmed":
            lines = [f"✅ Đã ghi nhận {label} ({ref}).", f"Doanh thu: {fmt_vnd(revenue)}"]
            if doc.items:
                qty = sum(ci.quantity or 0 for ci in doc.items)
                lines.append(f"{len(doc.items)} món" + (f", {qty:g} phần." if qty else "."))
            if rsrc == "computed":
                lines.append("(Nguồn không ghi tổng — doanh thu là tổng tiền các món, do hệ thống cộng.)")
        else:
            lines = [f"⚠️ Tôi đọc được {label} ({ref}) nhưng chưa đưa vào báo cáo cho đến khi xác nhận.",
                     f"Lý do: {reason}"]
            if revenue is not None:
                lines.append(f"Doanh thu đọc được: {fmt_vnd(revenue)}")
            lines.append(CONFIRM_HINT.format(ref=ref))
        return Result(reply="\n".join(lines), record_ref=ref, status=status)

    def _revenue_anomaly(self, day: str, mid: int, sid: int) -> None:
        hist = daily_revenue_history(self.db, day, 7)
        if len(hist) < 3:
            return
        avg = sum(hist) / len(hist)
        today = day_revenue(self.db, day) or 0  # same source rule as the report — never POS + bills
        if avg and (today > avg * 2):
            self._alert(day, "warning", "REVENUE_UNUSUAL",
                        f"Doanh thu ngày {day} ({fmt_vnd(today)}) cao gấp {today / avg:.1f} lần trung bình 7 ngày ({fmt_vnd(avg)}) — B{sid}.",
                        mid, "sales", sid)

    # ------------------------------------------------------------------ price lists
    def _record_price_list(self, mid: int, ext_id: int, ex: Extraction, source_text: str | None, received: date) -> Result:
        doc = self._check(ex, source_text, received)
        now = self._now_iso()
        changes, skipped = [], []
        if doc.critical and any(i.code.startswith("DATE") for i in doc.issues):
            skipped = [ci.raw.name for ci in doc.items]
        for ci in doc.items:
            if skipped:
                break
            if not ci.ok or ci.unit_price_base is None or ci.unit is None:
                skipped.append(ci.raw.name)
                continue
            product_id, _ = self.products.resolve(ci.raw.name, ci.unit.canonical, now)
            ph = self.db.insert("price_history", {
                "product_id": product_id, "price_date": doc.doc_date, "base_unit": ci.unit.base,
                "unit_price_base": ci.unit_price_base, "quantity_base": None, "supplier": ex.supplier,
                "source": "price_list", "purchase_item_id": None, "extraction_id": ext_id,
                "evidence": ci.raw.evidence, "created_at": now,
            })
            ch = change_for(self.db, ph)
            if ch and ch.direction != "SAME":
                changes.append(ch)
                self._price_alert(ch, mid, f"bảng giá msg#{mid}")
        self._store_issues(doc.all_issues(), doc.doc_date, mid, "ai_extractions", ext_id, f"bảng giá msg#{mid}")
        self.db.commit()
        lines = [f"✅ Đã lưu bảng giá: {len(doc.items) - len(skipped)} mặt hàng (không tính vào chi)."]
        for ch in changes:
            lines.append(f"{'⚠️' if ch.diff > 0 else '↓'} {ch.product_name}: {fmt_vnd(ch.old_price)} → "
                         f"{fmt_vnd(ch.new_price)}/{ch.base_unit} ({fmt_pct(ch.pct)})")
        if skipped:
            lines.append(f"❓ Chưa lưu giá (thiếu đơn vị/giá hoặc dữ liệu không chắc): {', '.join(skipped)}")
        return Result(reply="\n".join(lines), status="confirmed", price_changes=changes)

    # ------------------------------------------------------------------ Founder commands
    _CMD_CONFIRM = re.compile(r"^(?:ok|oke|xác nhận|xac nhan|đúng|dung|duyệt|duyet)\s+([mb])\s*(\d+)$", re.I)
    _CMD_CANCEL = re.compile(r"^(?:hủy|huy|bỏ|bo|xóa|xoa)\s+([mb])\s*(\d+)$", re.I)
    _CMD_MERGE = re.compile(r"^(?:gộp|gop)\s+(.+?)\s*=\s*(.+)$", re.I)

    def _command(self, mid: int, text: str, received: date) -> Result | None:
        if m := self._CMD_CONFIRM.match(text):
            return self.confirm(m.group(1).upper() + m.group(2))
        if m := self._CMD_CANCEL.match(text):
            return self.cancel(m.group(1).upper() + m.group(2))
        if m := self._CMD_MERGE.match(text):
            target, merged = self.products.merge_alias(m.group(1).strip(), m.group(2).strip(), self._now_iso())
            name = self.products.name(target)
            return Result(reply=f"✅ Từ nay '{m.group(1).strip()}' được tính là '{name}'."
                                + (" Đã gộp lịch sử giá." if merged else ""), status="command")
        return None

    def _ref(self, ref: str):
        table = "purchases" if ref[0] == "M" else "sales"
        row = self.db.one(f"SELECT * FROM {table} WHERE id = ?", (int(ref[1:]),))
        return table, row

    def confirm(self, ref: str) -> Result:
        table, row = self._ref(ref)
        if row is None:
            return Result(reply=f"Không tìm thấy {ref}.", status="command")
        if row["status"] == "confirmed":
            return Result(reply=f"{ref} đã được ghi nhận từ trước.", status="command")
        if row["status"] == "cancelled":
            return Result(reply=f"{ref} đã bị hủy, không thể xác nhận. Gửi lại chứng từ nếu cần.", status="command")
        self.db.update(table, row["id"], {"status": "confirmed",
                                          "status_reason": f"Founder xác nhận ({row['status_reason'] or ''})"})
        self.db.execute("UPDATE alerts SET resolved = 1 WHERE ref_table = ? AND ref_id = ? AND severity = 'critical'",
                        (table, row["id"]))
        changes = self._apply_prices(row["id"]) if table == "purchases" else []
        self.db.commit()
        lines = [f"✅ Đã xác nhận {ref}. Số liệu được đưa vào báo cáo."]
        for ch in changes:
            lines.append(f"{'⚠️' if ch.diff > 0 else '↓'} {ch.product_name} {fmt_pct(ch.pct)} so với lần mua trước.")
        return Result(reply="\n".join(lines), record_ref=ref, status="confirmed", price_changes=changes)

    def cancel(self, ref: str) -> Result:
        table, row = self._ref(ref)
        if row is None:
            return Result(reply=f"Không tìm thấy {ref}.", status="command")
        self.db.update(table, row["id"], {"status": "cancelled"})
        if table == "purchases":
            # price history is derived data; remove it so a cancelled bill never moves prices
            ph_ids = [r["id"] for r in self.db.all(
                "SELECT ph.id FROM price_history ph JOIN purchase_items pi ON pi.id = ph.purchase_item_id WHERE pi.purchase_id = ?",
                (row["id"],))]
            for i in ph_ids:
                self.db.execute("UPDATE alerts SET resolved = 1 WHERE ref_table = 'price_history' AND ref_id = ?", (i,))
                self.db.execute("DELETE FROM price_history WHERE id = ?", (i,))
        self.db.execute("UPDATE alerts SET resolved = 1 WHERE ref_table = ? AND ref_id = ?", (table, row["id"]))
        self.db.commit()
        return Result(reply=f"🗑 Đã hủy {ref}. Không tính vào báo cáo (dữ liệu gốc vẫn được lưu).",
                      record_ref=ref, status="cancelled")

    # ------------------------------------------------------------------ helpers
    def _check(self, ex: Extraction, source_text: str | None, received: date, require_unit: bool = True) -> CheckedDocument:
        return check_document(ex, source_text, received, tol_vnd=self.s.amount_tolerance_vnd,
                              tol_pct=self.s.amount_tolerance_pct, max_backdate_days=self.s.max_backdate_days,
                              require_unit=require_unit)

    @staticmethod
    def _decide_status(doc: CheckedDocument, duplicate_ref: str | None) -> tuple[str, str | None]:
        crit = doc.critical
        if crit:
            return "needs_confirmation", " ".join(i.message for i in crit[:3])
        if duplicate_ref:
            return "needs_confirmation", f"Có thể trùng với {duplicate_ref} (cùng ngày, cùng hàng, cùng tổng tiền)."
        return "confirmed", None

    def _store_issues(self, issues: list[Issue], day: str, mid: int, table: str, ref_id: int, ref: str) -> None:
        for i in issues:
            if i.code == "AMOUNT_MISSING":
                continue  # summarised per record in the report's missing-data section
            self._alert(day, i.severity, i.code, f"{i.message} — {ref}", mid, table, ref_id)

    def _alert(self, day: str, severity: str, code: str, message: str, mid: int | None, table: str | None, ref_id: int | None) -> None:
        self.db.insert("alerts", {"alert_date": day, "severity": severity, "code": code, "message": message,
                                  "message_id": mid, "ref_table": table, "ref_id": ref_id, "created_at": self._now_iso()})

    def _load_media(self, msg: IncomingMessage) -> tuple[bytes, str | None]:
        if msg.media_bytes is not None:
            return msg.media_bytes, msg.media_mime
        if not msg.media_url or not self.fetch_media:
            raise RuntimeError("Tin nhắn có tệp nhưng không tải được (thiếu URL).")
        data, mime = self.fetch_media(msg.media_url)
        # the real Content-Type wins over the webhook-based guess (e.g. assumed audio/aac)
        if mime and mime != "application/octet-stream":
            return data, mime
        return data, msg.media_mime or mime

    def _store_media(self, mid: int, msg: IncomingMessage, data: bytes, mime: str | None) -> tuple[int, str]:
        sha = hashlib.sha256(data).hexdigest()
        self.s.media_dir.mkdir(parents=True, exist_ok=True)
        path = Path(self.s.media_dir) / f"{msg.received_at:%Y%m%d}-{mid}-{sha[:12]}{self._ext(mime)}"
        path.write_bytes(data)
        media_id = self.db.insert("media", {
            "message_id": mid, "kind": msg.kind, "source_url": msg.media_url, "file_path": str(path),
            "sha256": sha, "mime": mime, "size": len(data), "created_at": self._iso(msg.received_at),
        })
        self.db.commit()
        return media_id, sha

    def _same_file_recorded(self, sha: str, media_id: int) -> str | None:
        """Exact same image/voice file already produced a live record -> definite duplicate."""
        r = self.db.one(
            """SELECT 'M' || p.id AS ref FROM media m JOIN purchases p ON p.message_id = m.message_id
               WHERE m.sha256 = ? AND m.id != ? AND p.status != 'cancelled'
               UNION ALL
               SELECT 'B' || s.id FROM media m JOIN sales s ON s.message_id = m.message_id
               WHERE m.sha256 = ? AND m.id != ? AND s.status != 'cancelled' LIMIT 1""",
            (sha, media_id, sha, media_id),
        )
        return r["ref"] if r else None

    def _set_msg(self, mid: int, status: str) -> None:
        self.db.update("messages", mid, {"status": status})
        self.db.commit()

    @staticmethod
    def _ext(mime: str | None) -> str:
        return (mimetypes.guess_extension(mime or "") or ".bin") if mime else ".bin"

    def _now_iso(self) -> str:
        return datetime.now(self.s.tz).isoformat(timespec="seconds")

    def _iso(self, dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")
