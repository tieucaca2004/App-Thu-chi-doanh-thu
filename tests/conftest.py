"""Test harness. The AI reader is replaced by a scripted extractor that returns exactly what a correct
reading of the source would be; everything after it (validation, arithmetic, dedup, price history,
reports) is the real code."""
from __future__ import annotations

from datetime import datetime
from itertools import count
from zoneinfo import ZoneInfo

import pytest

from founder_assistant.chatbot import Chatbot
from founder_assistant.config import Settings
from founder_assistant.db import DB
from founder_assistant.extraction import Extraction, ExtractedItem, QuestionIntent, RevenueFields
from founder_assistant.pipeline import IncomingMessage, Pipeline
from founder_assistant.products import ProductMaster

TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def item(name, quantity=None, unit=None, amount=None, unit_price=None, evidence=None, category="NGUYEN_LIEU",
         amount_text=None, unit_price_text=None):
    return ExtractedItem(name=name, quantity=quantity, unit=unit, unit_price=unit_price, amount=amount,
                         amount_text=amount_text, unit_price_text=unit_price_text, category=category,
                         evidence=evidence or name)


def extraction(doc_type="PURCHASE_BILL", items=(), stated_total=None, stated_total_text=None, document_date=None,
               supplier=None, ocr_text=None, revenue=None, question=None, unreadable=()):
    return Extraction(doc_type=doc_type, ocr_text=ocr_text, document_date=document_date, supplier=supplier,
                      items=list(items), stated_total=stated_total, stated_total_text=stated_total_text,
                      revenue=revenue, question=question, unreadable_parts=list(unreadable))


def revenue(**kw):
    base = dict(gross_revenue=None, bill_count=None, item_count=None, cash=None, bank_transfer=None,
                e_wallet=None, platform_fee=None, evidence=None)
    base.update(kw)
    return RevenueFields(**base)


def question(intent, period="TODAY", product=None, date=None):
    return QuestionIntent(intent=intent, period=period, product=product, date=date)


class ScriptedExtractor:
    model_name = "scripted-test"

    def __init__(self):
        self.queue: list[Extraction] = []
        self.calls: list[dict] = []

    def extract(self, **kw):
        self.calls.append(kw)
        return self.queue.pop(0)


class FakeTranscriber:
    def __init__(self):
        self.text = ""

    def transcribe(self, audio, filename, mime):
        return self.text


class Harness:
    def __init__(self, tmp_path):
        self.settings = Settings(data_dir=tmp_path)
        self.settings.ensure_dirs()
        self.db = DB(self.settings.db_path)
        ProductMaster(self.db).seed("2026-09-01T00:00:00+07:00")
        self.extractor = ScriptedExtractor()
        self.stt = FakeTranscriber()
        self.chatbot = Chatbot(self.db)
        self.pipeline = Pipeline(self.db, self.settings, self.extractor, self.stt,
                                 answer_question=self.chatbot.answer)
        self._ids = count(1)

    def send(self, ex: Extraction | None, *, text=None, image=None, audio=None, transcript=None,
             when="2026-09-26 09:00", msg_id=None):
        if ex is not None:
            self.extractor.queue.append(ex)
        kind = "image" if image is not None else "audio" if audio is not None else "text"
        if transcript is not None:
            self.stt.text = transcript
        msg = IncomingMessage(
            zalo_msg_id=msg_id or f"m{next(self._ids)}", user_id="founder", kind=kind,
            received_at=datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=TZ), text=text,
            media_bytes=image if image is not None else audio,
            media_mime="image/jpeg" if image is not None else "audio/aac" if audio is not None else None,
        )
        return self.pipeline.process(msg)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)
