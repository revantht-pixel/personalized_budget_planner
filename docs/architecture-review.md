# Architecture review

This review is based on `financial_engine_plan.pdf` (three pages, supplied July 19, 2026).

## What the plan gets right

- Double-entry bookkeeping is the right invariant. Split receipts, split tender, transfers,
  reimbursements, debt payments, and refunds all become journal entries instead of special cases.
- Posting-level tags preserve orthogonal reporting dimensions without multiplying category accounts.
- A deterministic route before an AI route reduces cost and makes recurring imports reproducible.
- Schema-constrained AI proposals and a human review queue are appropriate controls.
- Cash-flow forecasts become materially more trustworthy once the underlying ledger is reconciled.

## Issues and corrections

1. **The four-table schema is necessary but not sufficient.** It lacks institution/source identity,
   import idempotency, transaction state, reconciliation, review/audit records, recurring schedules,
   exchange rates, attachment lineage, and reversals. Those belong around the ledger rather than in
   unstructured `raw_data` on a transaction.
2. **Money needs an explicit representation.** Binary floating point is unacceptable. The initial
   implementation stores signed integer minor units and permits only one currency per transaction.
   Multi-currency entries later need explicit source and functional-currency amounts plus an FX rate.
3. **Balance cannot be a normal row constraint.** It spans postings. The implementation builds a
   draft atomically and uses a database trigger to permit `DRAFT -> POSTED` only when at least two
   postings exist, their sum is zero, and their account currencies match.
4. **"Immutable" needs operational semantics.** Posted transactions, postings, and tags cannot be
   edited or deleted. Corrections are equal-and-opposite reversal transactions. Draft AI proposals
   should live in a separate ingestion/review area, not masquerade as ledger facts.
5. **A refundable security deposit is not a cost to amortize.** It is normally an asset until it is
   returned or some portion becomes non-refundable. Prepaid first-month rent can be recognized in
   its applicable period; a true multi-period prepayment can use scheduled recognition entries.
6. **Reimbursements should not be negative income.** At purchase time, the friends' shares debit a
   receivable while only the user's share debits Dining. Repayments debit Cash and credit that
   receivable. A correlation ID groups the entries without mutating history.
7. **An AI-generated confidence number is not calibrated evidence.** Review policy should also use
   deterministic checks: totals, merchant ambiguity, unseen category/account, duplicate likelihood,
   extraction quality, and model/rule version. Thresholds should be measured against labeled data.
8. **Natural-language SQL should not execute arbitrary generated SQL.** Prefer a semantic query API
   or approved read-only views, a read replica/snapshot, parameterization, row/time limits, and a
   validator. The model should propose a query plan; an allow-listed service should execute it.
9. **Tags need governance.** An unconstrained key-value store quickly develops synonyms and typos.
   Keep posting-level flexibility, but define namespaced keys and optional controlled vocabularies.
10. **Forecasting requires more than historical run-rate.** Recurring obligations, expected income,
    account transfer timing, credit-card due dates, confidence bands, and scenario overrides should
    be first-class inputs. Forecasts must never create posted ledger facts.
11. **The document describes a ledger more than a budgeting engine.** Budgets need their own model:
    periods, category allocations, rollover policy, transfers between envelopes, goals, versions, and
    comparisons against an explicitly selected basis. None of those should be encoded as postings.
12. **Cash and accrual questions need separate views.** Amortizing a prepayment can improve expense
    recognition, but it does not undo the real checking-account outflow. The UI should show both cash
    runway and recognized spending; otherwise a "smoothed" budget can conceal an overdraft.
13. **Receipts and bank charges are evidence for the same event.** They must be matched, not posted as
    two transactions. Tax, tip, discounts, returns, and rounding allocation also need deterministic
    reconciliation before a split proposal can be approved.
14. **Financial source data is highly sensitive.** Production design needs encryption and backup
    policy, narrowly scoped provider tokens, attachment retention/redaction, access audit, export and
    deletion controls, and a clear rule that raw data is never sent to a model without consent.

## Costs and tradeoffs

- Double entry increases correctness but also onboarding and UI complexity. Users should see familiar
  categories and accounts while debit/credit mechanics remain behind a guided workflow.
- Accrual features add scheduled jobs and restatement questions. They should be opt-in and shown beside,
  not instead of, cash-basis reports.
- Posting-level tags are flexible but make analytics governance harder. Stable core dimensions may
  eventually deserve typed columns or definition tables while long-tail metadata stays flexible.
- AI-assisted splitting can reduce manual work, but receipt extraction, model usage, review UX, and
  evaluation create meaningful operating cost. Rules and memorized approved mappings should handle
  the common path.
- SQLite keeps this local first slice easy to run. A multi-user or hosted version should move the same
  service boundary to PostgreSQL for stronger concurrency, permissions, backups, and operational tools.

## Recommended bounded architecture

1. **Ingestion:** immutable source documents/events, checksums, institution IDs, deduplication, and
   privacy/redaction policy.
2. **Proposal:** deterministic rules first; an AI extraction/classification adapter second. Both emit
   the same versioned proposal contract with evidence and field-level provenance.
3. **Review:** separate proposal state machine (`PROPOSED`, `NEEDS_REVIEW`, `APPROVED`, `REJECTED`)
   and an audit trail. Approval calls the only ledger-posting service.
4. **Ledger:** accounts, transactions, postings, tags, balance validation, idempotency, reconciliation,
   and reversals. It contains accepted financial facts only.
5. **Projection/read models:** budgets, reimbursement status, cash-flow scenarios, and approved
   analytics views. Derived projections can be rebuilt from ledger facts and configuration.

## Delivery sequence

- **Slice 1 (implemented):** single-currency accounts; atomic balanced posting; integer minor units;
  source idempotency; immutable posted facts; correlation; reversals; trial balance.
- **Slice 2 (partially implemented):** account onboarding, opening balances, balance snapshots,
  monthly budgets and analytics, deterministic insights, provider-neutral proposal/review state, and
  the responsive local dashboard with account/transaction inspection, reversals, budget editing, and
  exact-balance statement reconciliation. CSV statements now stage pending rows, match unique existing
  ledger entries, and require review before posting; provider ingestion and user-authored rules remain.
- **Slice 3:** statement import and reconciliation, transfer matching, refunds, reimbursement views,
  and controlled tag definitions.
- **Slice 4:** budgets and scheduled recognition/sinking-fund goals. Keep cash saving goals separate
  from accrual expense recognition.
- **Slice 5:** cash-flow scenario engine with backtesting and uncertainty ranges.
- **Slice 6:** restricted natural-language analytics over curated read models.
