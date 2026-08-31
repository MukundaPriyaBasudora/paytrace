"""
Agentic reasoning layer -- Google Gemini version (free tier, no card required).

Takes whatever deterministic_matcher.py couldn't confidently resolve
(unresolved / duplicate_conflict / possible_batch) and makes a real
judgment call: match / no_match / needs_human, with a confidence score
and a short explanation -- not just a narration of a decision already made
elsewhere. This is what makes the system an agent rather than a fuzzy-
matching script with a chatbot bolted on.

VALIDATED: run against the real 122-settlement dataset and checked row-by-row
against ground_truth.json -- 10/10 correct on the current model
(gemini-3.5-flash-lite), including both decoy_near_miss cases correctly
returning no_match and all duplicate_conflict cases correctly using the
ledger notes field to disambiguate. See BUILD_LOG.md for the bugs found
and fixed along the way (candidate leakage across settlements, JSON response
shape differences between models, thinking-mode text leaking into output).
"""

import json

import pandas as pd
from google import genai
from google.genai import types

client = genai.Client()  # reads GEMINI_API_KEY from the environment
MODEL = "gemini-3.5-flash-lite"  # Switched from gemini-3.6-flash: confirmed
                              # via live AI Studio dashboard that Flash-Lite
                              # gets a 500 RPD free quota vs. 20 RPD for the
                              # full Flash tier -- 25x more headroom. Lighter
                              # model, so re-validate against ground truth
                              # after switching, don't assume equal quality.

PROMPT_TEMPLATE = """You are reconciling a payment settlement against a merchant's order ledger.

Settlement record:
  payment_id: {payment_id}
  gross_amount: {gross_amount}
  net_amount: {net_amount}
  fee: {fee}
  payment_date: {payment_date}
  settlement_date: {settlement_date}
  status: {status}

Candidate ledger record(s):
{candidates_block}

Decide whether ONE of these candidates is the SAME transaction as the settlement above.
"no_match" is a valid and expected answer if none genuinely correspond -- do not guess
just because a candidate is the closest available option. If there are multiple
candidates because of a possible duplicate entry, use the amount, date, and any notes
field to judge which one (if any) is the real match -- a note mentioning a duplicate
or double-click is a strong signal about which entry is NOT the real one.

Do NOT show arithmetic, comparisons, or your reasoning process in the explanation --
give only the final one-sentence justification, no scratchpad, no "let me check".
Respond with ONLY the JSON object below -- no text before or after it.

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"decision": "match" | "no_match" | "needs_human", "matched_order_ref": "<order_ref or null>", "confidence": <0.0-1.0>, "reason_code": "<short_snake_case_code>", "explanation": "<one sentence>"}}
"""

BATCH_PROMPT_TEMPLATE = """You are confirming a PROPOSED BATCH SETTLEMENT match.

A settlement may represent SEVERAL separate ledger orders paid out together in
one batch (a common real pattern when multiple orders settle at once) rather
than a single 1:1 match.

Settlement record:
  payment_id: {payment_id}
  gross_amount: {gross_amount}
  payment_date: {payment_date}
  status: {status}

Proposed batch of ledger orders (their amounts already sum to within a few
paise of the settlement gross_amount -- this arithmetic has been verified
separately, you do not need to re-check it):
{candidates_block}
  Sum of proposed candidates: {candidate_sum}

Judge whether this GROUPING is plausible as a single batched payout -- using
order dates, and any notes field, as evidence (a note like "part of batched
settlement" is a strong positive signal). "match" means ALL listed orders
together are confirmed as this settlement's batch. "no_match" means the
grouping looks coincidental rather than a real batch. Use "needs_human" if
genuinely unsure.

Do NOT show arithmetic or your reasoning process in the explanation -- give
only the final one-sentence justification. Respond with ONLY the JSON object
below -- no text before or after it.

Respond with ONLY valid JSON, no other text, in exactly this shape:
{{"decision": "match" | "no_match" | "needs_human", "matched_order_ref": "<comma-separated order_refs or null>", "confidence": <0.0-1.0>, "reason_code": "<short_snake_case_code>", "explanation": "<one sentence>"}}
"""


