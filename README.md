# Financial Engine

A dependency-free Python/SQLite personal-finance platform built from the supplied architecture plan.
The core application works without AI. Optional AI adapters can only create proposals that pass through
the same deterministic validation and review boundary as imports and rules.

The detailed plan review and recommended architecture are in
[`docs/architecture-review.md`](docs/architecture-review.md).

## Invariants already enforced

- Amounts are signed integer minor units: debit is positive, credit is negative.
- Every posted transaction has at least two postings and balances exactly to zero.
- Every posting in a transaction uses the transaction's currency.
- Posted transactions, postings, and tags are database-protected from update/delete.
- Corrections use reversals; imported source IDs are idempotent and content-checked.
- Tags attach to postings, while correlation IDs group related transactions such as reimbursements.

## Implemented product slice

- Responsive overview dashboard with account placeholders and empty states.
- Account onboarding for cash, bank, credit-card, loan, investment, and custom accounts.
- Auditable opening-balance transactions, calculated balances, and available credit.
- Timestamped bank balance snapshots adjusted by subsequent verified ledger movement.
- Manual expense, income, and transfer entry through the local web API.
- Monthly category budgets, previous-three-month averages, and top-spend reporting.
- Explainable, deterministic budget and spending insights—no AI required.
- Provider-neutral AI adapter contract and mandatory proposal/review/approval workflow.
- Clickable account activity, full double-entry transaction details, and corrective reversals.
- Editable category budgets with live plan totals.
- Statement reconciliation that can only complete at an exact zero difference.
- Non-AI CSV statement import with pending-row staging, deterministic ledger matching,
  reviewed counter-account selection, and content-idempotent re-imports.

Still intentionally out of scope for this slice: live bank-provider connections, automatic pending-to-
cleared provider updates, recurring schedules, FX, hosted authentication/encryption,
forecasting, and a configured model provider. SQLite is appropriate for local use; a hosted or
multi-user deployment should move the same service boundaries to a server database.

## Run locally with Docker

Docker Desktop (or Docker Engine with Compose) is the only requirement. Clone or download this
repository, open a terminal in its directory, and run:

```powershell
docker compose up --build -d
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The first build can take a few minutes while
Docker downloads the Python base image. Check the container with:

```powershell
docker compose ps
docker compose logs -f financial-engine
```

Stop the app with `docker compose down`. Your data remains in the named
`financial-engine_financial_engine_data` Docker volume and will be available the next time you run
`docker compose up -d`.

To use another browser port, copy `.env.example` to `.env`, change
`FINANCIAL_ENGINE_PORT`, and recreate the container:

```powershell
Copy-Item .env.example .env
docker compose up -d
```

The Compose configuration deliberately binds to `127.0.0.1`, runs as a non-root user, removes Linux
capabilities, and keeps the container filesystem read-only except for its data volume. This version
does not include authentication. Do not change the port binding to `0.0.0.0` or expose it to the
internet.

### Back up local data

Create a consistent SQLite backup and copy it into the current directory:

```powershell
docker compose exec -T financial-engine python -c "import sqlite3; source = sqlite3.connect('/data/financial_engine.db'); backup = sqlite3.connect('/data/financial_engine.backup.db'); source.backup(backup); backup.close(); source.close()"
docker compose cp financial-engine:/data/financial_engine.backup.db ./financial_engine.backup.db
```

Database files are ignored by Git. Keep backups somewhere private because they contain financial
information. Running `docker compose down --volumes` permanently removes the Docker-managed data
volume, so use that command only when you intentionally want a fresh database.

## Run from Python for development

Python 3.12 or newer is required.

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m financial_engine.demo
python -m financial_engine.webapp --port 8765 --db financial_engine.db
```

Then open [http://127.0.0.1:8765](http://127.0.0.1:8765). The server binds to localhost by default.

CSV import accepts `Date`, `Description`, and `Amount` headers, or separate `Debit` and `Credit`
columns. Optional `Status` and `Transaction ID` columns improve pending handling and source lineage.
Credit-card exports with positive charge amounts can use the **Charges are positive** convention in
the import dialog.

The two-part implementation boundary is documented in
[`docs/two-track-architecture.md`](docs/two-track-architecture.md).

## Small example

```python
from datetime import datetime, timezone
from financial_engine import AccountType, Ledger, PostingInput

with Ledger("finance.db") as ledger:
    ledger.initialize()
    ledger.create_account(
        code="Assets:Checking", name="Checking", account_type=AccountType.ASSET
    )
    ledger.create_account(
        code="Expenses:Groceries", name="Groceries", account_type=AccountType.EXPENSE
    )
    ledger.post_transaction(
        effective_at=datetime.now(timezone.utc),
        description="Groceries",
        postings=(
            PostingInput("Expenses:Groceries", 5_250, tags={"budget": "food"}),
            PostingInput("Assets:Checking", -5_250),
        ),
    )
```

`5_250` means USD 52.50 when the account currency is USD. The engine does not infer decimal places;
a future currency catalog should own that display concern.
