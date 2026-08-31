"""
Synthetic data generator for PayTrace.

Produces three files in data/:
  settlements.csv    -- Source A (Razorpay settlement report)
  ledger.csv          -- Source B (merchant's internal order ledger)
  ground_truth.json   -- the ANSWER KEY (not given to the matcher) used to
                          score match rate, precision, and exceptions later.

Every _make_* function returns a uniform shape --
    (list[SettlementRecord], list[LedgerRecord], list[GroundTruthLabel])
so the orchestrator treats every category identically, even though some
produce 1:1 pairs, some 1:many, and some records with no counterpart at all
(the genuine, permanently-unresolved cases).
"""

import itertools
import json
import random
import string
import uuid
from dataclasses import asdict
from datetime import date, timedelta

from schema import SettlementRecord, LedgerRecord, GroundTruthLabel

random.seed(42)  # reproducible dataset -- keep fixed once the matcher is tested against it

BASE_DATE = date(2026, 3, 1)
_order_counter = itertools.count(1)


# ---------------------------------------------------------------------------
# Low-level helpers
# ----------------------------------------lr-----------------------------------

def _rand_id(prefix: str, length: int = 14) -> str:
    chars = string.ascii_letters + string.digits
    return prefix + "".join(random.choices(chars, k=length))


def _rand_date(start_offset: int = 0, end_offset: int = 55) -> date:
    return BASE_DATE + timedelta(days=random.randint(start_offset, end_offset))


def _rand_amount(low: float = 199, high: float = 15999) -> float:
    return round(random.uniform(low, high), 2)


def _compute_fees(gross: float):
    """Razorpay-style fee model: 2% platform fee + 18% GST on that fee."""
    fee = round(gross * 0.02, 2)
    tax_on_fee = round(fee * 0.18, 2)
    net = round(gross - fee - tax_on_fee, 2)
    return fee, tax_on_fee, net


def _payment_method() -> str:
    return random.choice(["card", "upi", "netbanking", "wallet"])


def _next_order_ref() -> str:
    return f"ORD-2026-{next(_order_counter):05d}"


def _base_settlement(gross: float = None, payment_date: date = None) -> SettlementRecord:
    gross = gross if gross is not None else _rand_amount()
    fee, tax_on_fee, net = _compute_fees(gross)
    payment_date = payment_date or _rand_date()
    settlement_date = payment_date + timedelta(days=random.randint(1, 2))
    return SettlementRecord(
        settlement_id=_rand_id("stl_", 12),
        payment_id=_rand_id("pay_", 14),
        razorpay_order_id=_rand_id("order_", 14),
        gross_amount=gross,
        fee=fee,
        tax_on_fee=tax_on_fee,
        net_amount=net,
        currency="INR",
        payment_date=payment_date,
        settlement_date=settlement_date,
        payment_method=_payment_method(),
        status="settled",
    )


def _base_ledger(order_amount: float, order_date: date, gateway_payment_ref) -> LedgerRecord:
    return LedgerRecord(
        order_ref=_next_order_ref(),
        customer_id=str(uuid.uuid4())[:8],
        order_amount=order_amount,
        order_date=order_date,
        gateway_payment_ref=gateway_payment_ref,
        status="paid",
    )


# ---------------------------------------------------------------------------
# Messiness category generators -- each returns (settlements, ledgers, labels)
# ---------------------------------------------------------------------------

def _make_clean_pair():
    s = _base_settlement()
    l = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=s.payment_id)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "clean")]


def _make_missing_ref():
    s = _base_settlement()
    l = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=None)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "missing_ref")]


def _make_truncated_ref():
    s = _base_settlement()
    truncated = s.payment_id[:-4]  # simulates a CSV export column-width bug
    l = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=truncated)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "truncated_ref")]


def _make_fee_confusion():
    s = _base_settlement()
    # Ledger logs the GROSS sale amount, not the post-fee net amount -- realistic,
    # since the merchant's own system has no visibility into Razorpay's fee cut.
    l = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=None)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "fee_confusion")]


def _make_timing_shift():
    payment_date = _rand_date(0, 30)
    s = _base_settlement(payment_date=payment_date)
    s.settlement_date = payment_date + timedelta(days=random.randint(5, 9))  # can cross a month boundary
    l = _base_ledger(
        order_amount=s.gross_amount,
        order_date=payment_date - timedelta(days=random.choice([0, 1])),
        gateway_payment_ref=s.payment_id,
    )
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "timing_shift")]


def _make_partial_refund_lag():
    s = _base_settlement()
    s.status = "partially_refunded"
    s.refund_amount = round(s.gross_amount * random.uniform(0.2, 0.5), 2)
    # Ledger hasn't caught up yet -- still shows fully paid, no refund recorded.
    l = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=s.payment_id)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, True, "partial_refund_lag")]


def _make_duplicate_entry():
    s = _base_settlement()
    l1 = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=s.payment_id)
    l2 = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=s.payment_id)
    l2.notes = "possible duplicate -- customer double-clicked checkout"
    labels = [
        GroundTruthLabel(s.payment_id, l1.order_ref, True, "duplicate_entry"),
        GroundTruthLabel(s.payment_id, l2.order_ref, False, "duplicate_entry"),  # only ONE should match
    ]
    return [s], [l1, l2], labels


