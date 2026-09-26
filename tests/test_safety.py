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


# ------------------------------------------------------------ BUG-003: 8 expense categories, no unit needed
def test_expense_categories_without_unit(h):
    res = h.send(extraction("EXPENSE", items=[
        item("gas", None, None, 500000, category="gas", evidence="gas 500 ngàn", amount_text="500 ngàn"),
        item("tiền điện", None, None, 1200000, category="utilities", evidence="tiền điện 1tr2", amount_text="1tr2"),
        item("lương bé Lan", 1, "tháng", 5000000, category="salary", evidence="lương bé Lan 1 tháng 5 triệu", amount_text="5 triệu")]),
        text="gas 500 ngàn, tiền điện 1tr2, lương bé Lan 1 tháng 5 triệu")
    assert res.status == "confirmed"
    assert "đơn vị" not in res.reply
    assert h.db.one("SELECT COUNT(*) c FROM alerts WHERE code = 'UNIT_UNKNOWN'")["c"] == 0
    s = daily_summary(h.db, "2026-09-26")
    assert s.expense_by_category == {"gas": 500000, "utilities": 1200000, "salary": 5000000}
    assert not any("điện" in m for m in s.missing)
    assert any("nước" in m for m in s.missing)


# ------------------------------------------------------------ BUG-001: synonyms merge, varieties never do
def test_product_synonyms_and_varieties(h):
    pm = h.chatbot.products
    heo = pm.match("thịt heo")
    assert heo is not None and pm.match("thịt lợn") == heo and pm.match("Heo") == heo
    ids = {pm.match(n) for n in ("tôm sú", "tôm thẻ", "tôm càng", "tôm")}
    assert len(ids) == 4 and None not in ids
    for a, b in (("hành lá", "hành tím"), ("gà ta", "gà công nghiệp"), ("cải xanh", "cải ngọt"),
                 ("thịt heo nạc", "thịt heo"), ("mực ống", "mực")):
        assert pm.match(a) is None or pm.match(a) != pm.match(b), (a, b)


def test_unmapped_variety_gets_own_product_and_no_price_compare(h):
    h.send(extraction(items=[item("tôm sú", 1, "kg", 250000, evidence="1kg tôm sú 250k", amount_text="250k")]),
           text="1kg tôm sú 250k", when="2026-09-25 08:00")
    res = h.send(extraction(items=[item("tôm thẻ", 1, "kg", 150000, evidence="1kg tôm thẻ 150k", amount_text="150k")]),
                 text="1kg tôm thẻ 150k")
    assert res.price_changes == []
    assert "🆕 Mặt hàng mới: tôm thẻ" not in res.reply  # 'Tôm thẻ' is seeded, so it is known
    res = h.send(extraction(items=[item("tôm đất", 1, "kg", 180000, evidence="1kg tôm đất 180k", amount_text="180k")]),
                 text="1kg tôm đất 180k")
    assert "🆕 Mặt hàng mới: tôm đất" in res.reply and res.price_changes == []


# ------------------------------------------------------------ BUG-004 / BUG-005: real retry of failed messages
class _FakeZalo:
    def __init__(self):
        self.sent = []

    def send_text(self, user_id, text):
        self.sent.append((user_id, text))


def _service(h):
    import threading
    from founder_assistant.app import Service
    svc = Service.__new__(Service)
    svc.s, svc.db, svc.pipeline, svc.zalo, svc.lock = h.settings, h.db, h.pipeline, _FakeZalo(), threading.Lock()
    return svc


def test_failed_download_is_retried_from_stored_url(h):
    from datetime import datetime
    from founder_assistant.pipeline import IncomingMessage
    calls = []

    def flaky_fetch(url):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectionError("zalo cdn timeout")
        return b"real-bill-bytes", "image/jpeg"

    h.pipeline.fetch_media = flaky_fetch
    msg = IncomingMessage(zalo_msg_id="z1", user_id="founder", kind="image", media_url="https://cdn/x.jpg",
                          received_at=datetime(2026, 9, 26, 9, tzinfo=TZ))
    import pytest
    with pytest.raises(ConnectionError):
        h.pipeline.process(msg)
    assert h.db.one("SELECT media_url, status FROM messages")["media_url"] == "https://cdn/x.jpg"

    h.extractor.queue.append(bill())
    svc = _service(h)
    out = svc.retry_failed()
    assert out[0]["status"] == "confirmed" and calls == ["https://cdn/x.jpg"] * 2
    assert "M1" in svc.zalo.sent[0][1]
    assert h.db.one("SELECT attempts, status FROM messages")["attempts"] == 2


def test_retry_gives_up_and_tells_founder(h):
    class Boom:
        model_name = "x"

        def extract(self, **kw):
            raise ConnectionError("API down")

    h.pipeline.extractor = Boom()
    import pytest
    with pytest.raises(ConnectionError):
        h.send(None, image=b"bill")
    svc = _service(h)
    svc.retry_failed()  # attempt 2 fails
    svc.retry_failed()  # attempt 3 fails
    assert h.db.one("SELECT status, attempts FROM messages")["attempts"] == 3
    assert not svc.zalo.sent
    svc.retry_failed()  # max reached -> give up and tell the Founder once
    row = h.db.one("SELECT status, attempts FROM messages")
    assert row["status"] == "failed_final" and row["attempts"] == 3
    assert "vẫn chưa xử lý được sau 3 lần thử" in svc.zalo.sent[-1][1]
    assert svc.retry_failed() == []  # never retried again
    assert h.db.one("SELECT COUNT(*) c FROM media")["c"] == 1  # raw kept


