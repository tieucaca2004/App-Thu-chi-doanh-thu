import hashlib
import json

from fastapi.testclient import TestClient

from founder_assistant.app import create_app
from founder_assistant.config import Settings
from founder_assistant.db import DB
from founder_assistant.zalo import parse_event, verify_signature
from tests.conftest import TZ

EVENT = {
    "app_id": "app1", "event_name": "user_send_image", "timestamp": "1790384400000",
    "sender": {"id": "founder-1"}, "recipient": {"id": "oa-1"},
    "message": {"msg_id": "abc123", "text": "bill chợ sáng",
                "attachments": [{"type": "image", "payload": {"url": "https://zalo.example/img.jpg"}}]},
}


def test_parse_image_event():
    m = parse_event(EVENT, TZ)
    assert (m.kind, m.zalo_msg_id, m.user_id, m.media_url, m.text) == (
        "image", "abc123", "founder-1", "https://zalo.example/img.jpg", "bill chợ sáng")
    assert m.received_at.tzinfo is not None
    assert parse_event({"event_name": "follow"}, TZ) is None


def test_signature():
    body = json.dumps(EVENT).encode()
    mac = hashlib.sha256(("app1" + body.decode() + EVENT["timestamp"] + "secret").encode()).hexdigest()
    assert verify_signature(body, f"mac={mac}", "app1", "secret", EVENT["timestamp"])
    assert not verify_signature(body, "mac=deadbeef", "app1", "secret", EVENT["timestamp"])


class StubService:
    def __init__(self, tmp_path, founder):
        self.s = Settings(data_dir=tmp_path, zalo_app_id="app1", zalo_oa_secret_key="secret", founder_zalo_user_id=founder)
        self.db = DB(":memory:")
        self.handled = []

    def founder_id(self):
        return self.s.founder_zalo_user_id

    def handle(self, msg):
        self.handled.append(msg)


def _post(client, event):
    body = json.dumps(event).encode()
    mac = hashlib.sha256(("app1" + body.decode() + event["timestamp"] + "secret").encode()).hexdigest()
    return client.post("/webhook/zalo", content=body, headers={"X-ZEvent-Signature": f"mac={mac}"})


def test_webhook_accepts_founder_only(tmp_path):
    svc = StubService(tmp_path, "founder-1")
    with TestClient(create_app(service=svc, schedule=False)) as client:
        assert _post(client, EVENT).json() == {"accepted": "abc123"}
        stranger = {**EVENT, "sender": {"id": "someone-else"}}
        assert _post(client, stranger).json() == {"ignored": "not founder"}
        bad = client.post("/webhook/zalo", content=json.dumps(EVENT), headers={"X-ZEvent-Signature": "mac=00"})
        assert bad.status_code == 401
    assert [m.zalo_msg_id for m in svc.handled] == ["abc123"]
