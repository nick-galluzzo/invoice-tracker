import base64
import os
import threading
from datetime import date
import streamlit as st

os.environ["GEMINI_API_KEY"] = st.secrets["GEMINI_API_KEY"]
os.environ["SUPABASE_URL"] = st.secrets["SUPABASE_URL"]
os.environ["SUPABASE_KEY"] = st.secrets["SUPABASE_KEY"]

from database import (
    build_export_csv,
    list_invoices,
    mark_invoices_exported,
    save_invoice,
)
from extractor import RefinedLineItem, VAT_RATE, extract_invoice


@st.cache_resource
def _get_bg_store() -> dict:
    """Single worker thread + queue — avoids asyncio event loop conflicts from pydantic_ai."""
    import queue as _q

    store: dict = {"results": {}, "lock": threading.Lock(), "work_queue": _q.Queue()}

    def _worker() -> None:
        while True:
            job_id, file_bytes, media_type = store["work_queue"].get()
            try:
                invoices = extract_invoice(file_bytes, media_type)
                with store["lock"]:
                    store["results"][job_id] = {"ok": True, "invoices": invoices}
            except Exception as e:
                with store["lock"]:
                    store["results"][job_id] = {"ok": False, "error": str(e)}
            finally:
                store["work_queue"].task_done()

    threading.Thread(target=_worker, daemon=True).start()
    return store


def _extract_background(job_id: str, file_bytes: bytes, media_type: str) -> None:
    _get_bg_store()["work_queue"].put((job_id, file_bytes, media_type))


@st.fragment(run_every=0.5)
def _poll_bg_results() -> None:
    """Polls for completed background jobs without causing a full-page rerender."""
    queue = st.session_state.get("upload_queue", [])
    if not any(i["status"] == "processing" for i in queue):
        return

    _store = _get_bg_store()
    with _store["lock"]:
        completed = dict(_store["results"])

    changed = False
    for i, item in enumerate(queue):
        job_id = item.get("job_id")
        if item["status"] == "processing" and job_id in completed:
            result = completed[job_id]
            if result["ok"] and result["invoices"]:
                st.session_state["upload_queue"][i]["extracted"] = result["invoices"]
                st.session_state["upload_queue"][i]["inv_statuses"] = [
                    "pending_review"
                ] * len(result["invoices"])
                st.session_state["upload_queue"][i]["status"] = "needs_review"
            elif result["ok"]:
                st.session_state["upload_queue"][i]["status"] = "done"
            else:
                st.session_state["upload_queue"][i]["status"] = "failed"
                st.session_state["upload_queue"][i]["error"] = result["error"]
            with _store["lock"]:
                _store["results"].pop(job_id, None)
            changed = True

    if changed:
        st.rerun()  # full rerun only when a job actually completed


def _line_items_table(line_items: list) -> list[dict]:
    rows = []
    for li in line_items:
        is_dict = isinstance(li, dict)
        rows.append(
            {
                "Item": li["ingredient_name"] if is_dict else li.ingredient_name,
                "Qty": li["quantity"] if is_dict else li.quantity,
                "Unit": li["unit"] if is_dict else li.unit,
                "Unit Price": f"฿{(li['unit_price'] if is_dict else li.unit_price):,.2f}",
                "Total": f"฿{(li['total_price'] if is_dict else li.total_price):,.2f}",
                "Category": li["category"] if is_dict else li.category,
                "Sub": (li.get("subcategory") or "—")
                if is_dict
                else (li.subcategory or "—"),
                "VAT": "✓"
                if (li["is_vat_eligible"] if is_dict else li.is_vat_eligible)
                else "✗",
                "Calc. VAT": f"฿{(li['calculated_vat'] if is_dict else li.calculated_vat):,.2f}",
            }
        )
    return rows


# Auth
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        if password == st.secrets["APP_PASSWORD"]:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

st.set_page_config(page_title="Invoice Tracker", layout="wide")
st.title("Invoice Tracker")
tab1, tab2, tab3 = st.tabs(["Upload", "Ready to Export", "Exported"])

