"""
SQLite persistence layer -- a real, queryable audit trail alongside the
JSON cache metrics.py already produces (kept as-is; this is additive, not
a replacement, since the JSON path is already proven working).

Two tables:
  match_results   -- one row per settlement: bucket, method, confidence, reasoning
  orphaned_ledger -- one row per ledger entry with no settlement counterpart
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = "../data/paytrace.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS match_results (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_payment_id  TEXT,
    matched_order_ref      TEXT,
    bucket                 TEXT NOT NULL,
    method                 TEXT NOT NULL,
    confidence             REAL,
    reasoning              TEXT,
    created_at             TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orphaned_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref   TEXT NOT NULL,
    reasoning   TEXT,
    created_at  TEXT NOT NULL
);
"""


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = DB_PATH):
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def reset_results(db_path: str = DB_PATH):
    """Clear previous run's data -- each run should reflect the current
    code and data, not a stale mix of old runs."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM match_results")
        conn.execute("DELETE FROM orphaned_ledger")
        conn.commit()


def save_results(classified, orphans, db_path: str = DB_PATH):
    """classified: pandas DataFrame with settlement_payment_id, matched_order_ref,
    bucket, method, confidence, reasoning. orphans: list of dicts with order_ref, reasoning."""
    init_db(db_path)
    reset_results(db_path)
    now = datetime.now(timezone.utc).isoformat()

    with get_conn(db_path) as conn:
        for row in classified.itertuples():
            conn.execute(
                """INSERT INTO match_results
                   (settlement_payment_id, matched_order_ref, bucket, method, confidence, reasoning, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row.settlement_payment_id, row.matched_order_ref, row.bucket,
                 row.method, row.confidence, row.reasoning, now),
            )
        for o in orphans:
            conn.execute(
                "INSERT INTO orphaned_ledger (order_ref, reasoning, created_at) VALUES (?, ?, ?)",
                (o["order_ref"], o["reasoning"], now),
            )
        conn.commit()


def fetch_by_bucket(bucket: str, db_path: str = DB_PATH):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM match_results WHERE bucket = ?", (bucket,)).fetchall()
        return [dict(r) for r in rows]


def fetch_orphans(db_path: str = DB_PATH):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT * FROM orphaned_ledger").fetchall()
        return [dict(r) for r in rows]


def method_breakdown(db_path: str = DB_PATH):
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT method, COUNT(*) as n FROM match_results GROUP BY method").fetchall()
        return {r["method"]: r["n"] for r in rows}


if __name__ == "__main__":
    # Self-test against real data -- not synthetic fixtures -- using the
    # actual latest_results.json metrics.py already produced and validated.
    import json
    import os
    import pandas as pd

    with open("../data/latest_results.json") as f:
        results = json.load(f)

    classified = pd.DataFrame(results["classified"])
    orphans = results["orphaned_ledger_records"]

    test_path = "/tmp/test_paytrace.db"
    if os.path.exists(test_path):
        os.remove(test_path)

    save_results(classified, orphans, db_path=test_path)

    matched = fetch_by_bucket("matched", db_path=test_path)
    no_match = fetch_by_bucket("confirmed_no_match", db_path=test_path)
    needs_human = fetch_by_bucket("needs_human", db_path=test_path)
    orphan_rows = fetch_orphans(db_path=test_path)
    breakdown = method_breakdown(db_path=test_path)

    print(f"matched: {len(matched)}, confirmed_no_match: {len(no_match)}, needs_human: {len(needs_human)}")
    print(f"orphaned_ledger: {len(orphan_rows)}")
    print(f"method breakdown: {breakdown}")

    assert len(matched) == results["metrics"]["matched"], "matched count mismatch vs metrics.py"
    assert len(no_match) == results["metrics"]["confirmed_no_match"], "confirmed_no_match count mismatch"
    assert len(needs_human) == results["metrics"]["needs_human"], "needs_human count mismatch"
    assert len(orphan_rows) == len(orphans), "orphan count mismatch"

    print("\ndb.py self-test passed -- SQLite counts match metrics.py exactly.")
    os.remove(test_path)