from pydantic import BaseModel
from pydantic_ai import Agent, BinaryContent


class LineItem(BaseModel):
    ingredient_name: str
    quantity: float
    unit: str
    unit_price: float
    total_price: float


class Invoice(BaseModel):
    supplier_name: str
    invoice_date: str  # YYYY-MM-DD
    line_items: list[LineItem]
    subtotal: float
    tax_amount: float
    total_amount: float


agent = Agent(
    "google-gla:gemini-2.5-flash",
    output_type=list[Invoice],
    system_prompt=(
        "You extract structured data from restaurant purchase invoices. "
        "Return supplier name, invoice date (YYYY-MM-DD), tax amount, subtotal, total amount, all line items with ingredient name, "
        "quantity, unit (e.g. kg, pcs, liters), unit price, and total price. "
        "If a field is missing or unclear, make a best guess from context."
        "If the original is in another language, translate everything to English."
    ),
)


def extract_invoice(file_bytes: bytes, media_type: str) -> list[Invoice]:
    result = agent.run_sync(
        [
            BinaryContent(data=file_bytes, media_type=media_type),
            "Extract ALL invoices in this document. "
            "If multiple pages belong to the same supplier and invoice, combine them into ONE invoice with all line items merged. "
            "Only create separate invoices when the supplier or transaction is genuinely different.",
        ]
    )
    return result.output
