# AI Founder Data Assistant (V1)

> **Trạng thái: NOT PRODUCTION READY.** Chưa kiểm chứng với Claude/Zalo/STT thật và bill/voice thật.
> Xem [`REAL-WORLD-VALIDATION.md`](REAL-WORLD-VALIDATION.md) và quy trình kiểm chứng ở [`validation/README.md`](validation/README.md).

Founder chỉ cần **nói hoặc chụp** gửi vào Zalo OA. Hệ thống tự đọc, lọc, chuẩn hóa, lưu, so sánh giá,
và cuối ngày gửi **Founder Daily Report** (tin nhắn Zalo + file `Founder-Daily-Report-YYYY-MM-DD.xlsx`).

```
Zalo OA ─► /webhook/zalo ─► lưu raw (text / ảnh / voice) ─► STT (voice) ─► Claude đọc (structured output)
        ─► VALIDATION (Python: tính toán, đối chiếu nguồn, trùng lặp, đơn vị, ngày)
        ─► purchases / sales / price_history / alerts ─► trả lời ngắn trên Zalo
        ─► 21:30 hằng ngày: analytics ─► báo cáo text + XLSX ─► Zalo
```

## Nguyên tắc chống sai số (đã có test)

| Nguyên tắc | Cách làm |
|---|---|
| AI chỉ **đọc**, không **tính** | Claude trả về đúng những gì ghi trên nguồn + đoạn trích nguyên văn (`evidence`). Đơn giá, thành tiền, tổng, % thay đổi đều do Python tính và được đánh dấu `source` / `computed`. |
| Không bịa số | Trường không có trong nguồn → `null` → hiển thị `UNKNOWN`, không bao giờ là 0. |
| Số phải truy được nguồn | Mỗi dòng hàng và mỗi con số phải xuất hiện trong text/transcript/OCR gốc; chuỗi tiền gốc (`"450 ngàn"`) được parse lại để đối chiếu. Không khớp → **chưa đưa vào báo cáo** cho đến khi Founder xác nhận. |
| Tổng không khớp | `SL × đơn giá ≠ thành tiền` hoặc `tổng dòng ≠ tổng bill` → cảnh báo, giữ lại chờ xác nhận. |
| Không nhầm đơn vị | `ký/kí/kg/cân`, `g`, `lạng`… quy về kg; `thùng`, `chai`, `lon`… là đơn vị riêng, không bao giờ so với kg. Đơn vị lạ → `unit = null`, `needs_confirmation`, không so giá. |
| Không cộng trùng | Trùng `message id` (Zalo gửi lại) → bỏ qua; trùng hash ảnh → báo trùng, không gọi AI; trùng nội dung (ngày + hàng + tổng) → chờ xác nhận. |
| Nguồn doanh thu | `PRIMARY_REVENUE_SOURCE` (`pos_closing`/`sales_bills`) do Founder xác nhận; nguồn còn lại chỉ để đối chiếu, lệch → alert, không sửa số. Chưa cấu hình mà có cả hai → UNKNOWN. |
| Số tiền ước lượng / đọc sai số 0 | "khoảng…", "tầm…" → chờ xác nhận. Giá lệch ≥ 3× lần mua trước → chờ xác nhận. Tiền bằng chữ ("ba trăm sáu chục") được đối chiếu. |
| FACT vs INFERENCE | % thay đổi là phép tính trên số liệu thật. Câu "xu hướng" chỉ xuất hiện khi có ≥ 4 lần ghi giá, và được gắn nhãn `(Nhận định)`; ít hơn → "Không đủ dữ liệu để kết luận xu hướng." |
| Thuật ngữ đúng | Báo cáo dùng **"Thu – chi"**, ghi rõ *chưa phải lợi nhuận ròng*. |
| Giữ raw data | Ảnh, voice, text, transcript, OCR, JSON AI đều được giữ; hủy bill chỉ đổi trạng thái. Lỗi hạ tầng (API, tải ảnh) → tự thử lại mỗi `RETRY_INTERVAL_SECONDS`, tối đa `MAX_ATTEMPTS` lần, rồi báo Founder. |
| Truy vết | Báo cáo → `M12`/`B7` → `ai_extractions` → `messages` → file gốc (đường dẫn + SHA-256). Sheet `SOURCES` và các cột *Mã chứng từ / Tin nhắn / Trích nguồn* trong XLSX. |

## Lệnh Founder nhắn trên Zalo

| Nhắn | Tác dụng |
|---|---|
| (ảnh bill / voice / text) | Ghi nhận. Trả lời mã chứng từ: `M…` = mua/chi, `B…` = bán/doanh thu |
| `ok M12` | Xác nhận chứng từ đang chờ → đưa vào báo cáo, ghi lịch sử giá |
| `hủy M12` | Bỏ chứng từ (dữ liệu gốc vẫn giữ) |
| `gộp ba chỉ = thịt heo` | Thêm tên gọi khác cho mặt hàng, gộp lịch sử giá |
| "Hôm nay chi bao nhiêu?", "Giá tôm lần gần nhất?", "Cho tôi báo cáo hôm qua"… | Chatbot: AI chỉ phân loại câu hỏi, số liệu lấy bằng SQL từ dữ liệu đã xác nhận, kèm nguồn |