def load_data():
    settlements = pd.read_csv("../data/settlements.csv", parse_dates=["payment_date", "settlement_date"])
    ledger = pd.read_csv("../data/ledger.csv", parse_dates=["order_date"])
    return settlements, ledger


def _format_candidates(candidates: pd.DataFrame) -> str:
    if candidates.empty:
        return "  (no candidates found)"
    lines = []
    for c in candidates.itertuples():
        note = c.notes if isinstance(c.notes, str) and c.notes else "none"
        lines.append(f"  - order_ref: {c.order_ref}, order_amount: {c.order_amount}, order_date: {c.order_date.date()}, notes: {note}")
    return "\n".join(lines)


def _parse_decision(raw: str) -> dict:
    """Parse the model's JSON response defensively. Different models format
    "respond with JSON" instructions differently -- we hit this for real:
    gemini-3.6-flash returned a bare object as asked, but gemini-3.5-flash-lite
    sometimes wraps it in a list (e.g. [{"decision": ...}]). Handle both
    shapes here, in one place, rather than duplicating the fix per call site."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return dict(decision="needs_human", matched_order_ref=None, confidence=None,
                    reason_code="llm_response_unparseable", explanation=f"Raw response: {raw[:200]}")

    if isinstance(parsed, list):
        if len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        return dict(decision="needs_human", matched_order_ref=None, confidence=None,
                    reason_code="llm_response_unexpected_list_shape",
                    explanation=f"Expected one JSON object, got a list of {len(parsed)}: {raw[:200]}")
    if isinstance(parsed, dict):
        return parsed
    return dict(decision="needs_human", matched_order_ref=None, confidence=None,
                reason_code="llm_response_unexpected_type",
                explanation=f"Expected a JSON object, got {type(parsed).__name__}: {raw[:200]}")


def _ask_llm(settlement_row, candidates: pd.DataFrame) -> dict:
    if candidates.empty:
        # Nothing to reason about -- don't spend an API call on an unanswerable question.
        return dict(decision="needs_human", matched_order_ref=None, confidence=None,
                    reason_code="no_candidates", explanation="No ledger candidates found within date/amount window.")

    prompt = PROMPT_TEMPLATE.format(
        payment_id=settlement_row.payment_id, gross_amount=settlement_row.gross_amount,
        net_amount=settlement_row.net_amount, fee=settlement_row.fee,
        payment_date=settlement_row.payment_date.date(), settlement_date=settlement_row.settlement_date.date(),
        status=settlement_row.status, candidates_block=_format_candidates(candidates),
    )

    response = client.models.generate_content(
        model=MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",   # asks Gemini to guarantee valid JSON
            temperature=0.2,                            # more reliable than prompt instructions alone
            max_output_tokens=200,                       # hard cap -- backstop against rambling
            # NOTE: thinking_config was removed -- it caused a 400 INVALID_ARGUMENT
            # on gemini-3.5-flash-lite in real testing. The explicit "don't show your
            # reasoning" prompt instruction was sufficient on its own: validated 10/10
            # correct against ground truth without it.
        ),
    )
    return _parse_decision(response.text.strip())


def _ask_llm_batch(settlement_row, candidates: pd.DataFrame) -> dict:
    """Separate path for possible_batch cases -- asks whether the GROUP sums
    to a plausible match, not whether any single candidate matches alone.
    This is the fix for the bug we found in real testing: the generic
    per-candidate prompt answers "no single one matches," which is true but
    misses the actual question for a batch."""
    candidate_sum = candidates.order_amount.sum()
    prompt = BATCH_PROMPT_TEMPLATE.format(
        payment_id=settlement_row.payment_id, gross_amount=settlement_row.gross_amount,
        payment_date=settlement_row.payment_date.date(), status=settlement_row.status,
        candidates_block=_format_candidates(candidates), candidate_sum=round(candidate_sum, 2),
    )
    response = client.models.generate_content(
        model=MODEL, contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.2,
            max_output_tokens=200,
        ),
    )
    return _parse_decision(response.text.strip())


def run_llm_layer(deterministic_results: pd.DataFrame) -> pd.DataFrame:
    settlements, ledger = load_data()
    to_review = deterministic_results[
        deterministic_results.resolution_method.isin(["unresolved", "duplicate_conflict", "possible_batch"])
    ]

    # Build the set of order_refs already "spoken for" elsewhere -- BUG FIX:
    # without this, rebuilding an "unresolved" shortlist by date window alone
    # can hand the model a candidate that's already claimed by a different
    # settlement (via exact/fuzzy/possible_batch) or already sitting in
    # someone else's duplicate_conflict pair. Confirmed in real testing: this
    # caused the same ledger record to get matched to 3 unrelated settlements.
    already_spoken_for = set()
    for _, r in deterministic_results.iterrows():
        if r.matched_order_ref and r.resolution_method in ("exact", "fuzzy", "possible_batch"):
            already_spoken_for.update(r.matched_order_ref.split(","))
    duplicate_payment_ids = deterministic_results[
        deterministic_results.resolution_method == "duplicate_conflict"
    ].settlement_payment_id.tolist()
    if duplicate_payment_ids:
        already_spoken_for.update(
            ledger[ledger.gateway_payment_ref.isin(duplicate_payment_ids)].order_ref.tolist()
        )

    llm_results = []
    for _, row in to_review.iterrows():
        if row.settlement_payment_id is None:
            continue  # missing_from_settlement rows have no settlement side to reason about

        s_row = settlements[settlements.payment_id == row.settlement_payment_id]
        if s_row.empty:
            continue
        s_row = s_row.iloc[0]

        if row.resolution_method == "duplicate_conflict":
            shortlist = ledger[ledger.gateway_payment_ref == row.settlement_payment_id].order_ref.tolist()
        elif row.resolution_method == "possible_batch":
            shortlist = row.matched_order_ref.split(",") if row.matched_order_ref else []
        else:  # unresolved -- recompute the same loose window Stage D used,
               # EXCLUDING anything already claimed or in a duplicate pair
               # elsewhere (see already_spoken_for above)
            window = ledger[
                (ledger.order_date >= s_row.payment_date - pd.Timedelta(days=6))
                & (ledger.order_date <= s_row.settlement_date + pd.Timedelta(days=2))
                & (~ledger.order_ref.isin(already_spoken_for))
            ]
            shortlist = window.order_ref.tolist()

        candidates = ledger[ledger.order_ref.isin(shortlist)]
        if row.resolution_method == "possible_batch":
            decision = _ask_llm_batch(s_row, candidates)
        else:
            decision = _ask_llm(s_row, candidates)
        decision["settlement_payment_id"] = row.settlement_payment_id
        decision["original_method"] = row.resolution_method
        llm_results.append(decision)

    return pd.DataFrame(llm_results)


if __name__ == "__main__":
    import sys
    from deterministic_matcher import run_matcher

    # Quota-conscious testing: `python llm_matcher.py` runs everything (10 calls).
    # `python llm_matcher.py possible_batch` runs ONLY that category (2 calls) --
    # use this while iterating on a specific fix instead of burning the full
    # batch against a limited daily quota (e.g. 20 RPD on some free-tier setups).
    only_method = sys.argv[1] if len(sys.argv) > 1 else None

    det_results = run_matcher()
    if only_method:
        det_results = det_results[
            (det_results.resolution_method == only_method)
            | (~det_results.resolution_method.isin(["unresolved", "duplicate_conflict", "possible_batch"]))
        ]
        print(f"[TEST MODE] Only sending '{only_method}' rows to the LLM layer.")

    llm_results = run_llm_layer(det_results)
    if len(llm_results):
        print(llm_results.to_string(index=False))
    else:
        print("No records needed LLM review.")