def test_unreadable_is_not_auto_retried(h):
    from founder_assistant.extraction import ExtractionError

    class Refuse:
        model_name = "x"

        def extract(self, **kw):
            raise ExtractionError("AI không trả về dữ liệu hợp lệ.")

    h.pipeline.extractor = Refuse()
    res = h.send(None, image=b"blurry")
    assert res.status == "unreadable" and "gửi lại" in res.reply
    assert _service(h).retry_failed() == []


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from founder_assistant.db import DB
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, zalo_msg_id TEXT UNIQUE, user_id TEXT, kind TEXT NOT NULL, "
                "text TEXT, transcript TEXT, received_at TEXT NOT NULL, event_json TEXT, status TEXT NOT NULL DEFAULT 'received', error TEXT)")
    con.execute("INSERT INTO messages (kind, received_at, text) VALUES ('text', '2026-09-25T08:00:00', 'giữ nguyên')")
    con.commit()
    con.close()
    db = DB(path)
    row = db.one("SELECT * FROM messages")
    assert row["text"] == "giữ nguyên" and row["attempts"] == 1 and row["media_url"] is None


# ------------------------------------------------------------ BUG-007/008/009: spoken input end-to-end
def test_spoken_amount_words_confirmed(h):
    said = "Rau hết một trăm hai."
    res = h.send(extraction(items=[item("rau", None, None, 120000, amount_text="một trăm hai", evidence="Rau hết một trăm hai")]),
                 audio=b"v1", transcript=said)
    assert res.status == "confirmed"
    assert h.db.one("SELECT amount FROM purchase_items")["amount"] == 120000


def test_spoken_amount_misread_is_caught(h):
    said = "Lấy thêm ba ký tôm, ba trăm sáu chục."
    res = h.send(extraction(items=[item("tôm", 3, "ký", 3600000, amount_text="ba trăm sáu chục",
                                        evidence="ba ký tôm, ba trăm sáu chục")]), audio=b"v2", transcript=said)
    assert res.status == "needs_confirmation"


def test_spoken_unit_price_per_kg(h):
    said = "Mua ba ký ba chỉ giá chín mươi lăm ngàn một ký."
    res = h.send(extraction(items=[item("ba chỉ", 3, "ký", None, unit_price=95000, unit_price_text="chín mươi lăm ngàn một ký",
                                        evidence="ba ký ba chỉ giá chín mươi lăm ngàn một ký")]), audio=b"v3", transcript=said)
    assert res.status == "confirmed"
    row = h.db.one("SELECT * FROM purchase_items")
    assert row["amount"] == 285000 and row["amount_source"] == "computed" and row["unit_price_source"] == "source"


def test_ki_lo_unit(h):
    from founder_assistant.units import normalize_unit
    assert normalize_unit("ki lô").canonical == "kg" and normalize_unit("ki-lô-gam").canonical == "kg"


def test_approximate_amount_needs_confirmation(h):
    said = "Hôm nay đi chợ hết khoảng một triệu hai."
    res = h.send(extraction(items=[item("đi chợ", None, None, 1200000, amount_text="một triệu hai",
                                        evidence="đi chợ hết khoảng một triệu hai")]), audio=b"v4", transcript=said)
    assert res.status == "needs_confirmation" and "ước lượng" in res.reply
    assert daily_summary(h.db, "2026-09-26").expense is None


def test_missing_quantity_still_booked_with_unknown(h):
    said = "Mua thêm năm ký rau cải."
    res = h.send(extraction(items=[item("rau cải", 5, "ký", None, evidence="năm ký rau cải")]), audio=b"v5", transcript=said)
    assert res.status == "confirmed" and "Chưa có giá: rau cải" in res.reply
    assert h.db.one("SELECT amount FROM purchase_items")["amount"] is None


# ------------------------------------------------------------ BUG-010: consistent x10 misread
def test_consistent_x10_misread_is_held(h):
    h.send(extraction(items=[item("thịt heo", 5, "kg", 450000, evidence="5kg thịt heo 450k", amount_text="450k")]),
           text="5kg thịt heo 450k", when="2026-09-25 08:00")
    ocr = "Thịt heo 5kg 4.600.000\nTổng 4.600.000"
    res = h.send(extraction(ocr_text=ocr, stated_total=4600000, stated_total_text="4.600.000",
                            items=[item("Thịt heo", 5, "kg", 4600000, amount_text="4.600.000", evidence="Thịt heo 5kg 4.600.000")]),
                 image=b"bill-x10")
    assert res.status == "needs_confirmation" and "sai số 0" in res.reply
    assert daily_summary(h.db, "2026-09-26").expense is None
    # a real +20% move is still booked normally
    res = h.send(extraction(items=[item("thịt heo", 5, "kg", 540000, evidence="5kg thịt heo 540k", amount_text="540k")]),
                 text="5kg thịt heo 540k")
    assert res.status == "confirmed"
