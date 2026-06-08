import os
import time
from supabase import create_client
from dotenv import load_dotenv
from extractor import Invoice

load_dotenv()

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def upload_file(file_bytes: bytes, filename: str, content_type: str) -> str:
    path = f"{int(time.time())}_{filename}"
    supabase.storage.from_("invoices").upload(
        path, file_bytes, {"content-type": content_type}
    )
    return supabase.storage.from_("invoices").get_public_url(path)


def save_invoice(
    invoice: Invoice, file_bytes: bytes, filename: str, content_type: str
) -> str:
    file_url = upload_file(file_bytes, filename, content_type)
    row = (
        supabase.table("invoices")
        .insert(
            {
                "supplier_name": invoice.supplier_name,
                "invoice_date": invoice.invoice_date,
                "total_amount": invoice.total_amount,
                "tax_amount": invoice.tax_amount,
                "subtotal": invoice.subtotal,
                "file_url": file_url,
            }
        )
        .execute()
    )
    invoice_id = row.data[0]["id"]

    items = [
        {
            "invoice_id": invoice_id,
            "ingredient_name": item.ingredient_name,
            "quantity": item.quantity,
            "unit": item.unit,
            "unit_price": item.unit_price,
            "total_price": item.total_price,
        }
        for item in invoice.line_items
    ]
    supabase.table("invoice_line_items").insert(items).execute()
    return invoice_id


def list_invoices():
    return (
        supabase.table("invoices")
        .select("*, invoice_line_items(*)")
        .order("created_at", desc=True)
        .execute()
        .data
    )
