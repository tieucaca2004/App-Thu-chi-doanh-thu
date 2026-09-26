"""AI extraction: turn text / transcript / image into a structured, *uncomputed* record.

Division of labour (anti-hallucination):
  * The model only READS: document type, what is written, and the exact snippet it read it from.
  * Every number the model returns must be written in the source; otherwise it must be null.
  * All arithmetic (totals, unit prices, % change) is done in Python, never by the model.
"""
from __future__ import annotations

import base64
import json
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, Field

PROMPT_VERSION = "v1.1"

DocType = Literal[
    "PURCHASE_BILL",    # mua hàng / hóa đơn nhà cung cấp / phiếu nhập / "đi chợ mua..."
    "EXPENSE",          # chi phí khác không phải hàng hóa: điện, nước, gas, lương, thuê mặt bằng, sửa chữa
    "SALES_BILL",       # bill bán hàng cho khách (có món)
    "REVENUE_REPORT",   # báo cáo doanh thu / chốt ca / POS / screenshot doanh thu
    "PRICE_LIST",       # bảng giá, báo giá, thông tin giá (không phải giao dịch)
    "INVENTORY",        # phiếu xuất/kiểm kho
    "QUESTION",         # Founder hỏi về số liệu
    "OTHER",
]


ExpenseCategory = Literal["ingredient", "packaging", "gas", "transport", "platform_fee", "salary", "utilities", "other"]
# Goods: have quantity/unit and are price-tracked. Other categories are money-only (no unit required).
GOODS_CATEGORIES = {"ingredient", "packaging"}


class ExtractedItem(BaseModel):
    name: str = Field(description="Tên hàng/món đúng như nguồn ghi (không dịch, không gộp).")
    quantity: Optional[float] = Field(description="Số lượng nếu nguồn ghi/nói rõ, ngược lại null.")
    unit: Optional[str] = Field(description="Đơn vị đúng như nguồn ghi (kg, ký, g, lạng, thùng, chai, con, phần...). null nếu không có.")
    unit_price: Optional[float] = Field(description="Đơn giá (VND) nếu nguồn ghi rõ đơn giá. null nếu không ghi — KHÔNG tự chia.")
    amount: Optional[float] = Field(description="Thành tiền (VND) nếu nguồn ghi rõ. null nếu không ghi — KHÔNG tự nhân.")
    amount_text: Optional[str] = Field(description="Chuỗi tiền gốc của thành tiền đúng như nguồn (vd '450 ngàn', '450.000'). null nếu không có.")
    unit_price_text: Optional[str] = Field(description="Chuỗi đơn giá gốc (vd '90k/kg'). null nếu không có.")
    category: ExpenseCategory = Field(description=(
        "Chỉ cho mua/chi: ingredient = nguyên liệu/hàng hóa chế biến; packaging = bao bì, hộp, ly, túi, ống hút; "
        "gas = gas/bình gas/than; transport = ship, xe, xăng, vận chuyển; platform_fee = phí app (Grab, ShopeeFood...); "
        "salary = lương, công nhân viên; utilities = điện, nước, internet, rác; other = còn lại (thuê, sửa chữa...). "
        "Với bán hàng dùng ingredient."))
    evidence: str = Field(description="Trích NGUYÊN VĂN đoạn trong nguồn chứa mặt hàng này (một dòng bill hoặc một cụm câu nói).")


class RevenueFields(BaseModel):
    gross_revenue: Optional[float] = Field(description="Tổng doanh thu ghi trên nguồn. null nếu không có.")
    bill_count: Optional[int] = Field(description="Số bill/hóa đơn/đơn hàng nếu ghi. null nếu không có.")
    item_count: Optional[int] = Field(description="Tổng số món/sản phẩm bán nếu ghi. null nếu không có.")
    cash: Optional[float] = Field(description="Tiền mặt nếu ghi.")
    bank_transfer: Optional[float] = Field(description="Chuyển khoản nếu ghi.")
    e_wallet: Optional[float] = Field(description="Ví điện tử (Momo, ZaloPay, ShopeePay...) nếu ghi.")
    platform_fee: Optional[float] = Field(description="Phí nền tảng/chiết khấu app (Grab, ShopeeFood...) nếu ghi.")
    evidence: Optional[str] = Field(description="Trích nguyên văn dòng tổng doanh thu.")


class QuestionIntent(BaseModel):
    intent: Literal[
        "EXPENSE_TOTAL", "REVENUE_TOTAL", "NET_TOTAL", "PRODUCT_LAST_PRICE",
        "PRODUCT_PRICE_CHANGE", "PRODUCT_SPEND", "TOP_ITEMS", "REPORT", "UNKNOWN",
    ]
    period: Literal["TODAY", "YESTERDAY", "THIS_WEEK", "LAST_7_DAYS", "THIS_MONTH", "LAST_MONTH", "LAST_30_DAYS", "DATE"]
    date: Optional[str] = Field(description="YYYY-MM-DD nếu period = DATE, ngược lại null.")
    product: Optional[str] = Field(description="Tên mặt hàng được hỏi, nếu có.")


