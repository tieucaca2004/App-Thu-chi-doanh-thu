"""Anti-hallucination, duplicate detection, Founder confirmation flow, chatbot, traceability."""
from datetime import date, datetime

from founder_assistant.analytics import daily_summary
from founder_assistant.pricing import NOT_ENOUGH_DATA, trend_statement
from tests.conftest import TZ, extraction, item, question, revenue


# ------------------------------------------------------------ §7 / §29: never invent numbers
def test_missing_price_stays_unknown(h):
    ocr = "Tôm sú 3kg"
    res = h.send(extraction(ocr_text=ocr, items=[item("Tôm sú", 3, "kg", None, evidence="Tôm sú 3kg")]), image=b"img")
    row = h.db.one("SELECT * FROM purchase_items")
    assert row["amount"] is None and row["unit_price"] is None
    assert h.db.one("SELECT COUNT(*) c FROM price_history")["c"] == 0
    assert "Chưa có giá: Tôm sú" in res.reply
    s = daily_summary(h.db, "2026-09-26")
    assert any("Chưa có giá: Tôm sú" in m for m in s.missing)


def test_number_not_in_source_is_rejected(h):
    # model returns 500k but the Founder said 450 ngàn -> must not be booked silently
    res = h.send(extraction(items=[item("thịt heo", 5, "ký", 500000, amount_text="500 ngàn", evidence="5 ký thịt heo 450 ngàn")]),
                 text="mua 5 ký thịt heo 450 ngàn")
    assert res.status == "needs_confirmation"
    assert daily_summary(h.db, "2026-09-26").expense is None


def test_amount_text_disagrees_with_amount(h):
    res = h.send(extraction(items=[item("tôm", 3, "kg", 36000, amount_text="360 ngàn", evidence="3 ký tôm 360 ngàn")]),
                 text="3 ký tôm 360 ngàn")
    assert res.status == "needs_confirmation"
    assert "360 ngàn" in res.reply


def test_item_not_in_source_is_rejected(h):
    res = h.send(extraction(items=[item("mực", 2, "kg", 300000, amount_text="300k", evidence="2kg mực 300k")]),
                 text="mua 2 ký tôm 300k")
    assert res.status == "needs_confirmation"
    assert "không tìm thấy" in res.reply


def test_future_date_held_back(h):
    res = h.send(extraction(document_date="2026-10-15", items=[item("rau", None, None, 50000, evidence="rau 50k", amount_text="50k")]),
                 text="rau 50k")
    assert res.status == "needs_confirmation" and "tương lai" in res.reply


def test_revenue_payments_exceed_total(h):
    ocr = "Doanh thu 1.000.000\nTiền mặt 800.000\nChuyển khoản 500.000"
    res = h.send(extraction("REVENUE_REPORT", ocr_text=ocr, revenue=revenue(gross_revenue=1000000, cash=800000, bank_transfer=500000)),
                 image=b"pos")
    assert res.status == "needs_confirmation"


# ------------------------------------------------------------ §20 duplicates
def bill():
    ocr = "Thịt heo 5kg 450.000\nTổng 450.000"
    return extraction(ocr_text=ocr, stated_total=450000, stated_total_text="450.000",
                      items=[item("Thịt heo", 5, "kg", 450000, amount_text="450.000", evidence="Thịt heo 5kg 450.000")])


def test_same_image_twice_not_counted_twice(h):
    h.send(bill(), image=b"same-photo")
    res = h.send(None, image=b"same-photo")  # identical file -> short-circuit, AI not even called
    assert res.status == "duplicate" and "M1" in res.reply
    assert len(h.extractor.calls) == 1
    assert daily_summary(h.db, "2026-09-26").expense == 450000


def test_same_bill_different_photo_flagged(h):
    h.send(bill(), image=b"photo-a")
    res = h.send(bill(), image=b"photo-b-retaken")
    assert res.status == "needs_confirmation" and "trùng với M1" in res.reply
    assert daily_summary(h.db, "2026-09-26").expense == 450000


def test_webhook_retry_is_idempotent(h):
    h.send(bill(), image=b"x", msg_id="zalo-123")
    res = h.send(None, image=b"x", msg_id="zalo-123")
    assert res.reply is None
    assert h.db.one("SELECT COUNT(*) c FROM messages")["c"] == 1


# ------------------------------------------------------------ confirmation flow
def test_confirm_then_cancel(h):
    h.send(bill(), image=b"a")
    r = h.send(bill(), image=b"b")
    assert r.status == "needs_confirmation"
    assert h.send(None, text="ok M2").status == "confirmed"
    assert daily_summary(h.db, "2026-09-26").expense == 900000
    assert h.db.one("SELECT COUNT(*) c FROM price_history")["c"] == 2
    assert h.send(None, text="hủy M2").status == "cancelled"
    assert daily_summary(h.db, "2026-09-26").expense == 450000
    assert h.db.one("SELECT COUNT(*) c FROM price_history")["c"] == 1
    # raw data is never deleted
    assert h.db.one("SELECT COUNT(*) c FROM purchases")["c"] == 2
    assert h.db.one("SELECT COUNT(*) c FROM media")["c"] == 2


