"""
Deterministic matcher -- Stages A-D, designed and stress-tested against
every category in our own synthetic dataset before writing this code.

Design principle: NEVER be confidently wrong. A record only gets
auto-resolved if it clears an ABSOLUTE tolerance bar, not just "closest
available candidate" -- a lone near-miss (the decoy case) must not sail
through just because nothing else is competing for it.

Standalone for now: returns results as a list of dicts / DataFrame and
prints a summary. Persistence into db.py's SQLite store gets wired in once
that file exists -- this file doesn't need to wait for it to be useful.
"""

import json
from datetime import timedelta
from itertools import combinations

import pandas as pd

TOLERANCE_ABS = 0.10      # rupees -- tight absolute bar for auto-accept.
                            # True matches in this dataset differ by exactly
                            # Rs 0.00; the closest known decoy differs by
                            # Rs 0.70. This value sits safely in that gap.
DATE_WINDOW_BEFORE = 6      # days before payment_date to search
DATE_WINDOW_AFTER = 2       # days after settlement_date to search


def load_data():
    settlements = pd.read_csv("../data/settlements.csv", parse_dates=["payment_date", "settlement_date"])
    ledger = pd.read_csv("../data/ledger.csv", parse_dates=["order_date"])
    return settlements, ledger


def run_matcher():
    settlements, ledger = load_data()
    results = []  # list of dicts -- becomes SQLite rows once db.py exists

    claimed_refs = set()
    duplicate_candidate_refs = set()
    ambiguous_candidate_refs = set()  # refs offered as candidates in an
        # unresolved settlement's shortlist -- must NOT also be logged as
        # orphaned in the reverse pass, they're not unclaimed, they're
        # pending a decision. Found via real testing: without this, both
        # halves of an ambiguous_no_signal pair got double-logged as
        # "missing_from_settlement" even though they're live candidates.

    # ---- Stage A: exact ID match ----
    unresolved_settlements = []
    for s in settlements.itertuples():
        candidates = ledger[ledger.gateway_payment_ref == s.payment_id]
        if len(candidates) == 1:
            l = candidates.iloc[0]
            claimed_refs.add(l.order_ref)
            flag, reasoning = None, f"Exact ID match on {s.payment_id}"
            amt_ok = abs(l.order_amount - s.gross_amount) <= TOLERANCE_ABS or abs(l.order_amount - s.net_amount) <= TOLERANCE_ABS
            if not amt_ok:
                flag = "amount_mismatch_despite_id_match"
                reasoning += f"; WARNING amount differs (ledger {l.order_amount} vs gross {s.gross_amount}/net {s.net_amount})"
            if s.status == "partially_refunded" and l.refund_amount == 0:
                flag = "refund_mismatch"
                reasoning += f"; settlement shows refund {s.refund_amount:.2f} not yet reflected in ledger"
            results.append(dict(settlement_payment_id=s.payment_id, matched_order_ref=l.order_ref,
                                  resolution_method="exact", confidence=1.0 if not flag else 0.85,
                                  flag=flag, reasoning=reasoning))
        elif len(candidates) > 1:
            refs = candidates.order_ref.tolist()
            duplicate_candidate_refs.update(refs)
            results.append(dict(settlement_payment_id=s.payment_id, matched_order_ref=None,
                                  resolution_method="duplicate_conflict", confidence=None, flag=None,
                                  reasoning=f"ID {s.payment_id} matches {len(refs)} ledger records ({', '.join(refs)}) -- needs human pick"))
        else:
            unresolved_settlements.append(s)

    # ---- Stage B: fuzzy match with an ABSOLUTE confidence floor ----
    still_unresolved = []
    for s in unresolved_settlements:
        pool = ledger[~ledger.order_ref.isin(claimed_refs | duplicate_candidate_refs)]
        window = pool[
            (pool.order_date >= s.payment_date - timedelta(days=DATE_WINDOW_BEFORE))
            & (pool.order_date <= s.settlement_date + timedelta(days=DATE_WINDOW_AFTER))
        ].copy()
        if len(window):
            window["diff"] = window.order_amount.apply(lambda a: min(abs(a - s.gross_amount), abs(a - s.net_amount)))
        strong = window[window["diff"] <= TOLERANCE_ABS] if len(window) else window

        if len(strong) == 1:
            l = strong.iloc[0]
            claimed_refs.add(l.order_ref)
            basis = "gross" if abs(l.order_amount - s.gross_amount) <= TOLERANCE_ABS else "net"
            results.append(dict(settlement_payment_id=s.payment_id, matched_order_ref=l.order_ref,
                                  resolution_method="fuzzy", confidence=0.9, flag=None,
                                  reasoning=f"Single strong candidate in date window, amount matches {basis} amount (diff {l['diff']:.2f})"))
        elif len(strong) > 1:
            ambiguous_candidate_refs.update(strong.order_ref.tolist())
            still_unresolved.append((s, strong.order_ref.tolist()))
        else:
            loose = window[window["diff"] <= 50] if len(window) else window
            still_unresolved.append((s, loose.order_ref.tolist()))

    # ---- Stage C: many-to-one cluster check (proposed, not auto-accepted) ----
    remaining = []
    for s, shortlist in still_unresolved:
        pool = ledger[
            ledger.order_ref.isin(shortlist)
            | ((ledger.order_date == s.payment_date) & ~ledger.order_ref.isin(claimed_refs | duplicate_candidate_refs))
        ]
        found = False
        for r in (2, 3):
            if found or len(pool) < r:
                continue
            for combo in combinations(pool.itertuples(), r):
                total = sum(c.order_amount for c in combo)
                if abs(total - s.gross_amount) <= TOLERANCE_ABS:
                    refs = [c.order_ref for c in combo]
                    claimed_refs.update(refs)
                    results.append(dict(settlement_payment_id=s.payment_id, matched_order_ref=",".join(refs),
                                          resolution_method="possible_batch", confidence=0.6, flag=None,
                                          reasoning=f"Sum of {len(refs)} same-date ledger records ({total:.2f}) matches settlement gross ({s.gross_amount:.2f}) -- proposed, needs confirmation"))
                    found = True
                    break
        if not found:
            remaining.append((s, shortlist))

    # ---- Stage D: log the rest as unresolved, shortlist attached for the LLM layer ----
    for s, shortlist in remaining:
        reasoning = (f"No confident deterministic match. Candidates for LLM review: {shortlist}"
                     if shortlist else "No candidates found within date/amount window.")
        results.append(dict(settlement_payment_id=s.payment_id, matched_order_ref=None,
                              resolution_method="unresolved", confidence=None, flag=None, reasoning=reasoning))

    # ---- Reverse pass: ledger records no settlement ever claimed ----
    unclaimed = ledger[~ledger.order_ref.isin(claimed_refs)]
    for l in unclaimed.itertuples():
        if l.order_ref in duplicate_candidate_refs:
            continue  # already logged under the settlement's duplicate_conflict row
        if l.order_ref in ambiguous_candidate_refs:
            continue  # already logged under an unresolved settlement's shortlist -- a live
                       # candidate awaiting a decision, not an orphan
        results.append(dict(settlement_payment_id=None, matched_order_ref=l.order_ref,
                              resolution_method="missing_from_settlement", confidence=None, flag=None,
                              reasoning="No settlement record claims this ledger entry -- possible unrecorded/failed payment"))

    return pd.DataFrame(results)


