# Quy trình kiểm chứng thực tế (Phase 2)

Mục tiêu: chứng minh **dữ liệu thật → kết quả đúng → truy được nguồn**. Không đánh giá bằng cảm giác, không coi "API trả 200" là PASS.

## 0. Điều kiện
```bash
cp .env.example .env    # điền ANTHROPIC_API_KEY, ZALO_*, FOUNDER_ZALO_USER_ID, STT_URL(+STT_API_KEY),
                        # PUBLIC_BASE_URL, REPORT_LINK_SECRET, PRIMARY_REVENUE_SOURCE (Founder xác nhận)
set -a; . ./.env; set +a
apt-get install ffmpeg   # voice Zalo (AAC/AMR) cần chuyển sang WAV trước khi STT
python -m founder_assistant.realval preflight     # mọi dòng phải CONFIGURED + ping OK
```
Dòng nào `BLOCKED` thì phần kiểm chứng tương ứng là **BLOCKED**, không được ghi PASS.

## 1. Thu thập dữ liệu (dữ liệu thật để trong `validation/real/`, đã gitignore)
- `voice/v01.m4a … v10.m4a`: **chính giọng Founder**, nói tự nhiên (không đọc như test case).
  Cần có: nói nhanh, nói chậm, ngoài đường, có tiếng ồn, nhiều món một câu, tiền bằng chữ, thiếu giá, thiếu số lượng, câu tự sửa.
  Cách lấy file: gửi voice vào Zalo OA thật (file gốc được lưu ở `data/media/`), hoặc ghi âm điện thoại rồi copy vào.
- `bills/b01.jpg … b20.jpg`: **20 bill thật, không chỉnh sửa**, chụp đúng như Founder chụp hằng ngày:
  thịt, cá, rau, nhà cung cấp, nhiều dòng, chữ nhỏ, nghiêng, thiếu sáng, có dấu, viết tắt, có tổng, không tổng, bill lỗi.
- Ít nhất 1 ảnh chốt ca POS và vài bill bán lẻ **cùng ngày** (để kiểm đối chiếu doanh thu §16–17).
- Cùng sản phẩm, khác ngày (để kiểm so sánh giá §14).

## 2. Ghi ground truth (trước khi chạy hệ thống)
Copy `validation/cases.example.json` → `validation/real/cases.json`. Mỗi case ghi **đúng như nhìn/nghe tận mắt từ chứng từ gốc**:
`date, supplier, total, items[product, quantity, unit, unit_price, amount]`, và `status` mong đợi
(`confirmed` / `needs_confirmation` / `duplicate`).
- Chứng từ **không ghi** giá trị nào → để `null` (hệ thống phải trả UNKNOWN, không được bịa).
- Case còn chữ `TODO` hoặc thiếu file → `NOT RUN`, không bao giờ tính là PASS.
- `expected_price_changes`: `{case, product, date, old, new, diff, pct, avg_7d?, avg_30d?}`, trong đó `case` là bill có giá mới.
- `expected_reports`: `{date, revenue, expense, net, bill_count?, reconciliation_matched?}`.
- Case trùng: thêm `"tags": ["duplicate"]`; giả lập Zalo gửi lại cùng message thì dùng cùng `"msg_id"`.

## 3. Chạy
```bash
python -m founder_assistant.realval run validation/real/cases.json --out validation/real/out
```
Mỗi lần chạy dùng database mới (`validation/real/out/data/`) để kết quả lặp lại được. Kết quả nằm ở `validation/real/out/RESULTS.md` và `results.json`:
- `PASS`: mọi trường đúng.
- `PARTIAL`: chỉ sai trường không phải tiền (tên, đơn vị, ngày, nhà cung cấp).
- `FAIL`: sai bất kỳ trường tiền nào, thiếu món, bịa thêm món, hoặc sai trạng thái. *OCR đúng 95% nhưng tổng sai → FAIL.*
- Mỗi trường sai có gợi ý tầng lỗi (STT / OCR-Vision / Claude extraction / Normalization-Validation). Transcript voice được in ra để tách lỗi STT khỏi lỗi đọc.

## 4. Kiểm tra bằng mắt
Mở `validation/real/out/data/reports/Founder-Daily-Report-*.xlsx`: số liệu, định dạng, 6 sheet, tổng, PRICE_CHANGE, ALERTS, SOURCES.

## 5. Truy vết (≥ 10 con số)
```bash
python -m founder_assistant.realval trace --db validation/real/out/data/founder.db --day 2026-10-01
python -m founder_assistant.realval trace --db data/founder.db --day <ngày chạy Zalo thật>
```
Mỗi con số: báo cáo → M/B record → extraction → message → file gốc, kèm kiểm tra SHA-256 (file bị sửa → FAIL).

## 6. Zalo E2E thật (§25)
Với webhook thật: gửi text, ảnh, voice; gửi lại cùng ảnh; gửi nội dung vô nghĩa; tắt `ANTHROPIC_API_KEY` tạm thời (AI lỗi → `failed` → tự thử lại);
đặt `STT_URL` sai (STT lỗi → `needs_transcription`, bot nhờ gõ lại); `POST /jobs/daily-report` rồi mở link tải có chữ ký, và thử link sai chữ ký (phải 403).

## 7. Ghi bug
Mỗi case FAIL/PARTIAL → một mục trong `REAL-WORLD-VALIDATION.md` §14:
`BUG ID, Input, Expected, Actual, Layer, Root cause, Fix, Regression test, Status`.
Sửa đúng tầng gây lỗi → thêm regression test → chạy toàn bộ `pytest` → FREEZE.
