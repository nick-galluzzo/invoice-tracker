import os
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
from extractor import extract_invoice

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
                    }
                )

    queue: list[dict] = st.session_state.get("upload_queue", [])
    if queue:
        total = len(queue)
        succeeded = sum(1 for i in queue if i["status"] == "success")
        failed = sum(1 for i in queue if i["status"] == "failed")
        in_progress = sum(1 for i in queue if i["status"] in ("pending", "processing"))

        # Progress bar (only meaningful for multiple files)
        if total > 1:
            st.progress((succeeded + failed) / total)

        # Status summary
        if in_progress > 0:
            st.caption(f"{succeeded + failed} of {total} complete")
        elif failed == 0:
            st.success(f"All {total} file{'s' if total != 1 else ''} saved.")
            st.session_state["upload_queue"] = []
            st.session_state["uploader_key"] = (
                st.session_state.get("uploader_key", 0) + 1
            )
            st.rerun()
        else:
            st.warning(f"{succeeded} saved · {failed} failed")

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

    # Auto-process: pick the next pending file and run it
    next_idx = next(
        (i for i, item in enumerate(queue) if item["status"] == "pending"), None
    )
    if next_idx is not None:
        st.session_state["upload_queue"][next_idx]["status"] = "processing"
        item = st.session_state["upload_queue"][next_idx]
        with st.spinner(f"Analyzing Invoice Data for **{item['name']}**…"):
            try:
                invoices = extract_invoice(item["bytes"], item["type"])
                for inv in invoices:
                    if inv.line_items:
                        save_invoice(inv, item["bytes"], item["name"], item["type"])
                st.session_state["upload_queue"][next_idx]["status"] = "success"
            except Exception as e:
                st.session_state["upload_queue"][next_idx]["status"] = "failed"
                st.session_state["upload_queue"][next_idx]["error"] = str(e)
        st.rerun()  # Loop to next pending file

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
            with st.expander(f"{inv['supplier_name']} — {inv['invoice_date']}"):
                st.table(
                    [
                        {
                            "Item": item["ingredient_name"],
                        }
                        for item in inv.get("invoice_line_items", [])
                    ]
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
            label = f"{inv['supplier_name']} — {inv['invoice_date']}"
            if exported_on:
                label += f"  ·  EXPORTED ON {exported_on}"
            with st.expander(label):
                st.table(
                    [
                        {
                            "Item": item["ingredient_name"],
                            "Qty": item["quantity"],
                        }
                        for item in inv.get("invoice_line_items", [])
                    ]
                )