def _make_missing_from_ledger():
    # A genuine settlement with no ledger counterpart at all. Permanently
    # unresolvable; should land in the exception list, not be forced to match.
    s = _base_settlement()
    return [s], [], [GroundTruthLabel(s.payment_id, "", False, "missing_from_ledger")]


def _make_missing_from_settlement():
    # Ledger shows "paid" but the payment never actually settled -- e.g. it
    # was reversed/failed after the order was logged. Also unresolvable.
    order_date = _rand_date()
    l = _base_ledger(order_amount=_rand_amount(), order_date=order_date, gateway_payment_ref=_rand_id("pay_", 14))
    l.notes = "payment likely failed or reversed after order was logged"
    return [], [l], [GroundTruthLabel("", l.order_ref, False, "missing_from_settlement")]


def _make_many_to_one():
    # One settlement batch covers multiple separate ledger orders.
    n_orders = random.choice([2, 3])
    order_amounts = [_rand_amount(199, 4999) for _ in range(n_orders)]
    gross = round(sum(order_amounts), 2)
    payment_date = _rand_date()
    s = _base_settlement(gross=gross, payment_date=payment_date)
    ledgers, labels = [], []
    for amt in order_amounts:
        l = _base_ledger(order_amount=amt, order_date=payment_date, gateway_payment_ref=None)
        l.notes = f"part of batched settlement {s.settlement_id}"
        ledgers.append(l)
        labels.append(GroundTruthLabel(s.payment_id, l.order_ref, True, "many_to_one"))
    return [s], ledgers, labels


def _make_decoy_near_miss():
    # Two INDEPENDENT records -- neither has a true counterpart in this batch --
    # that happen to be suspiciously close in amount and date. The matcher must
    # correctly NOT pair them together. This is the precision test.
    amount_a = _rand_amount(2000, 3000)
    amount_b = round(amount_a + random.uniform(-4, 4), 2)
    date_a = _rand_date()
    date_b = date_a + timedelta(days=random.choice([0, 1]))
    s = _base_settlement(gross=amount_a, payment_date=date_a)
    l = _base_ledger(order_amount=amount_b, order_date=date_b, gateway_payment_ref=None)
    return [s], [l], [GroundTruthLabel(s.payment_id, l.order_ref, False, "decoy_near_miss")]


def _make_ambiguous_no_signal():
    # TWO ledger candidates, SAME amount, SAME date, NO id, NO notes -- unlike
    # duplicate_entry (which has a notes field as a tiebreaker), there is
    # nothing anywhere in the visible data that reveals which one is real.
    # One of them genuinely IS the true match (we know, as the generator) but
    # that information isn't observable -- so the only honest system behavior
    # is "needs_human", not a confident guess in either direction, even if
    # that guess happens to land on the right one.
    s = _base_settlement()
    l1 = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=None)
    l2 = _base_ledger(order_amount=s.gross_amount, order_date=s.payment_date, gateway_payment_ref=None)
    # Deliberately no notes field on either -- that's the whole point.
    labels = [
        GroundTruthLabel(s.payment_id, l1.order_ref, True, "ambiguous_no_signal"),
        GroundTruthLabel(s.payment_id, l2.order_ref, False, "ambiguous_no_signal"),
    ]
    return [s], [l1, l2], labels


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def generate_dataset(output_dir: str = "../data"):
    generator_pool = (
        [_make_clean_pair] * 87
        + [_make_missing_ref] * 6
        + [_make_truncated_ref] * 4
        + [_make_fee_confusion] * 4
        + [_make_timing_shift] * 4
        + [_make_partial_refund_lag] * 4
        + [_make_duplicate_entry] * 3
        + [_make_missing_from_ledger] * 3
        + [_make_missing_from_settlement] * 3
        + [_make_many_to_one] * 2
        + [_make_decoy_near_miss] * 2
        + [_make_ambiguous_no_signal] * 3
    )
    random.shuffle(generator_pool)

    settlements, ledgers, labels = [], [], []
    for gen_fn in generator_pool:
        s_list, l_list, label_list = gen_fn()
        settlements.extend(s_list)
        ledgers.extend(l_list)
        labels.extend(label_list)

    import os
    os.makedirs(output_dir, exist_ok=True)

    _write_csv(os.path.join(output_dir, "settlements.csv"), [s.to_dict() for s in settlements])
    _write_csv(os.path.join(output_dir, "ledger.csv"), [l.to_dict() for l in ledgers])

    with open(os.path.join(output_dir, "ground_truth.json"), "w") as f:
        json.dump([asdict(lbl) for lbl in labels], f, indent=2)

    print(f"settlements: {len(settlements)} records")
    print(f"ledger:      {len(ledgers)} records")
    print(f"ground truth labels: {len(labels)} records")
    print(f"written to {os.path.abspath(output_dir)}/")


def _write_csv(path, rows):
    import csv
    if not rows:
        return
    keys = rows[0].keys()
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})


if __name__ == "__main__":
    generate_dataset()