# Tab 1: Bulk Upload
with tab1:
    uploaded_files = st.file_uploader(
        "Upload product invoices",
        type=["jpg", "jpeg", "png", "pdf"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.get('uploader_key', 0)}",
    )

    # Capture newly dropped files into the queue (deduped by file name)
    if uploaded_files:
        existing = {item["name"] for item in st.session_state.get("upload_queue", [])}
        for f in uploaded_files:
            if f.name not in existing:
                st.session_state.setdefault("upload_queue", []).append(
                    {
                        "name": f.name,
                        "bytes": f.read(),
                        "type": f.type,
                        "status": "pending",
                        "error": None,
                        "extracted": None,
                        "inv_statuses": None,
                    }
                )

    queue: list[dict] = st.session_state.get("upload_queue", [])
    if queue:
        total = len(queue)
        done = sum(1 for i in queue if i["status"] in ("done", "failed"))
        in_progress = sum(1 for i in queue if i["status"] in ("pending", "processing"))
        needs_review = sum(1 for i in queue if i["status"] == "needs_review")
        failed = sum(1 for i in queue if i["status"] == "failed")

        # Progress bar (only meaningful for multiple files)
        if total > 1:
            st.progress(done / total)

        # Status summary
        if in_progress > 0:
            st.caption(
                f"⏳ Extracting {in_progress} file{'s' if in_progress != 1 else ''}… ({done} of {total} done)"
            )

        # All done — clear queue
        if in_progress == 0 and needs_review == 0:
            if failed == 0:
                st.success(f"All {total} invoice{'s' if total != 1 else ''} saved.")
            else:
                saved = sum(1 for i in queue if i["status"] == "done")
                st.warning(f"{saved} saved · {failed} failed")
            st.session_state["upload_queue"] = []
            st.session_state["uploader_key"] = (
                st.session_state.get("uploader_key", 0) + 1
            )
            st.rerun()

        # Failed files with retry
        for i, item in enumerate(queue):
            if item["status"] == "failed":
                cols = st.columns([5, 1])
                cols[0].error(
                    f"**{item['name']}** — {item['error'] or 'Extraction failed'}"
                )
                if cols[1].button("Retry", key=f"retry_{i}"):
                    st.session_state["upload_queue"][i]["status"] = "pending"
                    st.session_state["upload_queue"][i]["error"] = None
                    st.rerun()

        # ── Review section ──────────────────────────────────────────────────────
        review_items = [
            (qi, item)
            for qi, item in enumerate(queue)
            if item["status"] == "needs_review"
        ]

        if review_items:
            total_pending = sum(
                sum(1 for s in item["inv_statuses"] if s == "pending_review")
                for _, item in review_items
            )
            if total_pending > 0:
                st.divider()
                st.subheader(
                    f"Review {total_pending} extracted invoice{'s' if total_pending != 1 else ''}"
                )
                st.caption(
                    "Verify the data below. Edit supplier or invoice number if needed, then confirm to save."
                )

            for queue_idx, item in review_items:
                for inv_idx, inv in enumerate(item["extracted"]):
                    if item["inv_statuses"][inv_idx] != "pending_review":
                        continue

                    if len(item["extracted"]) > 1:
                        st.caption(
                            f"📄 {item['name']} · Invoice {inv_idx + 1} of {len(item['extracted'])}"
                        )

                    with st.container(border=True):
                        doc_col, form_col = st.columns([2, 3])

                        # ── Document viewer ────────────────────────────────────
                        with doc_col:
                            if item["type"].startswith("image/"):
                                st.image(item["bytes"], use_container_width=True)
                            else:
                                b64 = base64.b64encode(item["bytes"]).decode()
                                st.markdown(
                                    f'<iframe src="data:application/pdf;base64,{b64}" '
                                    f'width="100%" height="780px" style="border:none;border-radius:6px;"></iframe>',
                                    unsafe_allow_html=True,
                                )

                        # ── Review form ────────────────────────────────────────
                        with form_col:
                            with st.form(key=f"review_{queue_idx}_{inv_idx}"):
                                # Editable header
                                c1, c2, c3 = st.columns([3, 3, 2])
                                new_supplier = c1.text_input(
                                    "Supplier", value=inv.supplier_name
                                )
                                new_invoice_id = c2.text_input(
                                    "Invoice #", value=inv.invoice_id or ""
                                )
                                c3.text_input(
                                    "Date", value=inv.invoice_date, disabled=True
                                )

                                # Math validation badge
                                if inv.math_validated:
                                    st.caption("✅ Math validated")
                                else:
                                    st.warning("⚠️ Verify totals before saving")

                                # Totals
                                m1, m2, m3 = st.columns(3)
                                m1.metric("Subtotal", f"฿{inv.subtotal:,.2f}")
                                m2.metric("VAT (7%)", f"฿{inv.tax_amount:,.2f}")
                                m3.metric("Total", f"฿{inv.total_amount:,.2f}")

                                # Discounts
                                if inv.global_discounts:
                                    parts = [
                                        f"{d.discount_type.value} −฿{d.amount:,.2f}"
                                        for d in inv.global_discounts
                                    ]
                                    st.caption(f"Discounts: {' · '.join(parts)}")

                                # Editable line items
                                st.caption(
                                    "Edit any field directly in the table below."
                                )
                                edited_df = st.data_editor(
                                    [
                                        {
                                            "Item": li.ingredient_name,
                                            "Qty": li.quantity,
                                            "Unit": li.unit,
                                            "Unit Price": li.unit_price,
                                            "Total": li.total_price,
                                            "Category": li.category,
                                            "Sub": li.subcategory or "",
                                            "VAT?": li.is_vat_eligible,
                                        }
                                        for li in inv.line_items
                                    ],
                                    column_config={
                                        "Qty": st.column_config.NumberColumn("Qty"),
                                        "Unit Price": st.column_config.NumberColumn(
                                            "Unit Price", format="฿%.2f"
                                        ),
                                        "Total": st.column_config.NumberColumn(
                                            "Total", format="฿%.2f"
                                        ),
                                        "Category": st.column_config.SelectboxColumn(
                                            "Category",
                                            options=[
                                                "Cleaning Supplies",
                                                "Kitchen Supplies",
                                                "COGS Food",
                                                "Equipment: Kitchen",
                                                "Equipment: Operation",
                                                "Supplies: Operation",
                                            ],
                                        ),
                                        "Sub": st.column_config.SelectboxColumn(
                                            "Sub",
                                            options=[
                                                "",
                                                "Food Ingredients",
                                                "Food Packaging",
                                            ],
                                        ),
                                        "VAT?": st.column_config.CheckboxColumn("VAT?"),
                                    },
                                    use_container_width=True,
                                    hide_index=True,
                                    num_rows="fixed",
                                )

                                # Action buttons
                                _, b1, b2 = st.columns([4, 1, 1])
                                discard = b1.form_submit_button(
                                    "Discard", use_container_width=True
                                )
                                confirm = b2.form_submit_button(
                                    "Confirm & Save",
                                    type="primary",
                                    use_container_width=True,
                                )

                            if confirm:
                                modified_items = [
                                    RefinedLineItem(
                                        ingredient_name=str(row["Item"]),
                                        quantity=float(row["Qty"]),
                                        unit=str(row["Unit"]),
                                        unit_price=float(row["Unit Price"]),
                                        total_price=float(row["Total"]),
                                        category=row["Category"],
                                        subcategory=row["Sub"] or None,
                                        is_vat_eligible=bool(row["VAT?"]),
                                        calculated_vat=round(
                                            float(row["Total"]) * VAT_RATE, 2
                                        )
                                        if row["VAT?"]
                                        else 0.0,
                                    )
                                    for row in edited_df
                                ]
                                edited_inv = inv.model_copy(
                                    update={
                                        "supplier_name": new_supplier,
                                        "invoice_id": new_invoice_id or None,
                                        "line_items": modified_items,
                                    }
                                )
                                save_invoice(
                                    edited_inv,
                                    item["bytes"],
                                    item["name"],
                                    item["type"],
                                )
                                st.session_state["upload_queue"][queue_idx][
                                    "inv_statuses"
                                ][inv_idx] = "confirmed"
                                if all(
                                    s != "pending_review"
                                    for s in st.session_state["upload_queue"][
                                        queue_idx
                                    ]["inv_statuses"]
                                ):
                                    st.session_state["upload_queue"][queue_idx][
                                        "status"
                                    ] = "done"
                                st.rerun()

                            if discard:
                                st.session_state["upload_queue"][queue_idx][
                                    "inv_statuses"
                                ][inv_idx] = "discarded"
                                if all(
                                    s != "pending_review"
                                    for s in st.session_state["upload_queue"][
                                        queue_idx
                                    ]["inv_statuses"]
                                ):
                                    st.session_state["upload_queue"][queue_idx][
                                        "status"
                                    ] = "done"
                                st.rerun()

    # Enqueue pending files for the background worker
    for i, item in enumerate(queue):
        if item["status"] == "pending":
            job_id = f"job_{i}_{item['name']}"
            st.session_state["upload_queue"][i]["status"] = "processing"
            st.session_state["upload_queue"][i]["job_id"] = job_id
            _extract_background(job_id, item["bytes"], item["type"])

    # Poll for results without dimming the page
    if any(i["status"] == "processing" for i in queue):
        _poll_bg_results()

