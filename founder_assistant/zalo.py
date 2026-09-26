"""Zalo Official Account integration: webhook parsing, signature check, sending messages, media download."""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .db import DB
from .pipeline import IncomingMessage

log = logging.getLogger(__name__)

SEND_URL = "https://openapi.zalo.me/v3.0/oa/message/cs"
TOKEN_URL = "https://oauth.zaloapp.com/v4/oa/access_token"
ZALO_TEXT_LIMIT = 2000

EVENT_KIND = {
    "user_send_text": "text",
    "user_send_image": "image",
    "user_send_audio": "audio",
    "user_send_file": "file",
    "user_send_gif": "image",
}


def verify_signature(raw_body: bytes, header: str | None, app_id: str, oa_secret_key: str, timestamp: str) -> bool:
    """Zalo OA webhook: X-ZEvent-Signature = 'mac=' + sha256(app_id + body + timestamp + oa_secret_key)."""
    if not header or not oa_secret_key:
        return False
    expected = hashlib.sha256((app_id + raw_body.decode("utf-8") + timestamp + oa_secret_key).encode()).hexdigest()
    got = header.split("=", 1)[1] if header.startswith("mac=") else header
    return hmac.compare_digest(expected, got)


def parse_event(event: dict, tz: ZoneInfo) -> IncomingMessage | None:
    """Convert a Zalo webhook event into an IncomingMessage. Returns None for events we ignore."""
    kind = EVENT_KIND.get(event.get("event_name", ""))
    if kind is None:
        return None
    message = event.get("message") or {}
    ts = event.get("timestamp")
    received = datetime.fromtimestamp(int(ts) / 1000, tz) if ts else datetime.now(tz)
    url, mime = None, None
    attachments = message.get("attachments") or []
    if attachments:
        payload = attachments[0].get("payload") or {}
        url = payload.get("url") or payload.get("thumbnail")
        mime = {"image": None, "audio": "audio/aac", "file": None}.get(kind)
        if kind == "file" and payload.get("type"):
            mime = {"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png"}.get(payload["type"].lower())
    text = message.get("text")  # for images this is the caption, kept as extra context
    return IncomingMessage(
        zalo_msg_id=str(message.get("msg_id") or f"{event.get('sender', {}).get('id')}-{ts}"),
        user_id=str((event.get("sender") or {}).get("id", "")),
        kind=kind, received_at=received, text=text, media_url=url, media_mime=mime, event_json=event,
    )


class ZaloClient:
    def __init__(self, db: DB, app_id: str, app_secret: str, access_token: str, refresh_token: str, tz: ZoneInfo):
        self.db, self.app_id, self.app_secret, self.tz = db, app_id, app_secret, tz
        self._access = db.kv_get("zalo_access_token") or access_token
        self._refresh = db.kv_get("zalo_refresh_token") or refresh_token

    def refresh(self) -> bool:
        if not (self._refresh and self.app_id and self.app_secret):
            return False
        r = httpx.post(TOKEN_URL, headers={"secret_key": self.app_secret},
                       data={"refresh_token": self._refresh, "app_id": self.app_id, "grant_type": "refresh_token"},
                       timeout=30)
        body = r.json()
        if "access_token" not in body:
            log.error("Zalo token refresh failed: %s", body)
            return False
        now = datetime.now(self.tz).isoformat(timespec="seconds")
        self._access, self._refresh = body["access_token"], body.get("refresh_token", self._refresh)
        self.db.kv_set("zalo_access_token", self._access, now)
        self.db.kv_set("zalo_refresh_token", self._refresh, now)
        return True

    def send_text(self, user_id: str, text: str) -> None:
        for chunk in _chunks(text, ZALO_TEXT_LIMIT):
            self._post({"recipient": {"user_id": user_id}, "message": {"text": chunk}})

    def _post(self, body: dict, retry: bool = True) -> dict:
        r = httpx.post(SEND_URL, headers={"access_token": self._access, "Content-Type": "application/json"},
                       json=body, timeout=30)
        data = r.json()
        # -216: access token invalid/expired
        if data.get("error") in (-216, -124) and retry and self.refresh():
            return self._post(body, retry=False)
        if data.get("error"):
            log.error("Zalo send failed: %s", data)
        return data


def fetch_media(url: str) -> tuple[bytes, str | None]:
    r = httpx.get(url, timeout=60, follow_redirects=True)
    r.raise_for_status()
    mime = (r.headers.get("content-type") or "").split(";")[0].strip() or None
    return r.content, mime


def _chunks(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + line
    if cur:
        out.append(cur)
    return out