def _validate_against_ground_truth(results: pd.DataFrame):
    """DEV-TIME ONLY -- a real deployed agent wouldn't have ground truth to
    check against. This exists purely so WE can trust our own numbers before
    calling this stage done, the same way we caught the tolerance bug earlier."""
    gt = pd.DataFrame(json.load(open("../data/ground_truth.json")))

    auto = results[results.resolution_method.isin(["exact", "fuzzy"])]
    false_positives = []
    for _, r in auto.iterrows():
        truth = gt[(gt.settlement_id_or_payment_id == r.settlement_payment_id) & (gt.order_ref == r.matched_order_ref)]
        if truth.empty or not truth.iloc[0].should_match:
            false_positives.append((r.settlement_payment_id, r.matched_order_ref))

    print(f"\n--- Validation against ground truth (dev-time only) ---")
    print(f"Auto-resolved matches: {len(auto)}")
    print(f"False positives: {len(false_positives)}")
    for fp in false_positives:
        print("  FALSE POSITIVE:", fp)


if __name__ == "__main__":
    df = run_matcher()
    print("Resolution method breakdown:")
    print(df.resolution_method.value_counts())
    match_rate = df.resolution_method.isin(["exact", "fuzzy", "possible_batch"]).sum() / len(df)
    print(f"\nProvisional match rate (pre-LLM-layer): {match_rate:.0%}")
    _validate_against_ground_truth(df)