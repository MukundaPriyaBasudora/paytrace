# PayTrace — AI Finance Controller: Reconciliation Agent

Built for Razorpay's AI Buildathon — Track 4 (AI Finance Controller).

An agent that reconciles a payment settlement report against a merchant's
internal order ledger — two independently-maintained records of the same
underlying transactions that drift apart in predictable, real ways: fee
deductions, timing shifts, missing or corrupted references, duplicate
entries, and batched payouts.

## The problem, briefly

Razorpay pays merchants out in batches, separate from the moment a customer
paid. The merchant's own system independently records "we made a sale."
Today, reconciling these two records is manual, slow, and error-prone —
this project builds an agent that does it instead, with a measured,
validated accuracy rather than an asserted one.

## Architecture

Full diagram and design rationale: **[ARCHITECTURE.md](ARCHITECTURE.md)**

In short: two synthetic data sources → a deterministic matcher that
resolves the confident majority cheaply (109/122, 0 false positives) →
an LLM reasoning layer (Gemini) that makes real judgment calls only on
the genuinely ambiguous remainder → every decision validated against a
hidden ground-truth answer key → persisted to both SQLite and a JSON
cache → shown in a Streamlit dashboard.

## The 11 messiness categories in the dataset

| Category | What it tests |
|---|---|
| `clean` | Straightforward exact match (75% of the dataset) |
| `missing_ref` | Ledger never captured the gateway payment reference |
| `truncated_ref` | Reference cut short (simulated export bug) |
| `fee_confusion` | Ledger logs gross amount, must reconcile against net |
| `timing_shift` | Settlement date lags payment date, can cross a month boundary |
| `partial_refund_lag` | Refund reflected in one system, not yet the other |
| `duplicate_entry` | Same order double-logged; notes field disambiguates |
| `missing_from_ledger` | Settlement with no ledger counterpart at all |
| `missing_from_settlement` | Ledger order with no settlement — likely a failed payment |
| `many_to_one` | One payout batches several separate orders |
| `decoy_near_miss` | Two unrelated records, coincidentally close in amount/date — the precision test |
| `ambiguous_no_signal` | Two genuinely identical candidates, no way to disambiguate — the honesty test |

## How to run

```powershell
pip install -r requirements.txt
cd src
python generate_data.py        # writes data/settlements.csv, ledger.csv, ground_truth.json
python deterministic_matcher.py # resolves the confident majority, no API calls
python llm_matcher.py           # requires GEMINI_API_KEY -- resolves the ambiguous remainder
python metrics.py               # combines both, validates, caches results
cd ..
streamlit run app.py            # launches the dashboard
```

## Validated results (current locked baseline)

| Metric | Value |
|---|---|
| Total settlements | 122 |
| Matched | 116 |
| Confirmed no match | 5 |
| Needs human | 1 |
| **Match rate** | **95.1%** |
| Orphaned ledger records | 5 |
| False positives (deterministic layer, 112 auto-resolved) | **0** |

Resolution breakdown: 95 exact ID matches, 14 fuzzy matches, 3 duplicate
pairs correctly disambiguated via the ledger notes field, 2 batch payouts
confirmed, 8 cases escalated to the LLM as genuinely unresolved.

## Known limitations — stated honestly, not hidden

**Two `OVERCONFIDENT` flags.** Of 3 deliberately "genuinely ambiguous"
test cases (identical amount, identical date, no distinguishing signal),
the LLM correctly abstained (`needs_human`) on 1 and confidently guessed
— and happened to be right — on the other 2. Our own validation
(`_validate_against_ground_truth` in `metrics.py`) catches this
automatically: a correct guess with no justification is still flagged,
because grading only the final answer would silently reward luck.

**Root causes, investigated, not just observed:**
- One of the two overconfident cases had a *noisier* candidate list than
  the others (5 candidates, 3 of them leaked in from unrelated settlements
  via the loose date-window search) — harder to reason about than a clean
  2-candidate tie, and a known gap: the loose fallback window isn't fully
  closed against cross-settlement leakage the way the tight deterministic
  match already is.
- The other overconfident case is structurally identical to the one the
  model gets right — same evidence, same shape of ambiguity — and the
  difference in outcome has no clean data-level explanation. This is a
  genuine, honestly-unresolved model consistency limitation, not something
  we're claiming to have solved.

**Run-to-run determinism.** Earlier testing at `temperature=0.2` showed
real variation between runs (`needs_human` ranged 1-3 on identical code).
Set to `temperature=0` for reproducibility — verified with two consecutive
identical runs — though determinism is not the same as correctness; see
above.

**Synthetic data, by necessity.** Real settlement/ledger data isn't
available for a hackathon build, and using real merchant financial data
would raise obvious privacy concerns even if it were. `ground_truth.json`
is a development/testing artifact that would not exist in production — see
`BUILD_LOG.md` for the full discussion.

Full bug-by-bug history, including the tolerance bug, the candidate-leakage
bug, model-availability issues, and this overconfidence investigation, is
in `BUILD_LOG.md`.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | |
| Backend | None | Streamlit calls Python functions directly — no separate API server needed for a single-user dashboard |
| Source data | CSV | Realistic — how data actually arrives from source systems |
| Working store | SQLite | Every decision logged with method, confidence, reasoning — verifiable, not asserted |
| LLM | Gemini (`gemini-3.5-flash-lite`) | Free tier, no card required; validated against a heavier model first to confirm quality before switching for quota reasons |
| Frontend | Streamlit | Fast to build, still demo-quality |

## The Proof

- **Repo runs**: commands above, tested end-to-end
- **Video**: [[link](https://youtu.be/eYT9P_-7lWo?si=4fS6bnd_x-LtzcYv)]
- **What broke, and how we got out**: see `BUILD_LOG.md` — 12+ real, specific
  bugs found via validation against ground truth, not asserted fixes