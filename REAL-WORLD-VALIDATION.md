# REAL-WORLD-VALIDATION — AI Founder Data Assistant V1

> **Kết luận: NOT PRODUCTION READY.**
> Chưa có phần nào được kiểm chứng bằng dữ liệu thật. Môi trường thực hiện Phase 2 không có credential (Claude, Zalo OA, STT),
> không có ảnh bill thật và không có voice thật. Mọi kết quả dưới đây từ test tự động là **mock/synthetic PASS**, không phải **production PASS**.

Ngày: 2026-09-26 · Nhánh: `claude/gallant-pascal-jphd3b`

## 1. Environment

| Mục | Giá trị |
|---|---|
| Baseline trước Phase 2 | commit `c944ae4`, working tree sạch, `pytest`: **53 passed** |
| Sau Phase 2 | `pytest`: **113 passed** (xem §14–15) |
| Python / SQLite | 3.11 / sqlite3 stdlib |
| ffmpeg | **MISSING** trong môi trường này (cần cho voice Zalo AAC/AMR) |
| Dữ liệu thật | **Không có**: 0 ảnh bill, 0 voice, 0 tin nhắn Zalo |

Kết quả `python -m founder_assistant.realval preflight` (không in giá trị secret):
```text
Claude           BLOCKED    missing: ANTHROPIC_API_KEY
Zalo OA          BLOCKED    missing: ZALO_APP_ID, ZALO_OA_SECRET_KEY, ZALO_ACCESS_TOKEN, ZALO_REFRESH_TOKEN, ZALO_APP_SECRET, FOUNDER_ZALO_USER_ID
Report download  BLOCKED    missing: PUBLIC_BASE_URL, REPORT_LINK_SECRET (still default)
STT              BLOCKED    missing: STT_URL
Revenue source   BLOCKED    missing: PRIMARY_REVENUE_SOURCE
ffmpeg           MISSING    missing: ffmpeg (needed for Zalo AAC/AMR voice)
```

## 2. API status (Claude)
**BLOCKED**: không có `ANTHROPIC_API_KEY`. Đã kiểm được (bằng HTTP giả lập, không phải API thật): request đúng dạng (structured output, effort, fallback),
parse kết quả, và xử lý khi model từ chối (xem BUG-012). Độ chính xác đọc bill/voice của Claude: **chưa đo**.

## 3. Zalo status
**BLOCKED**: không có credential OA. Đã kiểm bằng test: parse sự kiện, chữ ký `X-ZEvent-Signature`, chỉ nhận tin Founder, link tải có chữ ký (200/403).
Chưa kiểm: webhook thật, định dạng thật của payload voice/ảnh, gửi tin trả lời thật, refresh token thật.

## 4. STT status
**BLOCKED**: không có `STT_URL`. Provider được hỗ trợ: mọi endpoint Whisper-compatible `/audio/transcriptions`
(OpenAI `whisper-1`/`gpt-4o-transcribe`, hoặc faster-whisper tự host), `language=vi`. Đã thêm (BUG-006): timeout, retry có backoff,
không retry lỗi 4xx, trạng thái lỗi `needs_transcription`, lưu audio gốc trước khi STT, chuyển AAC/AMR → WAV bằng ffmpeg.
**Giả định chưa kiểm chứng**: định dạng file voice Zalo thật (AAC/AMR/M4A). Hệ thống ưu tiên `Content-Type` thật khi tải về.

## 5. Claude status
**BLOCKED**, như §2.

## 6. Voice accuracy
```text
VOICE TEST
10 cases (kịch bản ở validation/cases.example.json, ground truth suy từ chính câu nói)
Correct: NOT RUN
Incorrect: NOT RUN
```
Lý do: chưa có file voice thật của Founder, chưa có STT, chưa có Claude.

## 7. Image accuracy
```text
IMAGE TEST
20 cases
Correct: NOT RUN
Incorrect: NOT RUN
```
Lý do: chưa có bill thật, chưa có Claude.

