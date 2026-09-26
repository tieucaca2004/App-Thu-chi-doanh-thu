"""BUG-014 / BUG-015 — reproduced on a real local server during the security audit (§17)."""
import hashlib
import hmac
import threading

import pytest
from fastapi.testclient import TestClient

from founder_assistant.app import Service, create_app
from founder_assistant.config import Settings
from founder_assistant.db import DB


class _Zalo:
    def __init__(self):
        self.sent = []

    def send_text(self, uid, text):
        self.sent.append((uid, text))


def _svc(tmp_path, **kw):
    svc = Service.__new__(Service)
    svc.s = Settings(data_dir=tmp_path, founder_zalo_user_id="founder-1", public_base_url="https://x.example", **kw)
    svc.s.ensure_dirs()
    svc.db, svc.zalo, svc.lock, svc.pipeline = DB(svc.s.db_path), _Zalo(), threading.Lock(), None
    return svc


def _client(svc):
    return TestClient(create_app(service=svc, schedule=False))


def test_jobs_disabled_without_token(tmp_path):
    with _client(_svc(tmp_path, jobs_token="")) as c:
        r = c.post("/jobs/daily-report", params={"day": "2026-09-26", "send": "false"})
        assert r.status_code == 503 and "FOUNDER DAILY REPORT" not in r.text
        assert c.post("/jobs/reprocess-failed").status_code == 503


def test_jobs_require_bearer_token(tmp_path):
    svc = _svc(tmp_path, jobs_token="t0ken-long-random")
    with _client(svc) as c:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "t0ken-long-random"}):
            r = c.post("/jobs/daily-report", params={"day": "2026-09-26", "send": "false"}, headers=headers)
            assert r.status_code == 401 and "FOUNDER DAILY REPORT" not in r.text
        r = c.post("/jobs/daily-report", params={"day": "2026-09-26", "send": "false"},
                   headers={"Authorization": "Bearer t0ken-long-random"})
        assert r.status_code == 200 and "FOUNDER DAILY REPORT" in r.json()["text"]


@pytest.mark.parametrize("secret", ["change-me", ""])
def test_default_report_secret_cannot_be_forged(tmp_path, secret):
    svc = _svc(tmp_path, report_link_secret=secret, jobs_token="tk")
    with _client(svc) as c:
        c.post("/jobs/daily-report", params={"day": "2026-09-26", "send": "false"}, headers={"Authorization": "Bearer tk"})
        forged = hmac.new(secret.encode(), b"2026-09-26", hashlib.sha256).hexdigest()[:32]
        assert c.get("/reports/2026-09-26.xlsx", params={"sig": forged}).status_code == 503
    assert svc.report_link("2026-09-26") is None  # never hand out a forgeable link


def test_real_secret_download_works(tmp_path):
    svc = _svc(tmp_path, report_link_secret="a-long-random-secret", jobs_token="tk")
    with _client(svc) as c:
        c.post("/jobs/daily-report", params={"day": "2026-09-26", "send": "false"}, headers={"Authorization": "Bearer tk"})
        link = svc.report_link("2026-09-26")
        assert c.get(link.replace("https://x.example", "")).status_code == 200
        assert c.get("/reports/2026-09-26.xlsx", params={"sig": "0" * 32}).status_code == 403
