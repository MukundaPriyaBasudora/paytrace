"""
Combines the deterministic matcher and LLM reasoning layer into the final,
required deliverables: match rate and a categorized exception list.

Classifies every settlement into exactly ONE of three buckets:
  matched             -- a real counterpart, confirmed
  confirmed_no_match  -- correctly resolved as having no counterpart (this
                          includes both real decoy_near_miss cases in our
                          dataset -- a correct "no" is not the same as "gave up")
  needs_human         -- genuinely unresolved -- the real exception list

Ledger orphans (missing_from_settlement) are reported separately, since
they're not unmatched SETTLEMENTS -- they're ledger entries with nothing on
the settlement side at all.

Caches combined results to data/latest_results.json so app.py (Streamlit)
doesn't have to re-run the full pipeline -- and re-trigger LLM calls -- on
every interaction. db.py takes over this role properly later.
"""

import json

import pandas as pd

from deterministic_matcher import run_matcher
from llm_matcher import run_llm_layer
import db


def run_full_pipeline():
    det_results = run_matcher()
    llm_results = run_llm_layer(det_results)
    return det_results, llm_results


def classify(det_results: pd.DataFrame, llm_results: pd.DataFrame):
    llm_lookup = {}
    if len(llm_results):
        for row in llm_results.itertuples():
            llm_lookup[row.settlement_payment_id] = row

    settlement_rows = det_results[det_results.settlement_payment_id.notna()]
    orphan_rows = det_results[det_results.settlement_payment_id.isna()]

    classified = []
    for row in settlement_rows.itertuples():
        pay_id = row.settlement_payment_id

        if row.resolution_method in ("exact", "fuzzy"):
            classified.append(dict(
                settlement_payment_id=pay_id, bucket="matched",
                matched_order_ref=row.matched_order_ref, method=row.resolution_method,
                confidence=row.confidence, reasoning=row.reasoning,
            ))
        elif row.resolution_method in ("possible_batch", "duplicate_conflict", "unresolved"):
            llm_row = llm_lookup.get(pay_id)
            if llm_row is None:
                classified.append(dict(
                    settlement_payment_id=pay_id, bucket="needs_human",
                    matched_order_ref=None, method=row.resolution_method,
                    confidence=None, reasoning="No LLM decision found for this record.",
                ))
                continue

            decision = getattr(llm_row, "decision", "needs_human")
            explanation = getattr(llm_row, "explanation", "")
            confidence = getattr(llm_row, "confidence", None)

            if decision == "match":
                raw_ref = getattr(llm_row, "matched_order_ref", None)
                # Normalize comma-separated batch refs -- Gemini formats them
                # as "ORD-1, ORD-2, ORD-3" (space after comma). Splitting on
                # "," alone leaves a leading space on every item after the
                # first, which then fails to match ground truth exactly and
                # gets wrongly flagged as a false positive. Confirmed in real
                # testing, not a hypothetical -- fix it here, once, at the
                # source, rather than in every place that later reads this field.
                clean_ref = ",".join(p.strip() for p in raw_ref.split(",")) if raw_ref else None
                classified.append(dict(
                    settlement_payment_id=pay_id, bucket="matched",
                    matched_order_ref=clean_ref,
                    method=f"llm_{row.resolution_method}", confidence=confidence, reasoning=explanation,
                ))
            elif decision == "no_match":
                classified.append(dict(
                    settlement_payment_id=pay_id, bucket="confirmed_no_match",
                    matched_order_ref=None, method=f"llm_{row.resolution_method}",
                    confidence=confidence, reasoning=explanation,
                ))
            else:  # needs_human, or any unrecognized value -- fail safe to the honest bucket
                classified.append(dict(
                    settlement_payment_id=pay_id, bucket="needs_human",
                    matched_order_ref=None, method=f"llm_{row.resolution_method}",
                    confidence=confidence,
                    reasoning=explanation or "LLM returned needs_human or an unrecognized decision.",
                ))
        else:
            classified.append(dict(
                settlement_payment_id=pay_id, bucket="needs_human",
                matched_order_ref=None, method=row.resolution_method,
                confidence=None, reasoning=f"Unrecognized resolution_method: {row.resolution_method}",
            ))

    orphans = [dict(order_ref=row.matched_order_ref, reasoning=row.reasoning) for row in orphan_rows.itertuples()]
    return pd.DataFrame(classified), orphans


