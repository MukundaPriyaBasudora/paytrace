import streamlit as st

st.set_page_config(
    page_title="PayTrace",
    page_icon="💳",
    layout="wide"
)

st.title("PayTrace")
st.subheader("AI-Powered Payment Reconciliation Agent")

st.write(
    "Reconcile payment settlements with internal ledger records "
    "and identify unresolved exceptions."
)