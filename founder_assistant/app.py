"""FastAPI service: Zalo OA webhook, daily report job, signed report download.

Run:  uvicorn founder_assistant.app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .chatbot import Chatbot
from .config import Settings
from .db import DB
from .extraction import ClaudeExtractor
from .pipeline import IncomingMessage, Pipeline
from .products import ProductMaster
from .report import generate_daily_report
from .stt import NoTranscriber, WhisperHTTPTranscriber
from .zalo import ZaloClient, fetch_media, parse_event, verify_signature

log = logging.getLogger("founder_assistant")
logging.basicConfig(level=logging.INFO)


class Service:
    def __init__(self, settings: Settings):
        self.s = settings
        settings.ensure_dirs()
        self.db = DB(settings.db_path)
        ProductMaster(self.db).seed(datetime.now(settings.tz).isoformat(timespec="seconds"))
        self.chatbot = Chatbot(self.db)
        transcriber = (WhisperHTTPTranscriber(settings.stt_url, settings.stt_api_key, settings.stt_model)
                       if settings.stt_url else NoTranscriber())
        self.pipeline = Pipeline(self.db, settings, ClaudeExtractor(settings.claude_model), transcriber,
                                 fetch_media, answer_question=self.chatbot.answer)
        self.zalo = ZaloClient(self.db, settings.zalo_app_id, settings.zalo_app_secret,
                               settings.zalo_access_token, settings.zalo_refresh_token, settings.tz)
        self.lock = threading.Lock()  # one message at a time: ordering matters for price history

    def founder_id(self) -> str | None:
        return self.s.founder_zalo_user_id or self.db.kv_get("founder_user_id")

    def handle(self, msg: IncomingMessage) -> None:
        with self.lock:
            try:
                result = self.pipeline.process(msg)
            except Exception:  # noqa: BLE001
                log.exception("processing failed for %s", msg.zalo_msg_id)
                reply = "⚠️ Có lỗi khi xử lý tin nhắn này. Dữ liệu gốc đã được lưu và sẽ được xử lý lại."
            else:
                reply = result.reply
        if reply:
            self.zalo.send_text(msg.user_id, reply)

    def report_link(self, day: str) -> str | None:
        if not self.s.public_base_url:
            return None
        return f"{self.s.public_base_url.rstrip('/')}/reports/{day}.xlsx?sig={sign(day, self.s.report_link_secret)}"

    def daily_report(self, day: str | None = None, send: bool = True) -> dict:
        day = day or datetime.now(self.s.tz).date().isoformat()
        with self.lock:
            text, path, _ = generate_daily_report(self.db, day, self.s.reports_dir, datetime.now(self.s.tz))
        link = self.report_link(day)
        full = text + (f"\n📎 File báo cáo: {link}" if link else f"\n📎 File: {path.name}")
        fid = self.founder_id()
        if send and fid:
            self.zalo.send_text(fid, full)
        return {"day": day, "xlsx": str(path), "text": full, "sent_to": fid if send else None}


def sign(day: str, secret: str) -> str:
    return hmac.new(secret.encode(), day.encode(), hashlib.sha256).hexdigest()[:32]


async def _scheduler(svc: Service) -> None:
    hh, mm = (int(x) for x in svc.s.daily_report_time.split(":"))
    while True:
        now = datetime.now(svc.s.tz)
        run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run <= now:
            run += timedelta(days=1)
        await asyncio.sleep((run - now).total_seconds())
        try:
            await asyncio.to_thread(svc.daily_report, run.date().isoformat())
        except Exception:  # noqa: BLE001
            log.exception("daily report failed")


def create_app(settings: Settings | None = None, service: Service | None = None, schedule: bool = True) -> FastAPI:
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["svc"] = service or Service(settings or Settings())
        task = asyncio.create_task(_scheduler(holder["svc"])) if schedule else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="AI Founder Data Assistant", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/webhook/zalo")
    async def zalo_webhook(request: Request, background: BackgroundTasks):
        svc: Service = holder["svc"]
        raw = await request.body()
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid json")
        if svc.s.zalo_oa_secret_key and not verify_signature(
                raw, request.headers.get("X-ZEvent-Signature"), svc.s.zalo_app_id, svc.s.zalo_oa_secret_key,
                str(event.get("timestamp", ""))):
            raise HTTPException(401, "bad signature")
        msg = parse_event(event, svc.s.tz)
        if msg is None:
            return {"ignored": event.get("event_name")}
        founder = svc.founder_id()
        if founder is None:
            # V1 serves exactly one Founder: bind the first sender, log it loudly.
            svc.db.kv_set("founder_user_id", msg.user_id, datetime.now(svc.s.tz).isoformat())
            log.warning("FOUNDER_ZALO_USER_ID not set — bound to first sender %s", msg.user_id)
        elif msg.user_id != founder:
            log.warning("ignored message from non-founder user %s", msg.user_id)
            return {"ignored": "not founder"}
        background.add_task(svc.handle, msg)  # answer Zalo fast; process after responding
        return {"accepted": msg.zalo_msg_id}

    @app.post("/jobs/daily-report")
    def run_daily_report(day: str | None = None, send: bool = True):
        if day:
            date.fromisoformat(day)
        return holder["svc"].daily_report(day, send=send)

    @app.post("/jobs/reprocess-failed")
    def reprocess_failed():
        """Retry messages that failed (API outage, network...). Replies are sent to the Founder."""
        svc: Service = holder["svc"]
        done = []
        for row in svc.db.all("SELECT id, user_id FROM messages WHERE status = 'failed' ORDER BY id"):
            with svc.lock:
                try:
                    res = svc.pipeline.reprocess(row["id"])
                except Exception as exc:  # noqa: BLE001
                    done.append({"message_id": row["id"], "error": str(exc)})
                    continue
            if res.reply:
                svc.zalo.send_text(row["user_id"], res.reply)
            done.append({"message_id": row["id"], "status": res.status, "ref": res.record_ref})
        return {"reprocessed": done}

    @app.get("/reports/{day}.xlsx")
    def download_report(day: str, sig: str):
        svc: Service = holder["svc"]
        if not hmac.compare_digest(sig, sign(day, svc.s.report_link_secret)):
            raise HTTPException(403, "bad signature")
        path = svc.s.reports_dir / f"Founder-Daily-Report-{date.fromisoformat(day).isoformat()}.xlsx"
        if not path.exists():
            raise HTTPException(404, "report not generated")
        return FileResponse(path, filename=path.name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if os.environ.get("DEV_ENDPOINTS") == "1":
        @app.post("/dev/message")
        async def dev_message(request: Request):
            """Local testing without Zalo: {"text": "..."}; returns the reply instead of sending it."""
            svc: Service = holder["svc"]
            body = await request.json()
            now = datetime.now(svc.s.tz)
            msg = IncomingMessage(zalo_msg_id=f"dev-{now.timestamp()}", user_id="dev", kind="text",
                                  received_at=now, text=body["text"])
            with svc.lock:
                res = svc.pipeline.process(msg)
            return JSONResponse({"reply": res.reply, "ref": res.record_ref, "status": res.status})

    return app


app = create_app()