## Cài đặt

```bash
pip install -r requirements.txt
cp .env.example .env         # điền ANTHROPIC_API_KEY, ZALO_*, FOUNDER_ZALO_USER_ID, (STT_URL)
set -a; . ./.env; set +a
uvicorn founder_assistant.app:app --host 0.0.0.0 --port 8000
```

- Cấu hình Webhook URL trong Zalo OA: `https://<domain>/webhook/zalo`, bật các sự kiện
  `user_send_text`, `user_send_image`, `user_send_audio`, `user_send_file`. Chữ ký `X-ZEvent-Signature` được kiểm tra khi có `ZALO_OA_SECRET_KEY`.
- Chỉ nhận tin từ `FOUNDER_ZALO_USER_ID` (nếu để trống: gắn với người gửi đầu tiên và ghi cảnh báo trong log).
- Access token Zalo tự refresh khi hết hạn (cần `ZALO_APP_SECRET` + `ZALO_REFRESH_TOKEN`).
- **Voice**: Claude không nhận audio, nên cần một dịch vụ STT tương thích Whisper (`STT_URL`, ví dụ faster-whisper server tự host
  hoặc API `/v1/audio/transcriptions`). Chưa cấu hình → voice vẫn được lưu và bot nhờ Founder gõ lại.
- **Báo cáo**: tự chạy lúc `DAILY_REPORT_TIME` (mặc định 21:30, giờ VN). Có thể gọi tay `POST /jobs/daily-report?day=2026-09-26`.
  Zalo OA không gửi được file .xlsx, nên bot gửi link tải có chữ ký (`PUBLIC_BASE_URL`); file luôn nằm trong `DATA_DIR/reports/`.
- Model mặc định `claude-opus-5` (đổi bằng `CLAUDE_MODEL`), bật server-side fallback khi model từ chối.
- Thử nhanh không cần Zalo: `DEV_ENDPOINTS=1`, rồi `curl -X POST localhost:8000/dev/message -d '{"text":"mua 5 ký thịt heo 450 ngàn"}' -H 'content-type: application/json'`.

Nhóm chi: `ingredient, packaging, gas, transport, platform_fee, salary, utilities, other`. Chỉ hàng hóa (ingredient, packaging) cần đơn vị và được theo dõi giá.

## Kiểm chứng thực tế

```bash
python -m founder_assistant.realval preflight                       # credential nào thiếu → BLOCKED
python -m founder_assistant.realval run       # chấm PASS/PARTIAL/FAIL theo ground truth
python -m founder_assistant.realval trace --db data/founder.db --day 2026-10-01   # truy vết từng con số tới file gốc
```

## Test

```bash
python -m pytest -q
```

`tests/test_acceptance.py` bám theo 5 acceptance test ở mục 30 của spec (voice, bill, thay đổi giá, bán hàng, báo cáo ngày)
và kiểm tra **dữ liệu lưu + con số tính ra + nội dung báo cáo/XLSX**, không chỉ HTTP 200.
Bước "Claude đọc ảnh" được thay bằng kết quả đọc đúng đã soạn sẵn; phần gọi SDK thật được kiểm bằng HTTP giả lập
(`tests/test_claude_extractor.py`). **Chưa có test với ảnh bill thật và API thật** — xem `REAL-WORLD-VALIDATION.md`.

## Cấu trúc

```
founder_assistant/
  app.py          FastAPI: webhook Zalo, job báo cáo, tải báo cáo, reprocess
  zalo.py         Parse sự kiện, kiểm chữ ký, gửi tin, refresh token, tải media
  extraction.py   Schema + prompt + gọi Claude (structured output)
  stt.py          Speech-to-text (Whisper-compatible HTTP)
  validation.py   Đối chiếu nguồn, tính toán, kiểm ngày, fingerprint
  pipeline.py     Luồng xử lý tin nhắn, trùng lặp, xác nhận/hủy, trả lời Zalo
  pricing.py      Lịch sử giá, so sánh lần trước, TB 7/30 ngày, nhận định xu hướng
  analytics.py    Tổng hợp theo ngày/kỳ (chỉ dữ liệu đã xác nhận)
  report.py       Báo cáo text + XLSX (SUMMARY, PURCHASE, SALES, PRICE_CHANGE, ALERTS, SOURCES)
  chatbot.py      Trả lời câu hỏi bằng truy vấn dữ liệu thật
  realval.py      Công cụ kiểm chứng thực tế (preflight / run / trace) — không phải tính năng sản phẩm
  products.py     Product master + alias
  units.py money.py textnorm.py db.py config.py
```
