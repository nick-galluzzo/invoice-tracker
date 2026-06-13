import csv
import io
import os
import time
from datetime import datetime, timezone
from supabase import create_client
from dotenv import load_dotenv
from extractor import RefinedInvoice

load_dotenv()

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def upload_file(file_bytes: bytes, filename: str, content_type: str) -> str:
    path = f"{int(time.time())}_{filename}"
    supabase.storage.from_("invoices").upload(
        path, file_bytes, {"content-type": content_type}
    )
    return supabase.storage.from_("invoices").get_public_url(path)


def _normalize_date(raw: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def save_invoice(
    invoice: RefinedInvoice, file_bytes: bytes, filename: str, content_type: str
) -> str:
    file_url = upload_file(file_bytes, filename, content_type)
    row = (
        supabase.table("invoices")
        .insert(
            {
                "invoice_id": invoice.invoice_id,
                "supplier_name": invoice.supplier_name,
                "invoice_date": _normalize_date(invoice.invoice_date),
                "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
                "total_amount": invoice.total_amount,
                "tax_amount": invoice.tax_amount,
                "subtotal": invoice.subtotal,
                "math_validated": invoice.math_validated,
                "discounts": [d.model_dump() for d in invoice.global_discounts],
                "file_url": file_url,
            }
        )
        .execute()
    )

    invoice_internal_id = row.data[0]["id"]
    items = [
        {
            "invoice_id": invoice_internal_id,
            "ingredient_name": item.ingredient_name,
            "quantity": item.quantity,
            "unit": item.unit,
            "unit_price": item.unit_price,
            "total_price": item.total_price,
            "category": item.category,
            "subcategory": item.subcategory,
            "is_vat_eligible": item.is_vat_eligible,
            "calculated_vat": item.calculated_vat,
        }
        for item in invoice.line_items
    ]
    supabase.table("invoice_line_items").insert(items).execute()

    return invoice_internal_id


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
            "Invoice Number",
            "Supplier",
            "Invoice Date",
            "Due Date",
            "Item",
            "Qty",
            "Unit",
            "Unit Price",
            "Line Total",
            "Category",
            "Subcategory",
            "Is VAT",
            "Calc VAT",
            "Discounts",
        ]
    )
    for inv in invoices:
        for item in inv.get("invoice_line_items", []):
            writer.writerow(
                [
                    inv["invoice_id"] or "",
                    inv["supplier_name"],
                    inv["invoice_date"] or "",
                    inv["due_date"] or "",
                    item["ingredient_name"],
                    item["quantity"],
                    item["unit"],
                    item["unit_price"],
                    item["total_price"],
                    item["category"] or "",
                    item["subcategory"] or "",
                    item["is_vat_eligible"] or "",
                    item["calculated_vat"] or "",
                    "; ".join(
                        f"{d['discount_type']} -{d['amount']}"
                        for d in (inv.get("discounts") or [])
                    ),
                ]
            )
    return output.getvalue().encode("utf-8")