def test_merge_alias_command(h):
    h.send(extraction(items=[item("ba chỉ", 2, "kg", 240000, evidence="2kg ba chỉ 240k", amount_text="240k")]),
           text="2kg ba chỉ 240k")
    r = h.send(None, text="gộp ba chỉ = thịt heo")
    assert "Thịt heo" in r.reply
    res = h.send(extraction(items=[item("ba chỉ", 2, "kg", 260000, evidence="2kg ba chỉ 260k", amount_text="260k")]),
                 text="2kg ba chỉ 260k")
    assert res.price_changes and res.price_changes[0].product_name == "Thịt heo"


# ------------------------------------------------------------ §18 fact vs inference
def test_trend_needs_enough_points(h):
    for i, price in enumerate([82, 85]):
        h.send(extraction(items=[item("thịt heo", 1, "kg", price * 1000, evidence=f"1kg thịt heo {price}k", amount_text=f"{price}k")]),
               text=f"1kg thịt heo {price}k", when=f"2026-09-{10 + i:02d} 08:00")
    pid = h.chatbot.products.match("thịt heo")
    assert trend_statement(h.db, pid, "kg", "2026-09-26") == NOT_ENOUGH_DATA
    for i, price in enumerate([88, 92]):
        h.send(extraction(items=[item("thịt heo", 1, "kg", price * 1000, evidence=f"1kg thịt heo {price}k", amount_text=f"{price}k")]),
               text=f"1kg thịt heo {price}k", when=f"2026-09-{20 + i:02d} 08:00")
    assert trend_statement(h.db, pid, "kg", "2026-09-26").startswith("(Nhận định) Giá tăng liên tiếp 3 lần")


# ------------------------------------------------------------ §26 chatbot answers from data only
def test_chatbot(h):
    h.send(extraction(items=[item("tôm", 3, "kg", 354000, evidence="3kg tôm 354k", amount_text="354k")]),
           text="3kg tôm 354k", when="2026-09-20 08:00")
    h.send(extraction(items=[item("tôm", 2, "kg", 250000, evidence="2kg tôm 250k", amount_text="250k")]),
           text="2kg tôm 250k", when="2026-09-26 08:00")

    r = h.send(extraction("QUESTION", question=question("EXPENSE_TOTAL")), text="Hôm nay chi bao nhiêu?")
    assert "250.000đ" in r.reply and "M2" in r.reply
    r = h.send(extraction("QUESTION", question=question("PRODUCT_LAST_PRICE", product="tôm")), text="Giá tôm lần gần nhất?")
    assert "125.000đ/kg" in r.reply and "26/09/2026" in r.reply
    r = h.send(extraction("QUESTION", question=question("PRODUCT_PRICE_CHANGE", "THIS_MONTH", product="tôm")),
               text="Tháng này giá tôm tăng bao nhiêu?")
    assert "+7.000đ/kg" in r.reply and "+5,93%" in r.reply
    r = h.send(extraction("QUESTION", question=question("REVENUE_TOTAL", "YESTERDAY")), text="Hôm qua doanh thu?")
    assert "Chưa có dữ liệu doanh thu" in r.reply
    r = h.send(extraction("QUESTION", question=question("PRODUCT_LAST_PRICE", product="cua")), text="giá cua?")
    assert "Chưa có dữ liệu" in r.reply


def test_voice_without_stt_is_kept(h):
    from founder_assistant.stt import NoTranscriber
    h.pipeline.transcriber = NoTranscriber()
    res = h.send(None, audio=b"voice")
    assert res.status == "needs_transcription"
    assert h.db.one("SELECT status FROM messages")["status"] == "needs_transcription"
    assert h.db.one("SELECT COUNT(*) c FROM media")["c"] == 1


def test_failed_message_can_be_reprocessed(h):
    class Boom:
        model_name = "x"

        def extract(self, **kw):
            raise ConnectionError("API down")

    real = h.pipeline.extractor
    h.pipeline.extractor = Boom()
    import pytest
    with pytest.raises(ConnectionError):
        h.send(None, image=b"bill-photo")
    assert h.db.one("SELECT status FROM messages")["status"] == "failed"

    h.pipeline.extractor = real
    real.queue.append(bill())
    res = h.pipeline.reprocess(1)
    assert res.status == "confirmed" and res.record_ref == "M1"
    assert h.db.one("SELECT COUNT(*) c FROM media")["c"] == 1  # original file reused


def test_image_without_ocr_is_not_trusted(h):
    res = h.send(extraction(ocr_text=None, items=[item("Thịt heo", 5, "kg", 450000, evidence="Thịt heo 5kg 450.000")]),
                 image=b"blurry")
    assert res.status == "needs_confirmation"