## 8. Money accuracy
```text
MONEY EXTRACTION (thật)       Correct: NOT RUN
PARSER TIỀN (unit test)       47/47 test case PASS  (synthetic)
```
Parser dùng để đối chiếu số AI đọc với chuỗi gốc đã được kiểm với: `1.000`, `10.000`, `100.000`, `1.000.000`, `10.500`, `10.500.000`,
dấu phẩy, `ngàn/nghìn/triệu/trăm`, số thập phân (`10,5 triệu`, `2,5tr`), tiền bằng chữ (`bốn trăm năm chục ngàn`, `một triệu hai`,
`hai triệu rưỡi`, `một trăm lẻ năm nghìn`), và `90.000` đọc thành `900.000` → **bị bắt**.
Đây là kiểm chứng *bộ kiểm tra*, không phải kiểm chứng *độ đọc đúng* trên bill thật.

## 9. Product accuracy
```text
PRODUCT EXTRACTION (thật)     Correct: NOT RUN
UNIT EXTRACTION (thật)        Correct: NOT RUN
TOTAL CALCULATION (thật)      Correct: NOT RUN
```
Synthetic: `thịt heo/thịt lợn/heo` → cùng sản phẩm; `tôm sú/tôm thẻ/tôm càng/tôm` → 4 sản phẩm khác nhau; `hành lá≠hành tím`,
`gà ta≠gà công nghiệp` (BUG-001). Tên chưa có mapping → sản phẩm mới + nhắn Founder "gộp … = …", không bao giờ so giá với sản phẩm khác.
Đơn vị: `kg/ký/kí/cân/ki lô`, `g`, `lạng` quy về kg; `thùng/chai/bình/lon/con/cái/phần` không bao giờ so với kg.

## 10. Price comparison
```text
PRICE COMPARISON (thật)       Correct: NOT RUN
```
Synthetic PASS: 88.000 → 92.000 = +4.000 (+4,55%); 118.000 → 125.000 (+5,93%); 28.000 → 24.000 (−14,29%); TB 7/30 ngày;
chỉ so cùng sản phẩm + cùng đơn vị gốc; "xu hướng" chỉ khi ≥ 4 lần ghi giá và gắn nhãn *(Nhận định)*.
Mới (BUG-010): giá lệch ≥ 3 lần so với lần trước → giữ lại chờ xác nhận (dấu hiệu đọc sai số 0).

## 11. Revenue reconciliation
```text
REVENUE RECONCILIATION (thật) Correct: NOT RUN
```
Đã thay rule cũ "có chốt ca → bỏ bill" (BUG-002):
- `PRIMARY_REVENUE_SOURCE` (`pos_closing` | `sales_bills`) do **Founder xác nhận** là nguồn doanh thu chính; nguồn còn lại chỉ để đối chiếu và phân tích món.
- POS 5.000.000 + bill 5.000.000 → doanh thu 5.000.000, không phải 10.000.000.
- POS 5.000.000 vs bill 4.850.000 → alert *"POS: 5.000.000đ; Bill evidence: 4.850.000đ; Chênh lệch: 150.000đ. Cần Founder xác nhận"*, số không bị sửa.
- Chưa cấu hình và ngày có cả hai nguồn → doanh thu **UNKNOWN** + alert critical (không tự chọn).
- **Việc của Founder**: xác nhận nguồn doanh thu chính (hiện `PRIMARY_REVENUE_SOURCE` chưa đặt).

## 12. Daily report
```text
REPORT (thật)                 NOT RUN
```
Synthetic: 6 sheet `SUMMARY, PURCHASE, SALES, PRICE_CHANGE, ALERTS, SOURCES`; dùng "THU – CHI", ghi rõ *chưa phải lợi nhuận ròng*;
giá trị thiếu hiển thị `UNKNOWN`, không phải 0. Kiểm bằng mắt file XLSX synthetic tìm ra BUG-011 (đã sửa).
**Chưa** kiểm bằng mắt với một ngày dữ liệu kinh doanh thật.

## 13. Source traceability
```text
TRACEABILITY (thật)           NOT RUN (0 số liệu thật)
```
Công cụ: `python -m founder_assistant.realval trace --db <db> --day <ngày>`. Với mỗi con số trong báo cáo (doanh thu, tổng chi,
từng dòng chi/bán, từng thay đổi giá) nó đi báo cáo → `M/B` → extraction → message → file gốc và **kiểm SHA-256 của file gốc**
(file bị sửa sau này → FAIL). Self-test đã chứng minh công cụ này bắt được file gốc bị sửa.