# Tab 2: Ready to export
with tab2:
    if "export_success_count" in st.session_state:
        count = st.session_state.pop("export_success_count")
        st.success(f"Exported {count} invoice{'s' if count != 1 else ''} successfully.")

    invoices = list_invoices(exported=False)

    if not invoices:
        st.info("No invoices ready to export.")
    else:
        n = len(invoices)
        csv_bytes = build_export_csv(invoices)

        if st.download_button(
            label=f"Export All ({n}) as CSV",
            data=csv_bytes,
            file_name=f"invoices_{date.today().isoformat()}.csv",
            mime="text/csv",
            type="primary",
        ):
            mark_invoices_exported([inv["id"] for inv in invoices])
            st.session_state["export_success_count"] = n
            st.rerun()

        st.divider()

        for inv in invoices:
            header = f"{inv['supplier_name']}  ·  {inv['invoice_date']}"
            if inv.get("invoice_id"):
                header = f"{inv['invoice_id']}  ·  " + header
            with st.expander(header):
                m1, m2, m3 = st.columns(3)
                m1.metric("Subtotal", f"฿{inv['subtotal']:,.2f}")
                m2.metric("VAT (7%)", f"฿{inv['tax_amount']:,.2f}")
                m3.metric("Total", f"฿{inv['total_amount']:,.2f}")
                st.dataframe(
                    _line_items_table(inv.get("invoice_line_items", [])),
                    use_container_width=True,
                    hide_index=True,
                )

