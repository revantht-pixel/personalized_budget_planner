"""SQLite schema and database-level ledger guards."""

SCHEMA_VERSION = 5

SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('ASSET', 'LIABILITY', 'EQUITY', 'INCOME', 'EXPENSE')),
    currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_profiles (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id),
    kind TEXT NOT NULL CHECK (kind IN (
        'CHECKING', 'SAVINGS', 'CASH', 'CREDIT_CARD', 'LOAN', 'INVESTMENT',
        'GIFT_CARD', 'RECEIVABLE', 'CUSTOM_ASSET', 'CUSTOM_LIABILITY', 'CATEGORY'
    )),
    credit_limit_minor INTEGER CHECK (credit_limit_minor IS NULL OR credit_limit_minor >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    observed_at TEXT NOT NULL,
    ledger_balance_minor INTEGER NOT NULL,
    available_to_spend_minor INTEGER,
    source TEXT NOT NULL,
    external_id TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS balance_snapshot_external_identity
    ON balance_snapshots(account_id, source, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS balance_snapshots_latest
    ON balance_snapshots(account_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    description TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    merchant_name TEXT,
    source_type TEXT,
    source_id TEXT,
    content_hash TEXT NOT NULL,
    correlation_id TEXT,
    reversal_of_transaction_id TEXT REFERENCES transactions(id),
    status TEXT NOT NULL CHECK (status IN ('DRAFT', 'POSTED')),
    created_at TEXT NOT NULL,
    posted_at TEXT,
    CHECK ((source_type IS NULL) = (source_id IS NULL)),
    CHECK (
        (status = 'DRAFT' AND posted_at IS NULL)
        OR (status = 'POSTED' AND posted_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS transactions_source_identity
    ON transactions(source_type, source_id)
    WHERE source_type IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_reversal_per_transaction
    ON transactions(reversal_of_transaction_id)
    WHERE reversal_of_transaction_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS transactions_effective_at
    ON transactions(effective_at);

CREATE INDEX IF NOT EXISTS transactions_correlation_id
    ON transactions(correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS postings (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_minor INTEGER NOT NULL CHECK (amount_minor <> 0),
    memo TEXT,
    sort_order INTEGER NOT NULL CHECK (sort_order >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (transaction_id, sort_order)
);

CREATE INDEX IF NOT EXISTS postings_account_id
    ON postings(account_id);

CREATE TABLE IF NOT EXISTS posting_tags (
    posting_id TEXT NOT NULL REFERENCES postings(id),
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (posting_id, key),
    CHECK (length(trim(key)) > 0),
    CHECK (length(trim(value)) > 0)
);

CREATE TABLE IF NOT EXISTS budgets (
    id TEXT PRIMARY KEY,
    month TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (month, currency)
);

CREATE TABLE IF NOT EXISTS budget_allocations (
    id TEXT PRIMARY KEY,
    budget_id TEXT NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
    is_flexible INTEGER NOT NULL DEFAULT 1 CHECK (is_flexible IN (0, 1)),
    UNIQUE (budget_id, account_id)
);

CREATE TABLE IF NOT EXISTS transaction_proposals (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('NEEDS_REVIEW', 'APPROVED', 'REJECTED')),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    payload_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    provider_metadata_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    approved_transaction_id TEXT REFERENCES transactions(id),
    reviewed_by TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (source_type, source_id)
);

CREATE INDEX IF NOT EXISTS transaction_proposals_review_queue
    ON transaction_proposals(status, created_at);

CREATE TABLE IF NOT EXISTS reconciliations (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    statement_date TEXT NOT NULL,
    statement_balance_minor INTEGER NOT NULL,
    ledger_balance_minor INTEGER NOT NULL,
    difference_minor INTEGER NOT NULL CHECK (difference_minor = 0),
    completed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, statement_date)
);

CREATE INDEX IF NOT EXISTS reconciliations_account_date
    ON reconciliations(account_id, statement_date DESC);

CREATE TABLE IF NOT EXISTS statement_import_batches (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    filename TEXT NOT NULL,
    amount_convention TEXT NOT NULL CHECK (
        amount_convention IN ('ACCOUNT_SIGNED', 'CHARGES_POSITIVE')
    ),
    content_hash TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, content_hash)
);

CREATE TABLE IF NOT EXISTS statement_import_rows (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES statement_import_batches(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    row_number INTEGER NOT NULL CHECK (row_number > 0),
    external_id TEXT,
    row_hash TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    description TEXT NOT NULL,
    account_amount_minor INTEGER NOT NULL CHECK (account_amount_minor <> 0),
    bank_status TEXT NOT NULL CHECK (bank_status IN ('PENDING', 'CLEARED')),
    workflow_status TEXT NOT NULL CHECK (
        workflow_status IN ('PENDING', 'NEEDS_REVIEW', 'MATCHED', 'POSTED', 'IGNORED')
    ),
    matched_transaction_id TEXT REFERENCES transactions(id),
    suggested_account_id TEXT REFERENCES accounts(id),
    posted_transaction_id TEXT REFERENCES transactions(id),
    raw_json TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, row_number)
);

CREATE INDEX IF NOT EXISTS statement_import_rows_review
    ON statement_import_rows(workflow_status, effective_date);

CREATE INDEX IF NOT EXISTS statement_import_rows_external
    ON statement_import_rows(account_id, external_id)
    WHERE external_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS statement_import_rows_hash
    ON statement_import_rows(account_id, row_hash);

-- Learned "merchant -> category" rules. These are suggestions for review only;
-- they never post anything on their own.
CREATE TABLE IF NOT EXISTS categorization_rules (
    id TEXT PRIMARY KEY,
    merchant_key TEXT NOT NULL UNIQUE CHECK (length(trim(merchant_key)) > 0),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    times_used INTEGER NOT NULL DEFAULT 0 CHECK (times_used >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A posted transaction is only reachable through a balanced DRAFT -> POSTED
-- transition. Reports ignore drafts, so partially-created entries never leak.
CREATE TRIGGER IF NOT EXISTS transactions_must_start_as_draft
BEFORE INSERT ON transactions
WHEN NEW.status <> 'DRAFT'
BEGIN
    SELECT RAISE(ABORT, 'transactions must start as drafts');
END;

CREATE TRIGGER IF NOT EXISTS validate_transaction_before_posting
BEFORE UPDATE OF status ON transactions
WHEN OLD.status = 'DRAFT' AND NEW.status = 'POSTED'
BEGIN
    SELECT CASE WHEN (
        SELECT count(*) FROM postings WHERE transaction_id = OLD.id
    ) < 2 THEN RAISE(ABORT, 'a posted transaction needs at least two postings') END;

    SELECT CASE WHEN (
        SELECT coalesce(sum(amount_minor), 0) FROM postings WHERE transaction_id = OLD.id
    ) <> 0 THEN RAISE(ABORT, 'postings do not balance') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM postings p
        JOIN accounts a ON a.id = p.account_id
        WHERE p.transaction_id = OLD.id AND a.currency <> NEW.currency
    ) THEN RAISE(ABORT, 'posting currency does not match transaction currency') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM postings p
        JOIN accounts a ON a.id = p.account_id
        WHERE p.transaction_id = OLD.id AND a.is_active = 0
    ) THEN RAISE(ABORT, 'inactive account cannot receive a posting') END;
END;

CREATE TRIGGER IF NOT EXISTS posted_transactions_are_immutable
BEFORE UPDATE ON transactions
WHEN OLD.status = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS posted_transactions_cannot_be_deleted
BEFORE DELETE ON transactions
WHEN OLD.status = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'posted transactions cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS no_postings_added_to_posted_transaction
BEFORE INSERT ON postings
WHEN (SELECT status FROM transactions WHERE id = NEW.transaction_id) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'postings on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS posted_postings_cannot_change
BEFORE UPDATE ON postings
WHEN (SELECT status FROM transactions WHERE id = OLD.transaction_id) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'postings on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS posted_postings_cannot_be_deleted
BEFORE DELETE ON postings
WHEN (SELECT status FROM transactions WHERE id = OLD.transaction_id) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'postings on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS no_tags_added_to_posted_transaction
BEFORE INSERT ON posting_tags
WHEN (
    SELECT t.status
    FROM postings p JOIN transactions t ON t.id = p.transaction_id
    WHERE p.id = NEW.posting_id
) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'tags on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS posted_tags_cannot_change
BEFORE UPDATE ON posting_tags
WHEN (
    SELECT t.status
    FROM postings p JOIN transactions t ON t.id = p.transaction_id
    WHERE p.id = OLD.posting_id
) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'tags on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS posted_tags_cannot_be_deleted
BEFORE DELETE ON posting_tags
WHEN (
    SELECT t.status
    FROM postings p JOIN transactions t ON t.id = p.transaction_id
    WHERE p.id = OLD.posting_id
) = 'POSTED'
BEGIN
    SELECT RAISE(ABORT, 'tags on posted transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS used_account_dimensions_are_immutable
BEFORE UPDATE OF type, currency ON accounts
WHEN EXISTS (
    SELECT 1
    FROM postings p JOIN transactions t ON t.id = p.transaction_id
    WHERE p.account_id = OLD.id AND t.status = 'POSTED'
) AND (NEW.type <> OLD.type OR NEW.currency <> OLD.currency)
BEGIN
    SELECT RAISE(ABORT, 'type and currency of a used account are immutable');
END;
"""
