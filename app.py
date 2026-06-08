import streamlit as st
from extractor import extract_invoice
from database import save_invoice, list_invoices

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

tab1, tab2 = st.tabs(["Upload Invoice", "View Invoices"])


with tab1:
    uploaded = st.file_uploader(
        "Upload a receipt or invoice", type=["jpg", "jpeg", "png", "pdf"]
    )

    if uploaded:
        if uploaded.type.startswith("image"):
            st.image(uploaded, width=300)
        else:
            st.write(f"File: {uploaded.name}")

        with st.spinner("Extracting..."):
            file_bytes = uploaded.read()
            invoice = extract_invoice(file_bytes, uploaded.type)
            st.session_state["extracted"] = invoice
            st.session_state["file_bytes"] = file_bytes
            st.session_state["file_name"] = uploaded.name
            st.session_state["file_type"] = uploaded.type

    if "extracted" in st.session_state:
        for invoice in st.session_state["extracted"]:
            # Preview Invoice(s)
            st.subheader(f"{invoice.supplier_name} - {invoice.invoice_date}")
            st.metric("Total", f"${invoice.total_amount:.2f}")

            rows = [
                {
                    "Item": i.ingredient_name,
                    "Qty": i.quantity,
                    "Unit": i.unit,
                    "Unit Price": i.unit_price,
                    "Total": i.total_price,
                }
                for i in invoice.line_items
            ]
            st.table(rows)

            # Save Invoice
            save_invoice(
                invoice,
                st.session_state["file_bytes"],
                st.session_state["file_name"],
                st.session_state["file_type"],
            )
        st.success("Saved.")
        del st.session_state["extracted"]

with tab2:
    invoices = list_invoices()
    for inv in invoices:
        with st.expander(
            f"{inv['supplier_name']} - {inv['invoice_date']} (${inv['total_amount']})"
        ):
            rows = [
                {
                    "Item": item["ingredient_name"],
                    "Qty": item["quantity"],
                    "Unit": item["unit"],
                    "Unit Price": item["unit_price"],
                    "Total": item["total_price"],
                }
                for item in inv.get("invoice_line_items", [])
            ]
            st.table(rows)