## 14. Bugs found
Tất cả phát hiện bằng **rà soát theo spec Phase 2, self-test của harness và kiểm XLSX bằng mắt trên dữ liệu synthetic**,
không phải từ dữ liệu thật. Mỗi bug có regression test; toàn bộ suite PASS sau mỗi lần sửa.

| BUG | Input | Expected | Actual (trước khi sửa) | Layer | Root cause | Fix | Regression test | Status |
|---|---|---|---|---|---|---|---|---|
| 001 | "hành lá", "hành tím"; "gà ta", "gà công nghiệp"; "thịt heo nạc" | Sản phẩm khác nhau | Seed alias gộp chung → so giá sai | NORMALIZATION | Alias seed quá rộng | Chỉ giữ từ đồng nghĩa thật | `test_product_synonyms_and_varieties`, `test_unmapped_variety_…` | FIXED |
| 002 | POS + bill lẻ cùng ngày | Nguồn chính do Founder xác nhận + alert đối chiếu | Luôn lấy POS, bỏ bill, không đối chiếu | REPORT (analytics) | Rule đơn giản hóa | `PRIMARY_REVENUE_SOURCE`, `reconciliation()`, UNKNOWN khi chưa cấu hình; kiểm bất thường doanh thu cũng dùng rule này | `test_revenue_*` (5 test) | FIXED |
| 003 | "gas 500 ngàn", lương, điện | 8 nhóm chi, không báo thiếu đơn vị | Chỉ 2 nhóm | CLAUDE EXTRACTION schema | Schema V1 | Enum 8 nhóm; chỉ hàng hóa cần đơn vị | `test_expense_categories_without_unit` | FIXED |
| 004 | Claude/mạng lỗi | Tự thử lại thật như đã hứa | Bot hứa "sẽ xử lý lại" nhưng chỉ có endpoint chạy tay | PIPELINE/APP | Không có vòng retry | Vòng retry nền, tối đa `MAX_ATTEMPTS`, báo Founder khi bỏ cuộc; `unreadable` không tự retry (tránh trùng khi Founder gửi lại) | `test_retry_gives_up_…`, `test_unreadable_…` | FIXED |
| 005 | Tải ảnh Zalo lỗi | Retry tải lại được | URL media không được lưu → retry không bao giờ thành công | MEDIA DOWNLOAD | Thiếu cột | Lưu `media_url/media_mime/attempts` (+ migration DB cũ) | `test_failed_download_is_retried_…`, `test_old_database_is_migrated` | FIXED |
| 006 | Voice Zalo, STT chập chờn | Retry, timeout, lỗi rõ ràng | Không retry; định dạng AAC/AMR có thể bị STT từ chối | STT | — | Retry/backoff, không retry 4xx, transcode ffmpeg, ưu tiên Content-Type thật | `tests/test_stt.py` (7 test) | FIXED (chưa kiểm với STT thật) |
| 007 | "ba trăm sáu chục" đọc thành 3.600.000 | Bị giữ lại | Tiền bằng chữ không đối chiếu được → lọt | VALIDATION | Parser chỉ đọc chữ số | Parser số tiếng Việt; quy ước bỏ "nghìn" chỉ chấp nhận đúng ×1000 | `test_money_matrix`, `test_spoken_amount_*`, `test_implicit_thousand_only_exact` | FIXED |
| 008 | "ki lô" | kg | UNKNOWN | NORMALIZATION | Thiếu alias | Thêm alias | `test_ki_lo_unit` | FIXED |
| 009 | "hết khoảng một triệu hai" | Chờ xác nhận | Ghi như số chính xác | VALIDATION | Không nhận biết ước lượng | Từ ước lượng → `needs_confirmation` | `test_approximate_amount_needs_confirmation` | FIXED |
| 010 | Bill đọc sai ×10 nhất quán (dòng và tổng đều 4.600.000) | Chờ xác nhận | Qua mọi kiểm tra số học → ghi sai chi phí | VALIDATION | Kiểm tra số học không bắt được lỗi nhất quán | Giá lệch ≥3× so với lần trước → giữ lại | `test_consistent_x10_misread_is_held` | FIXED |
| 011 | Báo cáo có doanh thu từ POS | SOURCES liệt kê bill POS | Thiếu message POS và bill giá cũ | REPORT | Chỉ lấy message có dòng hàng | Thêm nguồn của mọi con số tiêu đề và giá cũ/mới | `test_5_daily_report` | FIXED |
| 012 | Claude từ chối trả lời | Báo Founder gửi lại | Lỗi parse làm hỏng pipeline | CLAUDE EXTRACTION | SDK lỗi trước khi kiểm `stop_reason` | Bọc thành `ExtractionError` *(sửa trong V1, ghi lại ở đây cho đủ)* | `test_refusal_raises` | FIXED |

