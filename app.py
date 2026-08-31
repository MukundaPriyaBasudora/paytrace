"""
PayTrace -- Streamlit dashboard.

Reads from data/latest_results.json (written by src/metrics.py) rather than
re-running the reconciliation pipeline live -- Streamlit re-executes this
script on every interaction, and re-running the pipeline each time would
re-trigger all 10 LLM calls per click, burning API quota for no reason.

Run with: streamlit run app.py   (from the project root)
"""

import json

import pandas as pd
import streamlit as st

st.set_page_config(page_title="PayTrace -- Reconciliation Agent", layout="wide")
st.title("PayTrace")
st.caption("AI Finance Controller -- multi-source reconciliation agent")

try:
    with open("data/latest_results.json") as f:
        results = json.load(f)
except FileNotFoundError:
    st.error("No results yet. Run `python metrics.py` from the src/ folder first.")
    st.stop()

classified = pd.DataFrame(results["classified"])
orphans = pd.DataFrame(results["orphaned_ledger_records"])
metrics = results["metrics"]

tab_overview, tab_matched, tab_no_match, tab_exceptions = st.tabs(
    ["Overview", "Matched", "Confirmed no match", "Exceptions & orphans"]
)

with tab_overview:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Match rate", f"{metrics['match_rate']:.1%}")
    col2.metric("Matched", metrics["matched"])
    col3.metric("Confirmed no match", metrics["confirmed_no_match"])
    col4.metric("Needs human", metrics["needs_human"])

    st.subheader("Resolution method breakdown")
    method_counts = classified["method"].value_counts()
    st.bar_chart(method_counts)

    st.caption(
        f"{metrics['total_settlements']} total settlements processed. "
        f"{len(orphans)} ledger records had no settlement counterpart at all (see Exceptions tab)."
    )

with tab_matched:
    st.caption("Every settlement confirmed to a real ledger counterpart, with the method and reasoning behind it.")
    matched = classified[classified.bucket == "matched"][
        ["settlement_payment_id", "matched_order_ref", "method", "confidence", "reasoning"]
    ]
    st.dataframe(matched, width='stretch', hide_index=True)

with tab_no_match:
    st.caption(
        "Settlements the system correctly identified as having NO real counterpart -- "
        "this is the precision test: a close-looking candidate was deliberately rejected, not just recall on easy matches."
    )
    no_match = classified[classified.bucket == "confirmed_no_match"][
        ["settlement_payment_id", "method", "confidence", "reasoning"]
    ]
    st.dataframe(no_match, width='stretch', hide_index=True)

with tab_exceptions:
    needs_human = classified[classified.bucket == "needs_human"][
        ["settlement_payment_id", "method", "reasoning"]
    ]
    if len(needs_human):
        st.warning(f"{len(needs_human)} settlement(s) need a human look.")
        st.dataframe(needs_human, width='stretch', hide_index=True)
    else:
        st.success("No settlements currently need human review.")

    st.subheader("Orphaned ledger records")
    st.caption("Ledger entries with no settlement claiming them at all -- possible unrecorded or failed payments.")
    st.dataframe(orphans, width='stretch', hide_index=True)