"""Self-test of the real-world validation HARNESS (scoring, layer hints, traceability).
Uses scripted readings — this proves the harness can FAIL, it is NOT real-world validation."""
import json

from founder_assistant.realval import preflight, run_cases, trace_day
from founder_assistant.db import DB
from tests.conftest import FakeTranscriber, ScriptedExtractor, extraction, item, revenue

OCR = "Thịt heo 5kg 460.000\nTổng 460.000"


def spec(tmp_path):
    (tmp_path / "b1.jpg").write_bytes(b"bill-1")
    (tmp_path / "b2.jpg").write_bytes(b"bill-2")
    (tmp_path / "v1.m4a").write_bytes(b"voice-1")
    (tmp_path / "pos.jpg").write_bytes(b"pos")
    heo = {"product": "thịt heo", "quantity": 5, "unit": "kg", "unit_price": 92000, "amount": 460000}
    return {
        "primary_revenue_source": "pos_closing",
        "cases": [
            {"id": "V01", "kind": "voice", "file": "v1.m4a", "sent_at": "2026-09-23 08:00",
             "expected": {"doc_type": "PURCHASE_BILL", "status": "confirmed",
                          "items": [{"product": "thịt heo", "quantity": 5, "unit": "kg", "amount": 440000}]}},
            {"id": "B01", "kind": "image", "file": "b1.jpg", "sent_at": "2026-09-26 07:00",
             "expected": {"doc_type": "PURCHASE_BILL", "total": 460000, "supplier": None, "items": [heo]}},
            {"id": "B02-misread", "kind": "image", "file": "b2.jpg", "sent_at": "2026-09-26 07:30",
             "expected": {"total": 46000, "items": [heo | {"amount": 46000}]}},
            {"id": "B01-again", "kind": "image", "file": "b1.jpg", "sent_at": "2026-09-26 08:00", "tags": ["duplicate"],
             "expected": {"status": "duplicate"}},
            {"id": "POS", "kind": "image", "file": "pos.jpg", "sent_at": "2026-09-26 21:00",
             "expected": {"doc_type": "REVENUE_REPORT", "total": 3000000}},
        ],
        "expected_price_changes": [{"case": "B01", "product": "thịt heo", "date": "2026-09-26", "old": 88000, "new": 92000,
                                    "diff": 4000, "pct": 4.55}],
        "expected_reports": [{"date": "2026-09-26", "revenue": 3000000, "expense": 460000, "net": 2540000,
                              "forbidden_phrases": ["lợi nhuận ròng:"]}],
    }


def scripted():
    ex = ScriptedExtractor()
    ex.queue += [
        extraction(items=[item("thịt heo", 5, "ký", 440000, amount_text="bốn trăm bốn chục ngàn",
                               evidence="năm ký thịt heo bốn trăm bốn chục ngàn")]),
        extraction(ocr_text=OCR, stated_total=460000, stated_total_text="460.000",
                   items=[item("Thịt heo", 5, "kg", 460000, amount_text="460.000", evidence="Thịt heo 5kg 460.000")]),
        # a second, different bill that the AI misread x10 on BOTH line and total (consistent -> passes validation)
        extraction(ocr_text="Thịt heo 5kg 4.600.000\nTổng 4.600.000", stated_total=4600000, stated_total_text="4.600.000",
                   items=[item("Thịt heo", 5, "kg", 4600000, amount_text="4.600.000", evidence="Thịt heo 5kg 4.600.000")]),
        extraction("REVENUE_REPORT", ocr_text="CHỐT CA\nDoanh thu 3.000.000", revenue=revenue(gross_revenue=3000000)),
    ]
    stt = FakeTranscriber()
    stt.text = "Hôm nay mua năm ký thịt heo bốn trăm bốn chục ngàn"
    return ex, stt


def test_harness_scores_and_traces(tmp_path):
    ex, stt = scripted()
    p = run_cases(spec(tmp_path), tmp_path, tmp_path / "out", extractor=ex, transcriber=stt)
    grades = {c["id"]: c["grade"] for c in p["cases"]}
    assert grades == {"V01": "PASS", "B01": "PASS", "B02-misread": "FAIL", "B01-again": "PASS", "POS": "PASS"}
    bad = next(c for c in p["cases"] if c["id"] == "B02-misread")
    hint = next(x for x in bad["checks"] if x["field"] == "thịt heo.amount")["layer_hint"]
    assert hint.startswith("OCR/VISION")
    assert p["price_changes"][0]["pass"] is True
    # the self-consistent x10 misread is held by the price-outlier guard (BUG-010) -> report numbers correct
    assert next(c for c in p["cases"] if c["id"] == "B02-misread")["status"] == "needs_confirmation"
    assert p["reports"][0]["pass"] is True and p["metrics"]["report"] == "PASS"
    assert p["metrics"]["duplicate_detection"] == (1, 1)
    assert p["metrics"]["traceability"][0] == p["metrics"]["traceability"][1] > 0
    md = (tmp_path / "out" / "RESULTS.md").read_text()
    assert "B02-misread | image | **FAIL**" in md and "Hôm nay mua năm ký thịt heo" in md
    json.loads((tmp_path / "out" / "results.json").read_text())


def test_trace_detects_tampered_original(tmp_path):
    ex, stt = scripted()
    run_cases(spec(tmp_path), tmp_path, tmp_path / "out", extractor=ex, transcriber=stt)
    db = DB(tmp_path / "out" / "data" / "founder.db")
    path = db.one("SELECT file_path FROM media WHERE message_id = 2")["file_path"]
    open(path, "wb").write(b"edited later")
    items = trace_day(db, "2026-09-26")
    assert not all(i["ok"] for i in items)
    assert any("SHA-256" in (c.get("why") or "") for i in items for c in i["chains"])


def test_preflight_reports_blocked_without_printing_secrets(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.delenv("STT_URL", raising=False)
    res = preflight(ping=False)
    assert res["Claude"]["status"] == "CONFIGURED" and res["STT"]["status"] == "BLOCKED"
    assert "sk-secret-value" not in json.dumps(res)


def test_harness_report_check_can_fail(tmp_path):
    ex, stt = scripted()
    sp = spec(tmp_path)
    sp["expected_reports"][0]["expense"] = 999
    p = run_cases(sp, tmp_path, tmp_path / "out", extractor=ex, transcriber=stt)
    assert p["metrics"]["report"] == "FAIL" and p["reports"][0]["checks"]["expense"] is False