Không có bug nào từ dữ liệu thật, vì **chưa chạy dữ liệu thật**.

## 15. Bugs fixed
BUG-001 … BUG-012: đã sửa, có regression test, toàn bộ suite **113 passed**. Các module không liên quan không bị refactor.

## 16. Remaining limitations / chưa kiểm chứng
1. Toàn bộ §2–§13 ở trạng thái **BLOCKED / NOT RUN** cho tới khi có credential và dữ liệu thật.
2. Chưa có ground truth cho 20 bill và 10 voice thật (template: `validation/cases.example.json`).
3. Định dạng voice Zalo thật chưa biết → cần ffmpeg trên server.
4. Đối chiếu nguồn cho ảnh dựa trên OCR do chính Claude chép lại: nếu Claude đọc sai *nhất quán* thì kiểm tra nội bộ không bắt được, trừ khi giá lệch ≥ 3× (BUG-010). Chỉ ground truth thật mới đo được độ chính xác này.
5. Chưa đo tỷ lệ "cảnh báo nhầm" (Founder phải xác nhận nhiều quá) trên dữ liệu thật: từ ước lượng ("hơn", "gần"…) và ngưỡng 3× có thể cần chỉnh.
6. Voice có câu tự sửa ("ba ký, à không, bốn ký") phụ thuộc hoàn toàn vào Claude, không có kiểm tra xác định.
7. `PRIMARY_REVENUE_SOURCE` chưa được Founder xác nhận.

## 17. Production readiness

| Gate | Trạng thái |
|---|---|
| Zalo OA thật | **BLOCKED** |
| STT thật | **BLOCKED** |
| Claude thật | **BLOCKED** |
| Image thật | **NOT RUN** |
| Voice thật | **NOT RUN** |
| Database thật | **NOT RUN** (SQLite chạy đúng trên dữ liệu synthetic) |
| Price comparison | **NOT RUN** (synthetic PASS) |
| Revenue reconciliation | **NOT RUN** (synthetic PASS) |
| Daily report | **NOT RUN** (synthetic PASS) |
| Source traceability | **NOT RUN** (công cụ sẵn sàng) |
| Automated tests | **PASS** (113/113) |
| Known critical bugs | Không có bug mở đã biết, nhưng dữ liệu thật chưa được thử |

## **NOT PRODUCTION READY**

### Để chuyển trạng thái (theo thứ tự)
1. Điền `.env` (xem `.env.example`), cài ffmpeg, chạy `python -m founder_assistant.realval preflight`: tất cả phải CONFIGURED + ping OK.
2. Founder xác nhận `PRIMARY_REVENUE_SOURCE`.
3. Thu thập 10 voice + 20 bill thật + chốt ca POS, ghi ground truth (`validation/README.md`).
4. `python -m founder_assistant.realval run validation/cases.json`, rồi dán khối metrics của `validation/out/RESULTS.md` vào §6–§13, ghi bug vào §14.
5. Chạy Zalo E2E thật (validation/README.md §6) và ít nhất một ngày kinh doanh thật; `trace` ≥ 10 con số; mở XLSX kiểm bằng mắt.
6. Chỉ khi mọi gate ở §17 là PASS mới được ghi PRODUCTION READY.