def compute_metrics(classified: pd.DataFrame, orphans: list):
    total = len(classified)
    matched = int((classified.bucket == "matched").sum())
    confirmed_no_match = int((classified.bucket == "confirmed_no_match").sum())
    needs_human = int((classified.bucket == "needs_human").sum())
    match_rate = matched / total if total else 0.0

    exceptions = classified[classified.bucket == "needs_human"][
        ["settlement_payment_id", "method", "reasoning"]
    ].to_dict(orient="records")

    return dict(
        total_settlements=total, matched=matched, confirmed_no_match=confirmed_no_match,
        needs_human=needs_human, match_rate=round(match_rate, 4),
        exceptions=exceptions, orphaned_ledger_records=orphans,
    )


def _validate_against_ground_truth(classified: pd.DataFrame):
    """DEV-TIME ONLY -- checks the FULL combined pipeline against the hidden
    answer key. A real deployed agent wouldn't have this available."""
    gt = json.load(open("../data/ground_truth.json"))
    errors = []
    for row in classified.itertuples():
        if row.bucket == "matched":
            # pd.notna() catches BOTH None and NaN -- pandas frequently
            # converts None to float NaN when building a DataFrame from mixed
            # dicts, and NaN is truthy in Python, so a plain `if x else []`
            # check silently fails to catch it. Hit this for real in testing.
            if pd.notna(row.matched_order_ref):
                refs = [r.strip() for r in str(row.matched_order_ref).split(",")]
            else:
                refs = []
                errors.append((row.settlement_payment_id, None, "MATCHED BUCKET WITH NO ORDER REF -- classify() bug"))
            for ref in refs:
                truth = next((g for g in gt if g["settlement_id_or_payment_id"] == row.settlement_payment_id and g["order_ref"] == ref), None)
                if truth is None or not truth["should_match"]:
                    errors.append((row.settlement_payment_id, ref, "FALSE POSITIVE"))
                elif truth.get("category") == "ambiguous_no_signal":
                    # A confident guess on a genuinely undecidable case is a
                    # reliability problem even when it happens to be right --
                    # nothing in the visible data justified the confidence.
                    errors.append((row.settlement_payment_id, ref, "OVERCONFIDENT -- guessed correctly on an undecidable case, should have been needs_human"))
        elif row.bucket == "confirmed_no_match":
            true_rows = [g for g in gt if g["settlement_id_or_payment_id"] == row.settlement_payment_id and g["should_match"]]
            if true_rows:
                if true_rows[0].get("category") == "ambiguous_no_signal":
                    errors.append((row.settlement_payment_id, true_rows[0]["order_ref"], "OVERCONFIDENT -- guessed no_match on an undecidable case, should have been needs_human"))
                else:
                    errors.append((row.settlement_payment_id, true_rows[0]["order_ref"], "FALSE NEGATIVE -- missed a real match"))
        # needs_human rows aren't graded right/wrong -- honest uncertainty isn't an error

    print(f"\n--- Full pipeline validation against ground truth (dev-time only) ---")
    print(f"Checked {len(classified)} settlements, {len(errors)} errors")
    for e in errors:
        print("  ERROR:", e)
    return errors


def save_results(classified: pd.DataFrame, orphans: list, metrics: dict, path="../data/latest_results.json"):
    payload = dict(
        classified=classified.to_dict(orient="records"),
        orphaned_ledger_records=orphans,
        metrics={k: v for k, v in metrics.items() if k not in ("exceptions", "orphaned_ledger_records")},
    )
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"Cached combined results to {path}")


if __name__ == "__main__":
    det_results, llm_results = run_full_pipeline()
    classified, orphans = classify(det_results, llm_results)
    metrics = compute_metrics(classified, orphans)

    print(f"Total settlements:     {metrics['total_settlements']}")
    print(f"Matched:               {metrics['matched']}")
    print(f"Confirmed no match:    {metrics['confirmed_no_match']}")
    print(f"Needs human:           {metrics['needs_human']}")
    print(f"MATCH RATE:            {metrics['match_rate']:.1%}")
    print(f"Orphaned ledger recs:  {len(orphans)}")

    _validate_against_ground_truth(classified)
    save_results(classified, orphans, metrics)

    db.save_results(classified, orphans)
    print("Also persisted to SQLite: data/paytrace.db")