# Tab 3: Exported
with tab3:
    invoices = list_invoices(exported=True)

    if not invoices:
        st.info("No exported invoices yet.")
    else:
        n = len(invoices)
        st.write(f"**{n} exported invoice{'s' if n != 1 else ''}**")
        csv_bytes = build_export_csv(invoices)
        st.download_button(
            label=f"Re-export All ({n}) as CSV",
            data=csv_bytes,
            file_name=f"invoices_reexport_{date.today().isoformat()}.csv",
            mime="text/csv",
        )
        st.divider()

        for inv in invoices:
            exported_on = (inv.get("exported_at") or "")[:10]
            header = f"{inv['supplier_name']}  ·  {inv['invoice_date']}"
            if inv.get("invoice_id"):
                header = f"{inv['invoice_id']}  ·  " + header
            if exported_on:
                header += f"  ·  exported {exported_on}"
            with st.expander(header):
                m1, m2, m3 = st.columns(3)
                m1.metric("Subtotal", f"฿{inv['subtotal']:,.2f}")
                m2.metric("VAT (7%)", f"฿{inv['tax_amount']:,.2f}")
                m3.metric("Total", f"฿{inv['total_amount']:,.2f}")
                st.dataframe(
                    _line_items_table(inv.get("invoice_line_items", [])),
                    use_container_width=True,
                    hide_index=True,
                )
