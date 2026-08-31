"""
Schema definitions for PayTrace's reconciliation data.

SOURCE A — Settlement Report (SettlementRecord)
    What Razorpay says was paid out to the merchant. This is the "source of
    truth" for how much money actually moved, but it uses Razorpay's own IDs
    and includes fee/tax deductions the merchant's system may not model.

SOURCE B — Internal Order Ledger (LedgerRecord)
    What the merchant's own system recorded when a customer placed an order.
    This is where real-world messiness lives: gateway_payment_ref (the copy
    of Razorpay's payment_id) is deliberately unreliable -- missing,
    truncated, or simply wrong -- because that depends on the merchant's
    checkout integration correctly capturing it.

GROUND TRUTH (GroundTruthLabel)
    NOT part of the real system -- a real merchant wouldn't have this. It's
    our own scaffolding: the hidden answer key used to grade the matcher's
    output afterward. Never shown to the matcher itself.
"""

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional


@dataclass
class SettlementRecord:
    """A record from Razorpay's settlement report (Source A)."""

    settlement_id: str          # e.g. "stl_9K3xLp2Qa1" -- groups payments settled together
    payment_id: str             # e.g. "pay_Hd82ksLQmN0" -- Razorpay's unique payment reference (PRIMARY JOIN KEY)
    razorpay_order_id: str      # e.g. "order_9A33XWu170gUtm"
    gross_amount: float         # amount the customer actually paid, in INR
    fee: float                  # Razorpay's platform fee (2% in our model)
    tax_on_fee: float           # GST on the fee (18% of the fee)
    net_amount: float           # gross_amount - fee - tax_on_fee -- what lands in the merchant's account
    currency: str                # "INR"
    payment_date: date          # when the customer paid
    settlement_date: date       # when funds were actually settled to the merchant
    payment_method: str          # "card" | "upi" | "netbanking" | "wallet"
    status: str                  # "settled" | "refunded" | "partially_refunded"
    refund_amount: float = 0.0

    def to_dict(self):
        d = asdict(self)
        d["payment_date"] = self.payment_date.isoformat()
        d["settlement_date"] = self.settlement_date.isoformat()
        return d


@dataclass
class LedgerRecord:
    """A record from the merchant's internal order ledger (Source B)."""

    order_ref: str                        # merchant's own order number, e.g. "ORD-2026-04113"
    customer_id: str
    order_amount: float                   # what the merchant's system thinks was charged
    order_date: date
    gateway_payment_ref: Optional[str]    # merchant's stored copy of payment_id -- DELIBERATELY UNRELIABLE
    status: str                            # "paid" | "refunded" | "partially_refunded" | "pending"
    refund_amount: float = 0.0
    refund_date: Optional[date] = None
    notes: Optional[str] = None            # free text -- occasionally holds a useful clue

    def to_dict(self):
        d = asdict(self)
        d["order_date"] = self.order_date.isoformat()
        if self.refund_date:
            d["refund_date"] = self.refund_date.isoformat()
        return d


@dataclass
class GroundTruthLabel:
    """The hidden answer key for one settlement/ledger pair -- used only to
    grade the matcher afterward, never given to it."""

    settlement_id_or_payment_id: str   # payment_id of the settlement side; "" if none exists
    order_ref: str                      # order_ref of the ledger side; "" if none exists
    should_match: bool                  # the actual correct answer
    category: str                       # "clean" | "missing_ref" | "truncated_ref" | "fee_confusion" |
                                         # "timing_shift" | "partial_refund_lag" | "duplicate_entry" |
                                         # "missing_from_ledger" | "missing_from_settlement" |
                                         # "many_to_one" | "decoy_near_miss"


if __name__ == "__main__":
    # Self-test: build one of each and confirm serialization round-trips
    # cleanly before anything downstream depends on this file.
    from datetime import date as _date

    s = SettlementRecord(
        settlement_id="stl_TEST01", payment_id="pay_TEST01", razorpay_order_id="order_TEST01",
        gross_amount=999.00, fee=19.98, tax_on_fee=3.60, net_amount=975.42, currency="INR",
        payment_date=_date(2026, 3, 1), settlement_date=_date(2026, 3, 2),
        payment_method="upi", status="settled",
    )
    led_rec = LedgerRecord(
        order_ref="ORD-2026-00001", customer_id="cust_abc123", order_amount=999.00,
        order_date=_date(2026, 3, 1), gateway_payment_ref="pay_TEST01", status="paid",
    )
    g = GroundTruthLabel(
        settlement_id_or_payment_id="pay_TEST01", order_ref="ORD-2026-00001",
        should_match=True, category="clean",
    )

    print("SettlementRecord.to_dict():", s.to_dict())
    print("LedgerRecord.to_dict():    ", led_rec.to_dict())
    print("GroundTruthLabel (asdict):", asdict(g))
    print("\nschema.py self-test passed.")