class Extraction(BaseModel):
    doc_type: DocType
    ocr_text: Optional[str] = Field(description="Với ảnh: chép lại toàn bộ chữ đọc được trên ảnh, giữ xuống dòng. Với text: null.")
    document_date: Optional[str] = Field(description="Ngày GHI TRÊN nguồn dạng YYYY-MM-DD. 'hôm nay'/'hôm qua' -> tính từ ngày nhận tin được cung cấp. null nếu nguồn không có ngày.")
    supplier: Optional[str] = Field(description="Nhà cung cấp / nơi mua nếu ghi rõ, ngược lại null.")
    items: list[ExtractedItem]
    stated_total: Optional[float] = Field(description="Tổng tiền GHI TRÊN nguồn (dòng 'Tổng', 'Cộng', 'Thanh toán'). null nếu không có — KHÔNG tự cộng.")
    stated_total_text: Optional[str] = Field(description="Chuỗi gốc của tổng tiền.")
    revenue: Optional[RevenueFields] = Field(description="Chỉ cho SALES_BILL/REVENUE_REPORT, còn lại null.")
    question: Optional[QuestionIntent] = Field(description="Chỉ khi doc_type = QUESTION.")
    unreadable_parts: list[str] = Field(description="Những phần mờ/không chắc/không đọc được. Rỗng nếu không có.")


SYSTEM_PROMPT = """Bạn là bộ phận ĐỌC DỮ LIỆU cho trợ lý tài chính của một chủ quán ăn ở Việt Nam.
Đầu vào là tin nhắn Zalo: văn bản, bản chép giọng nói, hoặc ảnh (bill mua hàng, hóa đơn nhà cung cấp, bill bán hàng, báo cáo doanh thu/POS/chốt ca, bảng giá, phiếu nhập/xuất, giấy ghi chép).

Nhiệm vụ: phân loại và CHÉP LẠI dữ liệu thành cấu trúc. Bạn KHÔNG tính toán.

Quy tắc bắt buộc — dữ liệu này dùng để báo cáo tiền, sai một con số là hỏng cả báo cáo:
1. Chỉ điền số khi con số đó được VIẾT hoặc NÓI rõ trong nguồn. Không có -> null. Không đoán, không ước lượng, không lấy giá thị trường.
2. Không tự nhân/chia/cộng: nếu bill ghi "Thịt heo 5kg 450.000" thì amount = 450000, unit_price = null (phần mềm sẽ tự tính). Chỉ điền unit_price khi nguồn ghi đơn giá.
3. Quy đổi cách viết tiền Việt thành số VND: "450 ngàn"/"450k"/"450.000" = 450000; "1tr2" = 1200000; "88 nghìn một ký" là đơn giá 88000. Luôn chép chuỗi gốc vào amount_text / unit_price_text.
4. Đơn vị: chép đúng như nguồn ("ký", "kg", "lạng", "thùng"...). Không có đơn vị -> null. Không đổi thùng thành kg.
5. evidence phải là đoạn trích nguyên văn từ nguồn (với ảnh: từ ocr_text).
6. Mỗi dòng hàng là một item; không gộp hai mặt hàng, không tách một mặt hàng. Không đưa dòng "Tổng" vào items.
7. Tên hàng: giữ nguyên như nguồn ("thịt lợn" giữ "thịt lợn"); phần mềm tự chuẩn hóa tên.
8. Phân loại chi (category): ingredient, packaging, gas, transport, platform_fee, salary, utilities, other. Chi không phải hàng hóa (gas, lương, điện nước, ship, thuê...) không cần số lượng/đơn vị; nếu cả tin chỉ có loại chi này dùng doc_type EXPENSE.
9. Bill bán hàng/doanh thu: items là các món bán; revenue chứa các trường tổng hợp. Trường nào nguồn không có -> null.
10. Câu hỏi của Founder về số liệu ("hôm nay chi bao nhiêu", "giá tôm lần gần nhất") -> doc_type QUESTION, điền question, items rỗng. Không trả lời câu hỏi.
11. Phần mờ/không chắc -> ghi vào unreadable_parts, và để null trường đó thay vì đoán.
"""


class Extractor(Protocol):
    model_name: str

    def extract(self, *, text: str | None, image: bytes | None, image_mime: str | None,
                received_date: str, source_kind: str) -> Extraction: ...


def build_user_content(text: str | None, image: bytes | None, image_mime: str | None,
                       received_date: str, source_kind: str) -> list[dict]:
    content: list[dict] = []
    if image is not None:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_mime or "image/jpeg",
                       "data": base64.b64encode(image).decode()},
        })
    header = f"Ngày nhận tin: {received_date}\nLoại nguồn: {source_kind}\n"
    if text:
        header += f"Nội dung:\n<<<\n{text}\n>>>"
    elif image is not None:
        header += "Hãy đọc ảnh trên."
    content.append({"type": "text", "text": header})
    return content


class ClaudeExtractor:
    """Extractor backed by the Claude API with schema-validated structured output."""

    def __init__(self, model: str = "claude-opus-5", client=None):
        import anthropic

        self.model_name = model
        self.client = client or anthropic.Anthropic()

    def extract(self, *, text: str | None, image: bytes | None, image_mime: str | None,
                received_date: str, source_kind: str) -> Extraction:
        import pydantic

        try:
            response = self._call(text, image, image_mime, received_date, source_kind)
        except pydantic.ValidationError as exc:
            # refusal / truncated output leaves no valid JSON; never guess, ask the Founder to resend
            raise ExtractionError("AI không trả về dữ liệu hợp lệ.") from exc
        if response.stop_reason == "refusal":
            raise ExtractionError("Model từ chối xử lý nội dung này.")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("Kết quả đọc bị cắt ngang (quá dài).")
        parsed = response.parsed_output
        if parsed is None:
            raise ExtractionError("Không đọc được kết quả có cấu trúc từ AI.")
        return parsed

    def _call(self, text, image, image_mime, received_date, source_kind):
        return self.client.beta.messages.parse(
            model=self.model_name,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            # Reading is precision work, not open-ended reasoning.
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": build_user_content(text, image, image_mime, received_date, source_kind)}],
            output_format=Extraction,
        )


class ExtractionError(RuntimeError):
    pass


def extraction_to_json(e: Extraction) -> str:
    return json.dumps(e.model_dump(), ensure_ascii=False)
