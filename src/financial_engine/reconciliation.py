"""Statement reconciliation against immutable ledger balances."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4

from .errors import IdempotencyConflict, ReconciliationMismatch
from .ledger import Ledger, _required_text, _utc_now_iso
from .models import (
    Account,
    AccountType,
    ReconciliationPreview,
    ReconciliationRecord,
)


class ReconciliationService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def preview(
        self,
        *,
        account_code: str,
        statement_date: date,
        statement_balance_minor: int,
    ) -> ReconciliationPreview:
        account = self.ledger.get_account(account_code)
        _validate_reconcilable(account)
        if not isinstance(statement_date, date) or isinstance(statement_date, datetime):
            raise ValueError("statement_date must be a date")
        if isinstance(statement_balance_minor, bool) or not isinstance(
            statement_balance_minor, int
        ):
            raise ValueError("statement_balance_minor must be an integer")
        end = datetime.combine(
            statement_date + timedelta(days=1), time.min, tzinfo=timezone.utc
        )
        row = self.ledger.connection.execute(
            """
            SELECT coalesce(sum(p.amount_minor), 0) AS balance_minor
            FROM postings p
            JOIN transactions t ON t.id = p.transaction_id
            WHERE p.account_id = ? AND t.status = 'POSTED' AND t.effective_at < ?
            """,
            (account.id, end.isoformat(timespec="microseconds")),
        ).fetchone()
        ledger_signed = row["balance_minor"]
        ledger_display = -ledger_signed if account.type is AccountType.LIABILITY else ledger_signed
        return ReconciliationPreview(
            account=account,
            statement_date=statement_date,
            statement_balance_minor=statement_balance_minor,
            ledger_balance_minor=ledger_display,
            difference_minor=ledger_display - statement_balance_minor,
        )

    def complete(
        self,
        *,
        account_code: str,
        statement_date: date,
        statement_balance_minor: int,
        completed_by: str,
    ) -> ReconciliationRecord:
        preview = self.preview(
            account_code=account_code,
            statement_date=statement_date,
            statement_balance_minor=statement_balance_minor,
        )
        if preview.difference_minor != 0:
            raise ReconciliationMismatch(
                f"statement differs from the ledger by {preview.difference_minor} minor units"
            )
        completed_by = _required_text(completed_by, "completed_by")
        existing = self.ledger.connection.execute(
            """
            SELECT * FROM reconciliations
            WHERE account_id = ? AND statement_date = ?
            """,
            (preview.account.id, statement_date.isoformat()),
        ).fetchone()
        if existing is not None:
            if existing["statement_balance_minor"] != statement_balance_minor:
                raise IdempotencyConflict("statement date was already reconciled to another balance")
            return _record_from_row(existing, preview.account)

        record_id = str(uuid4())
        created_at = _utc_now_iso()
        ledger_signed = (
            -preview.ledger_balance_minor
            if preview.account.type is AccountType.LIABILITY
            else preview.ledger_balance_minor
        )
        statement_signed = (
            -statement_balance_minor
            if preview.account.type is AccountType.LIABILITY
            else statement_balance_minor
        )
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                INSERT INTO reconciliations(
                    id, account_id, statement_date, statement_balance_minor,
                    ledger_balance_minor, difference_minor, completed_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    record_id,
                    preview.account.id,
                    statement_date.isoformat(),
                    statement_signed,
                    ledger_signed,
                    completed_by,
                    created_at,
                ),
            )
        row = self.ledger.connection.execute(
            "SELECT * FROM reconciliations WHERE id = ?", (record_id,)
        ).fetchone()
        assert row is not None
        return _record_from_row(row, preview.account)

    def history(self, account_code: str, *, limit: int = 24) -> tuple[ReconciliationRecord, ...]:
        account = self.ledger.get_account(account_code)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 120:
            raise ValueError("limit must be between 1 and 120")
        rows = self.ledger.connection.execute(
            """
            SELECT * FROM reconciliations
            WHERE account_id = ?
            ORDER BY statement_date DESC
            LIMIT ?
            """,
            (account.id, limit),
        ).fetchall()
        return tuple(_record_from_row(row, account) for row in rows)


def _record_from_row(row: object, account: Account) -> ReconciliationRecord:
    statement_balance = row["statement_balance_minor"]
    ledger_balance = row["ledger_balance_minor"]
    if account.type is AccountType.LIABILITY:
        statement_balance = -statement_balance
        ledger_balance = -ledger_balance
    return ReconciliationRecord(
        id=row["id"],
        account=account,
        statement_date=date.fromisoformat(row["statement_date"]),
        statement_balance_minor=statement_balance,
        ledger_balance_minor=ledger_balance,
        completed_by=row["completed_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _validate_reconcilable(account: Account) -> None:
    if account.type not in {AccountType.ASSET, AccountType.LIABILITY}:
        raise ValueError("only asset and liability accounts can be reconciled")
