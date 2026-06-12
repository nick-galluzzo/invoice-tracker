import csv
import io
import os
import time
from datetime import datetime, timezone
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


def list_invoices(exported: bool | None = None):
    query = supabase.table("invoices").select("*, invoice_line_items(*)")
    if exported is not None:
        query = query.eq("exported", exported)
    return query.order("created_at", desc=True).execute().data


def mark_invoices_exported(invoice_ids: list[str]) -> None:
    supabase.table("invoices").update(
        {
            "exported": True,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
    ).in_("id", invoice_ids).execute()


def build_export_csv(invoices: list[dict]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Invoice ID",
            "Supplier",
            "Invoice Date",
            "Subtotal",
            "Tax",
            "Total",
            "Item",
            "Qty",
            "Unit",
            "Unit Price",
            "Line Total",
        ]
    )
    for inv in invoices:
        for item in inv.get("invoice_line_items", []):
            writer.writerow(
                [
                    inv["id"],
                    inv["supplier_name"],
                    inv["invoice_date"],
                    inv["subtotal"],
                    inv["tax_amount"],
                    inv["total_amount"],
                    item["ingredient_name"],
                    item["quantity"],
                    item["unit"],
                    item["unit_price"],
                    item["total_price"],
                ]
            )
    return output.getvalue().encode("utf-8")
