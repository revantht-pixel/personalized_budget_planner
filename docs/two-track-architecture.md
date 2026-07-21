# Core platform and optional AI boundary

The product is divided into two independently operable parts. The core platform is the system of
record. AI is an optional source of suggestions and has no ledger write capability.

## Part 1: core platform without AI

Implemented in the current vertical slice:

- Immutable double-entry ledger with integer minor units and reversals.
- Account onboarding and auditable opening balances.
- Calculated account balances, available credit, and external balance snapshots.
- Manual expense, income, and transfer workflows.
- Monthly category budgets, historic averages, and explainable savings insights.
- Responsive local dashboard and JSON API.
- A shared proposal queue that can be used by future statement imports and deterministic rules.
- Account and transaction detail workflows, reversals, review approval, and exact-balance
  statement reconciliation.
- CSV statement staging with pending/cleared status, exact amount/date ledger matching, explicit
  review before posting, and duplicate-file idempotency.

Next core work, in order:

1. Provider-driven pending-to-cleared updates and stronger statement matching controls.
2. Provider ingestion, source documents, and user-authored deterministic categorization rules.
3. Recurring schedules, reimbursement views, refunds, and transfer matching.
4. Budget rollover, sinking-fund goals, cash-flow scenarios, and notifications.
5. Authentication, encrypted provider credentials, backups, exports, and PostgreSQL deployment.

## Part 2: optional AI integration

The `AIProposalAdapter` protocol accepts a financial document and returns a typed proposal draft. The
`AIIntegrationService` can only put that draft into `transaction_proposals` with evidence, confidence,
provider, and model metadata. It cannot call `Ledger.post_transaction`.

```text
Document → AI adapter → Transaction proposal → Deterministic validation
                                             → User approval → Ledger posting
```

No model provider is configured. `DisabledAIAdapter` fails closed and produces no proposal or ledger
side effect. A future provider integration requires explicit user consent, redaction policy, credentials,
evaluation data, and review thresholds before it is enabled.

Planned AI capabilities:

- Receipt extraction and split suggestions.
- Merchant normalization and category/tag proposals.
- Receipt-to-bank-transaction matching and anomaly suggestions.
- Natural-language explanations over verified analytics results.
- Structured conversational analytics through an allow-listed semantic query API.

AI-generated SQL and direct AI ledger writes are explicitly outside the architecture.
