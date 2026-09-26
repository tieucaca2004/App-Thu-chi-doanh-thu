"""Real-world validation harness (Phase 2). Test tooling only — not a product feature.

  python -m founder_assistant.realval preflight
      Which credentials/services are available (values are never printed). Missing -> BLOCKED.

  python -m founder_assistant.realval run validation/cases.json --out validation/out
      Feeds REAL voice / bill images / text through the REAL STT + Claude + pipeline in order,
      then scores every field against the ground truth written by a human:
        PASS    every field correct
        PARTIAL only non-money fields wrong (name, unit, date, supplier, doc type)
        FAIL    any money field wrong (amount, unit price, total, revenue) or an invented item
      Writes results.json + RESULTS.md (metrics in the REAL-WORLD-VALIDATION.md format).

  python -m founder_assistant.realval trace --db data/founder.db --day 2026-10-01
      Walks every number of that day's report back to the original file and checks its SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .analytics import daily_summary
from .config import Settings
from .db import DB
from .money import amounts_match, fmt_vnd
from .pipeline import IncomingMessage, Pipeline
from .products import ProductMaster
from .textnorm import norm_key
from .units import normalize_unit

MONEY_FIELDS = {"amount", "unit_price", "total", "revenue"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".heic": "image/heic", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".aac": "audio/aac",
        ".amr": "audio/amr", ".wav": "audio/wav", ".ogg": "audio/ogg"}


# ------------------------------------------------------------------ preflight
REQUIRED = {
    "Claude": ["ANTHROPIC_API_KEY"],
    "Zalo OA": ["ZALO_APP_ID", "ZALO_OA_SECRET_KEY", "ZALO_ACCESS_TOKEN", "ZALO_REFRESH_TOKEN", "ZALO_APP_SECRET",
                "FOUNDER_ZALO_USER_ID"],
    "Report download": ["PUBLIC_BASE_URL", "REPORT_LINK_SECRET"],
    "STT": ["STT_URL"],
    "Revenue source": ["PRIMARY_REVENUE_SOURCE"],
}


def preflight(ping: bool = True) -> dict:
    out: dict = {}
    for area, names in REQUIRED.items():
        missing = [n for n in names if not os.environ.get(n, "").strip()]
        out[area] = {"status": "BLOCKED" if missing else "CONFIGURED", "missing": missing}
    if os.environ.get("REPORT_LINK_SECRET", "change-me") == "change-me":
        rd = out["Report download"]
        rd["status"] = "BLOCKED"
        rd["missing"] = [m for m in rd["missing"] if m != "REPORT_LINK_SECRET"] + ["REPORT_LINK_SECRET (still default)"]
    out["ffmpeg"] = {"status": "CONFIGURED" if shutil.which("ffmpeg") else "MISSING",
                     "missing": [] if shutil.which("ffmpeg") else ["ffmpeg (needed for Zalo AAC/AMR voice)"]}
    if not ping:
        return out
    import httpx
    if out["Claude"]["status"] == "CONFIGURED":
        try:
            import anthropic
            anthropic.Anthropic().models.retrieve(Settings().claude_model)
            out["Claude"]["ping"] = "OK"
        except Exception as exc:  # noqa: BLE001
            out["Claude"].update(status="ERROR", ping=type(exc).__name__)
    if out["Zalo OA"]["status"] == "CONFIGURED":
        try:
            r = httpx.get("https://openapi.zalo.me/v2.0/oa/getoa",
                          headers={"access_token": os.environ["ZALO_ACCESS_TOKEN"]}, timeout=20).json()
            out["Zalo OA"]["ping"] = "OK" if r.get("error") == 0 else f"error {r.get('error')}"
            if r.get("error") != 0:
                out["Zalo OA"]["status"] = "ERROR"
        except Exception as exc:  # noqa: BLE001
            out["Zalo OA"].update(status="ERROR", ping=type(exc).__name__)
    if out["Report download"]["status"] == "CONFIGURED":
        try:
            ok = httpx.get(os.environ["PUBLIC_BASE_URL"].rstrip("/") + "/health", timeout=20).status_code == 200
            out["Report download"]["ping"] = "OK" if ok else "unreachable"
        except Exception as exc:  # noqa: BLE001
            out["Report download"].update(status="ERROR", ping=type(exc).__name__)
    return out


# ------------------------------------------------------------------ scoring
@dataclass
class FieldCheck:
    field: str
    expected: object
    actual: object
    ok: bool
    layer_hint: str = ""


@dataclass
class CaseResult:
    id: str
    kind: str
    grade: str = "PASS"
    checks: list[FieldCheck] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    transcript: str | None = None
    reply: str | None = None
    ref: str | None = None
    status: str | None = None


def _eq(field_name: str, expected, actual) -> bool:
    if expected is None or actual is None:
        return expected is None and actual is None  # expected null means the system must NOT invent a value
    if field_name in MONEY_FIELDS or field_name == "quantity":
        return amounts_match(float(expected), float(actual), 1, 0)
    if field_name == "unit":
        e = normalize_unit(str(expected))
        return (e.canonical if e else str(expected)) == actual
    if field_name in ("product", "supplier"):
        return norm_key(str(expected)) == norm_key(str(actual))
    return str(expected) == str(actual)


def _raw_items(db: DB, message_id: int) -> list[dict]:
    r = db.one("SELECT raw_json FROM ai_extractions WHERE message_id = ? ORDER BY id DESC", (message_id,))
    return json.loads(r["raw_json"]).get("items", []) if r else []


def _hint(field_name: str, expected, raw_item: dict | None, kind: str) -> str:
    """Which layer most likely produced the error (a hint for the human filling the bug report)."""
    raw_key = {"product": "name", "total": None, "revenue": None}.get(field_name, field_name)
    if raw_item is None or raw_key is None:
        return "CLAUDE EXTRACTION (or STT/OCR input)" if kind != "text" else "CLAUDE EXTRACTION"
    raw_val = raw_item.get(raw_key)
    if _eq(field_name, expected, raw_val):
        return "NORMALIZATION/VALIDATION (AI read it right, stored value differs)"
    return {"voice": "STT or CLAUDE EXTRACTION (check transcript)", "image": "OCR/VISION (Claude)"}.get(
        kind, "CLAUDE EXTRACTION")


def score_case(db: DB, pm: ProductMaster, case: dict, message_id: int | None, result: CaseResult) -> CaseResult:
    exp = case.get("expected") or {}
    kind = case["kind"]
    if "status" in exp:
        result.checks.append(FieldCheck("status", exp["status"], result.status, result.status == exp["status"]))
    if message_id is None:
        return _grade(result)
    purchase = db.one("SELECT * FROM purchases WHERE message_id = ?", (message_id,))
    sale = db.one("SELECT * FROM sales WHERE message_id = ?", (message_id,))
    extraction = db.one("SELECT doc_type FROM ai_extractions WHERE message_id = ? ORDER BY id DESC", (message_id,))
    if "doc_type" in exp:
        result.checks.append(FieldCheck("doc_type", exp["doc_type"], extraction["doc_type"] if extraction else None,
                                        bool(extraction) and extraction["doc_type"] == exp["doc_type"]))
    record = purchase or sale
    if record is not None:
        date_col = "purchase_date" if purchase else "sale_date"
        if "date" in exp:
            result.checks.append(FieldCheck("date", exp["date"], record[date_col],
                                            exp["date"] is None and record["date_source"] == "message"
                                            or record[date_col] == exp["date"]))
        if purchase and "supplier" in exp:
            result.checks.append(FieldCheck("supplier", exp["supplier"], purchase["supplier"],
                                            _eq("supplier", exp["supplier"], purchase["supplier"])))
        if "total" in exp:
            actual = (purchase["stated_total"] if purchase["stated_total"] is not None else purchase["computed_total"]) \
                if purchase else sale["revenue"]
            result.checks.append(FieldCheck("total", exp["total"], actual, _eq("total", exp["total"], actual),
                                            "" if _eq("total", exp["total"], actual) else _hint("total", None, None, kind)))
        if sale is not None:
            for f in ("bill_count", "cash", "bank_transfer", "e_wallet", "platform_fee"):
                if f in exp:
                    fname = "revenue" if f in ("cash", "bank_transfer", "e_wallet", "platform_fee") else f
                    result.checks.append(FieldCheck(f, exp[f], sale[f], _eq(fname, exp[f], sale[f])))
    if "items" in exp:
        _score_items(db, pm, exp["items"], purchase, sale, message_id, kind, result)
    return _grade(result)


def _score_items(db, pm, expected_items, purchase, sale, message_id, kind, result):
    if purchase is not None:
        rows = [dict(r) for r in db.all("SELECT pi.*, p.name AS product_name FROM purchase_items pi "
                                        "LEFT JOIN products p ON p.id = pi.product_id WHERE purchase_id = ? ORDER BY pi.id",
                                        (purchase["id"],))]
    elif sale is not None:
        rows = [dict(r) | {"product_name": r["raw_name"], "unit": None}
                for r in db.all("SELECT * FROM sale_items WHERE sale_id = ? ORDER BY id", (sale["id"],))]
    else:
        rows = []
    raw = _raw_items(db, message_id)
    unused = list(range(len(rows)))
    for e in expected_items:
        want = norm_key(e["product"])
        idx = next((i for i in unused if want in (norm_key(rows[i]["product_name"] or ""), norm_key(rows[i]["raw_name"]))), None)
        if idx is None:
            result.checks.append(FieldCheck(f"item[{e['product']}]", "present", "missing", False,
                                            _hint("product", e["product"], None, kind)))
            result.notes.append(f"missing item {e['product']}")
            continue
        unused.remove(idx)
        row = rows[idx]
        raw_item = raw[idx] if idx < len(raw) else None
        result.checks.append(FieldCheck(f"{e['product']}.product", e["product"], row["product_name"] or row["raw_name"], True))
        for f in ("quantity", "unit", "unit_price", "amount"):
            if f not in e:
                continue
            ok = _eq(f, e[f], row.get(f))
            result.checks.append(FieldCheck(f"{e['product']}.{f}", e[f], row.get(f), ok,
                                            "" if ok else _hint(f, e[f], raw_item, kind)))
    for i in unused:  # anything the system recorded that is not on the source = invented
        result.checks.append(FieldCheck("extra_item.amount", None, f"{rows[i]['raw_name']} {rows[i].get('amount')}",
                                        False, "CLAUDE EXTRACTION (invented / split item)"))


def _grade(r: CaseResult) -> CaseResult:
    bad = [c for c in r.checks if not c.ok]
    if not bad:
        r.grade = "PASS"
    elif any(c.field.split(".")[-1] in MONEY_FIELDS or c.field.startswith("item[") or c.field == "status" for c in bad):
        r.grade = "FAIL"
    else:
        r.grade = "PARTIAL"
    return r


# ------------------------------------------------------------------ run
def run_cases(spec: dict, base_dir: Path, out_dir: Path, extractor=None, transcriber=None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    if spec.get("primary_revenue_source"):
        os.environ["PRIMARY_REVENUE_SOURCE"] = spec["primary_revenue_source"]
    settings = Settings(data_dir=out_dir / "data")
    settings.ensure_dirs()
    if settings.db_path.exists():
        settings.db_path.unlink()  # every run starts clean: results must be reproducible
    db = DB(settings.db_path)
    pm = ProductMaster(db)
    pm.seed(datetime.now(settings.tz).isoformat(timespec="seconds"))
    if extractor is None:
        from .extraction import ClaudeExtractor
        extractor = ClaudeExtractor(settings.claude_model)
    if transcriber is None:
        from .stt import NoTranscriber, WhisperHTTPTranscriber
        transcriber = (WhisperHTTPTranscriber(settings.stt_url, settings.stt_api_key, settings.stt_model)
                       if settings.stt_url else NoTranscriber())
    from .chatbot import Chatbot
    pipeline = Pipeline(db, settings, extractor, transcriber, answer_question=Chatbot(db).answer)

    results: list[CaseResult] = []
    by_case: dict[str, dict] = {}
    for n, case in enumerate(spec["cases"], 1):
        kind = case["kind"]
        received = datetime.strptime(case["sent_at"], "%Y-%m-%d %H:%M").replace(tzinfo=settings.tz)
        data = mime = None
        if "TODO" in json.dumps(case.get("expected", {}), ensure_ascii=False):
            results.append(CaseResult(case["id"], kind, grade="NOT RUN", notes=["ground truth not filled (TODO)"]))
            by_case[case["id"]] = {"message_id": None, "ref": None}
            continue
        if case.get("file"):
            path = base_dir / case["file"]
            if not path.exists():  # real recording/photo not collected yet -> NOT RUN, never PASS
                results.append(CaseResult(case["id"], kind, grade="NOT RUN", notes=[f"missing file {case['file']}"]))
                by_case[case["id"]] = {"message_id": None, "ref": None}
                continue
            data, mime = path.read_bytes(), MIME.get(path.suffix.lower(), "application/octet-stream")
        msg = IncomingMessage(zalo_msg_id=case.get("msg_id") or f"realval-{case['id']}", user_id="realval",
                              kind=kind if kind != "voice" else "audio", received_at=received,
                              text=case.get("text"), media_bytes=data, media_mime=mime)
        r = CaseResult(case["id"], kind)
        try:
            res = pipeline.process(msg)
            r.reply, r.ref, r.status = res.reply, res.record_ref, res.status
            mid = res.message_id
        except Exception as exc:  # noqa: BLE001 - a crash is a result, not a harness error
            r.status, mid = "crashed", db.one("SELECT id FROM messages WHERE zalo_msg_id = ?", (msg.zalo_msg_id,))
            mid = mid["id"] if mid else None
            r.notes.append(f"{type(exc).__name__}: {exc}")
        if mid:
            m = db.one("SELECT transcript FROM messages WHERE id = ?", (mid,))
            r.transcript = m["transcript"] if m else None
        results.append(score_case(db, pm, case, mid, r))
        by_case[case["id"]] = {"message_id": mid, "ref": r.ref}

    price = _check_price_changes(db, spec.get("expected_price_changes", []), by_case)
    reports = _check_reports(db, spec.get("expected_reports", []), settings)
    traces = {r["date"]: trace_day(db, r["date"]) for r in reports}
    summary = metrics(results, spec, price, reports, traces)
    payload = {"generated_at": datetime.now(settings.tz).isoformat(timespec="seconds"),
               "extractor": getattr(extractor, "model_name", "?"), "cases": [_asdict(r) for r in results],
               "price_changes": price, "reports": reports, "traces": traces, "metrics": summary}
    (out_dir / "results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    (out_dir / "RESULTS.md").write_text(render_markdown(payload))
    return payload


def _asdict(r: CaseResult) -> dict:
    return {"id": r.id, "kind": r.kind, "grade": r.grade, "status": r.status, "ref": r.ref,
            "transcript": r.transcript, "reply": r.reply, "notes": r.notes,
            "checks": [c.__dict__ for c in r.checks]}


def _check_price_changes(db: DB, expected: list[dict], by_case: dict | None = None) -> list[dict]:
    """Each expectation names the case (bill/voice) whose price is compared with the previous one."""
    from .pricing import average_price, change_for
    out = []
    pm = ProductMaster(db)
    for e in expected:
        mid = (by_case or {}).get(e.get("case"), {}).get("message_id")
        if by_case is not None and mid is None:
            continue  # the case did not run -> nothing to score (never counted as pass or fail)
        pid = pm.match(e["product"])
        row = pid and mid and db.one(
            """SELECT ph.id, ph.base_unit FROM price_history ph JOIN purchase_items pi ON pi.id = ph.purchase_item_id
               JOIN purchases p ON p.id = pi.purchase_id WHERE ph.product_id = ? AND p.message_id = ?
               ORDER BY ph.id LIMIT 1""", (pid, mid))
        ch = change_for(db, row["id"]) if row else None
        got = {"old": ch.old_price if ch else None, "new": ch.new_price if ch else None,
               "diff": ch.diff if ch else None, "pct": round(ch.pct, 2) if ch else None}
        checks = {k: (e[k] is None and got[k] is None) or (got[k] is not None and abs(got[k] - e[k]) <= (0.01 if k == "pct" else 1))
                  for k in ("old", "new", "diff", "pct") if k in e}
        for days in (7, 30):
            key = f"avg_{days}d"
            if key in e and row:
                avg, _ = average_price(db, pid, row["base_unit"], e["date"], days)
                got[key] = round(avg) if avg is not None else None
                checks[key] = got[key] is not None and abs(got[key] - e[key]) <= 1
        out.append({"product": e["product"], "date": e.get("date"), "case": e.get("case"), "expected": e, "actual": got,
                    "pass": bool(checks) and all(checks.values()), "checks": checks})
    return out


def _check_reports(db: DB, expected: list[dict], settings: Settings) -> list[dict]:
    from .report import generate_daily_report
    out = []
    for e in expected:
        if "TODO" in json.dumps(e, ensure_ascii=False):
            continue  # ground truth not filled -> not scored
        text, path, s = generate_daily_report(db, e["date"], settings.reports_dir, datetime.now(settings.tz))
        rec = s.extra.get("reconciliation")
        got = {"revenue": s.revenue, "expense": s.expense, "net": s.net, "bill_count": s.bill_count,
               "reconciliation_matched": rec["matched"] if rec else None, "xlsx": str(path)}
        checks = {}
        for k in ("revenue", "expense", "net", "bill_count"):
            if k in e:
                checks[k] = (e[k] is None and got[k] is None) or (got[k] is not None and e[k] is not None
                                                                  and abs(got[k] - e[k]) <= 1)
        if "reconciliation_matched" in e:
            checks["reconciliation_matched"] = got["reconciliation_matched"] == e["reconciliation_matched"]
        if "forbidden_phrases" in e:
            checks["wording"] = not any(p.lower() in text.lower() for p in e["forbidden_phrases"])
        out.append({"date": e["date"], "expected": e, "actual": got, "checks": checks,
                    "pass": all(checks.values()), "text": text})
    return out


# ------------------------------------------------------------------ traceability
def _file_ok(db: DB, message_id: int) -> dict:
    m = db.one("SELECT id, kind, text, transcript FROM messages WHERE id = ?", (message_id,))
    media = db.one("SELECT file_path, sha256 FROM media WHERE message_id = ?", (message_id,))
    link = {"message": f"msg#{message_id}", "kind": m["kind"] if m else None}
    if m is None:
        return link | {"ok": False, "why": "message missing"}
    if media is None:
        ok = bool(m["text"] or m["transcript"])
        return link | {"ok": ok, "original": "text" if ok else None, "why": None if ok else "no original content"}
    p = Path(media["file_path"])
    if not p.exists():
        return link | {"ok": False, "original": str(p), "why": "original file missing"}
    same = hashlib.sha256(p.read_bytes()).hexdigest() == media["sha256"]
    return link | {"ok": same, "original": str(p), "why": None if same else "SHA-256 mismatch (file changed)"}


def _chain(db: DB, table: str, row_id: int) -> dict:
    col_ext = db.one(f"SELECT extraction_id, message_id FROM {table} WHERE id = ?", (row_id,))
    if col_ext is None:
        return {"ok": False, "why": f"{table}#{row_id} missing"}
    ext = db.one("SELECT id, message_id FROM ai_extractions WHERE id = ?", (col_ext["extraction_id"],))
    if ext is None or ext["message_id"] != col_ext["message_id"]:
        return {"ok": False, "why": "extraction link broken"}
    return {"record": f"{'M' if table == 'purchases' else 'B'}{row_id}", "extraction": f"extraction#{ext['id']}"} \
        | _file_ok(db, ext["message_id"])


def trace_day(db: DB, day: str) -> list[dict]:
    s = daily_summary(db, day)
    items = []

    def add(label, value, refs):
        chains = [_chain(db, "purchases" if r[0] == "M" else "sales", int(r[1:])) for r in refs]
        items.append({"number": label, "value": value, "chains": chains,
                      "ok": value is None or (bool(chains) and all(c["ok"] for c in chains))})

    add("Doanh thu", s.revenue, s.revenue_refs)
    add("Tổng chi", s.expense, s.expense_refs)
    for r in s.purchase_rows:
        if r["amount"] is not None:
            add(f"Chi {r['raw_name']} ({r['ref']})", r["amount"], [r["ref"]])
    for r in s.sale_rows:
        if r["amount"] is not None:
            add(f"Bán {r['name']} ({r['ref']})", r["amount"], [r["ref"]])
    for c in s.price_changes:
        refs = []
        for ph in (c.old_ref, c.new_ref):
            row = db.one("SELECT pi.purchase_id FROM price_history ph JOIN purchase_items pi ON pi.id = ph.purchase_item_id "
                         "WHERE ph.id = ?", (ph,))
            if row:
                refs.append(f"M{row['purchase_id']}")
        add(f"Giá {c.product_name} {c.pct:+.2f}%", c.new_price, refs)
    return items


# ------------------------------------------------------------------ metrics & markdown
def metrics(results: list[CaseResult], spec: dict, price: list, reports: list, traces: dict) -> dict:
    def count(pred_fields):
        checks = [c for r in results for c in r.checks if pred_fields(c.field)]
        return sum(c.ok for c in checks), len(checks)

    def by_kind(kind):
        rs = [r for r in results if r.kind == kind]
        rs = [r for r in rs if r.grade != "NOT RUN"]
        return {"cases": len(rs), "not_run": sum(r.kind == kind for r in results) - len(rs),
                "pass": sum(r.grade == "PASS" for r in rs),
                "partial": sum(r.grade == "PARTIAL" for r in rs), "fail": sum(r.grade == "FAIL" for r in rs)}

    dup = [r for r, c in zip(results, spec["cases"]) if c.get("tags") and "duplicate" in c["tags"] and r.grade != "NOT RUN"]
    rec = [r for r in reports if "reconciliation_matched" in r["expected"] or "revenue" in r["expected"]]
    trace_items = [i for day in traces.values() for i in day if i["value"] is not None]  # empty != traced
    return {
        "voice": by_kind("voice"), "image": by_kind("image"), "text": by_kind("text"),
        "money": count(lambda f: f.split(".")[-1] in MONEY_FIELDS),
        "product": count(lambda f: f.endswith(".product") or f.startswith("item[")),
        "unit": count(lambda f: f.endswith(".unit")),
        "quantity": count(lambda f: f.endswith(".quantity")),
        "total": count(lambda f: f == "total"),
        "price_comparison": (sum(p["pass"] for p in price), len(price)),
        "duplicate_detection": (sum(r.grade == "PASS" for r in dup), len(dup)),
        "revenue_reconciliation": (sum(r["pass"] for r in rec), len(rec)),
        "report": "PASS" if reports and all(r["pass"] for r in reports) else ("NOT RUN" if not reports else "FAIL"),
        "traceability": (sum(i["ok"] for i in trace_items), len(trace_items)),
    }


def render_markdown(p: dict) -> str:
    m = p["metrics"]

    def frac(t):
        return f"{t[0]}/{t[1]}" if t[1] else "NOT RUN (0 cases)"

    def kind(k):
        v = m[k]
        run = (f"{v['cases']} cases — Correct (PASS): {v['pass']} · Partial: {v['partial']} · Incorrect (FAIL): {v['fail']}"
               if v["cases"] else "NOT RUN (0 cases)")
        return run + (f" · NOT RUN: {v['not_run']}" if v["not_run"] else "")

    lines = [f"# Real-world validation results — {p['generated_at']}", "", f"Extractor: `{p['extractor']}`", "",
             "```text",
             f"VOICE TEST        {kind('voice')}", f"IMAGE TEST        {kind('image')}", f"TEXT TEST         {kind('text')}",
             f"MONEY EXTRACTION  Correct: {frac(m['money'])}", f"PRODUCT           Correct: {frac(m['product'])}",
             f"UNIT              Correct: {frac(m['unit'])}", f"QUANTITY          Correct: {frac(m['quantity'])}",
             f"TOTAL             Correct: {frac(m['total'])}", f"PRICE COMPARISON  Correct: {frac(m['price_comparison'])}",
             f"DUPLICATES        Correct: {frac(m['duplicate_detection'])}",
             f"REVENUE RECONCIL. Correct: {frac(m['revenue_reconciliation'])}",
             f"TRACEABILITY      Traced: {frac(m['traceability'])}", f"REPORT            {m['report']}", "```", "",
             "## Cases", "", "| Case | Kind | Grade | Status | Wrong fields (expected → actual, layer hint) |", "|---|---|---|---|---|"]
    for c in p["cases"]:
        wrong = "; ".join(f"{x['field']}: {x['expected']} → {x['actual']}" + (f" [{x['layer_hint']}]" if x["layer_hint"] else "")
                          for x in c["checks"] if not x["ok"]) or "—"
        lines.append(f"| {c['id']} | {c['kind']} | **{c['grade']}** | {c['status']} | {wrong} |")
    voice = [c for c in p["cases"] if c["transcript"]]
    if voice:
        lines += ["", "## Transcripts (for STT vs extraction diagnosis)", ""]
        lines += [f"- **{c['id']}**: {c['transcript']}" for c in voice]
    if p["price_changes"]:
        lines += ["", "## Price comparison", "", "| Product | Date | Expected | Actual | Result |", "|---|---|---|---|---|"]
        for x in p["price_changes"]:
            lines.append(f"| {x['product']} | {x['date']} | {x['expected']} | {x['actual']} | {'PASS' if x['pass'] else 'FAIL'} |")
    for r in p["reports"]:
        lines += ["", f"## Report {r['date']} — {'PASS' if r['pass'] else 'FAIL'}", "", f"Checks: {r['checks']}", "",
                  "```text", r["text"], "```"]
    for day, items in p["traces"].items():
        lines += ["", f"## Traceability {day}", "", "| Number | Value | Chain | OK |", "|---|---|---|---|"]
        for i in items:
            chain = " / ".join(f"{c.get('record')}→{c.get('extraction')}→{c.get('message')}→{Path(c['original']).name if c.get('original') and c.get('original') != 'text' else c.get('original')}"
                               + (f" ({c['why']})" if c.get("why") else "") for c in i["chains"]) or "—"
            lines.append(f"| {i['number']} | {fmt_vnd(i['value'])} | {chain} | {'PASS' if i['ok'] else 'FAIL'} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m founder_assistant.realval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight")
    pf.add_argument("--no-ping", action="store_true")
    rn = sub.add_parser("run")
    rn.add_argument("cases")
    rn.add_argument("--out", default="validation/out")
    tr = sub.add_parser("trace")
    tr.add_argument("--db", default=str(Settings().db_path))
    tr.add_argument("--day", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "preflight":
        res = preflight(ping=not a.no_ping)
        for area, v in res.items():
            print(f"{area:16} {v['status']:10} {('missing: ' + ', '.join(v['missing'])) if v['missing'] else ''} {v.get('ping', '')}")
        return 0 if all(v["status"] == "CONFIGURED" for v in res.values()) else 2
    if a.cmd == "run":
        spec_path = Path(a.cases)
        payload = run_cases(json.loads(spec_path.read_text()), spec_path.parent, Path(a.out))
        print((Path(a.out) / "RESULTS.md").read_text())
        return 0
    items = trace_day(DB(a.db), a.day)
    for i in items:
        print(("PASS " if i["ok"] else "FAIL ") + f"{i['number']}: {fmt_vnd(i['value'])}  {i['chains']}")
    return 0 if all(i["ok"] for i in items) else 1


if __name__ == "__main__":
    sys.exit(main())
