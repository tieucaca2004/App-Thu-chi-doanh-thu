"""Exercises the real Anthropic SDK call path with a mocked HTTP transport (no network, no key)."""
import json

import anthropic
import httpx2 as httpx
import pytest

from founder_assistant.extraction import ClaudeExtractor, ExtractionError

READ = {
    "doc_type": "PURCHASE_BILL", "ocr_text": None, "document_date": "2026-09-26", "supplier": None,
    "items": [{"name": "thịt heo", "quantity": 5, "unit": "ký", "unit_price": None, "amount": 450000,
               "amount_text": "450 ngàn", "unit_price_text": None, "category": "NGUYEN_LIEU",
               "evidence": "5 ký thịt heo 450 ngàn"}],
    "stated_total": None, "stated_total_text": None, "revenue": None, "question": None, "unreadable_parts": [],
}


def client_returning(stop_reason, text, seen):
    def handler(request: httpx.Request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })
    return anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_request_shape_and_parse():
    seen = []
    ex = ClaudeExtractor("claude-opus-5", client_returning("end_turn", json.dumps(READ), seen))
    out = ex.extract(text="mua 5 ký thịt heo 450 ngàn", image=b"\xff\xd8img", image_mime="image/jpeg",
                     received_date="2026-09-26", source_kind="image")
    assert out.items[0].amount == 450000 and out.items[0].unit_price is None
    body = seen[0]
    assert body["model"] == "claude-opus-5"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert body["fallbacks"] == "default"
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "2026-09-26" in content[1]["text"]


def test_refusal_raises():
    ex = ClaudeExtractor("claude-opus-5", client_returning("refusal", "", []))
    with pytest.raises(ExtractionError):
        ex.extract(text="x", image=None, image_mime=None, received_date="2026-09-26", source_kind="text")
