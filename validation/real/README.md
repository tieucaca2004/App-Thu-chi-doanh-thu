# validation/real — dữ liệu thật của Founder (KHÔNG commit)

> Repository này **public**. Mọi thứ trong thư mục này (trừ file README này) bị `.gitignore`.
> Không commit ảnh bill (tên nhà cung cấp, giá), giọng nói Founder, `cases.json` hay kết quả chạy.
> Lưu trữ/backup bằng kênh riêng tư (máy Founder, Drive riêng...).

```
validation/real/
  voice/v01.m4a … v10.m4a     ghi âm thật bằng điện thoại Founder, nói tự nhiên
  bills/b01.jpg … b20.jpg     ảnh bill thật, không chỉnh sửa
  bills/pos01.jpg             ảnh chốt ca POS cùng ngày với một số bill bán lẻ
  cases.json                  ground truth — copy từ ../cases.example.json, FOUNDER xác nhận từng giá trị
  out/                        kết quả chạy (RESULTS.md, results.json, database, báo cáo XLSX)
```

Ground truth phải do **Founder** xác nhận bằng cách nhìn/nghe chứng từ gốc.
Không dùng output của OCR/AI làm ground truth. Giá trị chứng từ không ghi → `null`.

Chạy:
```bash
set -a; . ./.env; set +a
python -m founder_assistant.realval preflight
python -m founder_assistant.realval run                  # mặc định validation/real/cases.json → validation/real/out
python -m founder_assistant.realval trace --db validation/real/out/data/founder.db --day YYYY-MM-DD
```
