"""Transactional service for the immutable double-entry ledger."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from uuid import uuid4

from .errors import (
    AccountNotFound,
    CurrencyMismatch,
    DuplicateAccount,
    IdempotencyConflict,
    InvalidTransaction,
    PeriodLocked,
    TransactionNotFound,
)
from .models import (
    Account,
    AccountBalance,
    AccountKind,
    AccountOverview,
    AccountType,
    BalanceSnapshot,
    JournalTransaction,
    Posting,
    PostingInput,
)
from .schema import SCHEMA_SQL, SCHEMA_VERSION

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class Ledger:
    """Owns one SQLite connection and all ledger state transitions."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(database), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA_SQL)
        now = _utc_now_iso()
        # Backfill profiles when opening a database created by schema v1.
        self.connection.execute(
            """
            INSERT OR IGNORE INTO account_profiles(
                account_id, kind, credit_limit_minor, created_at, updated_at
            )
            SELECT id,
                   CASE type
                       WHEN 'ASSET' THEN 'CUSTOM_ASSET'
                       WHEN 'LIABILITY' THEN 'CUSTOM_LIABILITY'
                       ELSE 'CATEGORY'
                   END,
                   NULL, created_at, ?
            FROM accounts
            """,
            (now,),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_versions(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION, now),
        )

    @contextmanager
    def _write(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")

    def create_account(
        self,
        *,
        code: str,
        name: str,
        account_type: AccountType | str,
        currency: str = "USD",
        account_id: str | None = None,
        kind: AccountKind | str | None = None,
        credit_limit_minor: int | None = None,
    ) -> Account:
        code = _required_text(code, "account code")
        name = _required_text(name, "account name")
        currency = _validate_currency(currency)
        try:
            normalized_type = AccountType(account_type)
        except ValueError as exc:
            raise ValueError(f"unsupported account type: {account_type!r}") from exc
        normalized_kind = (
            AccountKind(kind) if kind is not None else _default_kind_for_account_type(normalized_type)
        )
        if credit_limit_minor is not None and (
            isinstance(credit_limit_minor, bool)
            or not isinstance(credit_limit_minor, int)
            or credit_limit_minor < 0
        ):
            raise ValueError("credit_limit_minor must be a non-negative integer")
        if credit_limit_minor is not None and normalized_kind is not AccountKind.CREDIT_CARD:
            raise ValueError("credit limits are only valid for credit-card accounts")

        account_id = account_id or str(uuid4())
        created_at = _utc_now_iso()
        try:
            with self._write():
                self.connection.execute(
                    """
                    INSERT INTO accounts(id, code, name, type, currency, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (account_id, code, name, normalized_type.value, currency, created_at),
                )
                self.connection.execute(
                    """
                    INSERT INTO account_profiles(
                        account_id, kind, credit_limit_minor, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        normalized_kind.value,
                        credit_limit_minor,
                        created_at,
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "accounts.code" in str(exc):
                raise DuplicateAccount(f"account code already exists: {code}") from exc
            raise
        return self.get_account(code)

    def onboard_account(
        self,
        *,
        code: str,
        name: str,
        kind: AccountKind | str,
        opening_balance_minor: int,
        balance_as_of: datetime,
        currency: str = "USD",
        credit_limit_minor: int | None = None,
    ) -> AccountOverview:
        """Create a balance-sheet account and record its auditable opening balance."""

        normalized_kind = AccountKind(kind)
        account_type = _account_type_for_kind(normalized_kind)
        _as_utc_iso(balance_as_of)
        if isinstance(opening_balance_minor, bool) or not isinstance(opening_balance_minor, int):
            raise ValueError("opening_balance_minor must be an integer")
        if opening_balance_minor < 0:
            raise ValueError("enter opening assets or amounts owed as non-negative values")
        if normalized_kind is AccountKind.CATEGORY:
            raise ValueError("category accounts cannot be onboarded with an available balance")

        account = self.create_account(
            code=code,
            name=name,
            account_type=account_type,
            currency=currency,
            kind=normalized_kind,
            credit_limit_minor=credit_limit_minor,
        )
        if opening_balance_minor:
            equity_code = f"Equity:OpeningBalances:{currency}"
            try:
                self.get_account(equity_code)
            except AccountNotFound:
                try:
                    self.create_account(
                        code=equity_code,
                        name=f"Opening balances ({currency})",
                        account_type=AccountType.EQUITY,
                        currency=currency,
                        kind=AccountKind.CATEGORY,
                    )
                except DuplicateAccount:
                    # Another writer may have created the shared offset account.
                    pass

            ledger_amount = (
                -opening_balance_minor
                if account_type is AccountType.LIABILITY
                else opening_balance_minor
            )
            self.post_transaction(
                effective_at=balance_as_of,
                description=f"Opening balance: {name}",
                currency=currency,
                source_type="OPENING_BALANCE",
                source_id=account.id,
                postings=(
                    PostingInput(account.code, ledger_amount, tags={"system": "opening-balance"}),
                    PostingInput(equity_code, -ledger_amount),
                ),
            )
        return self.get_account_overview(account.code)

    def get_account(self, code: str) -> Account:
        row = self.connection.execute(
            "SELECT * FROM accounts WHERE code = ?", (code,)
        ).fetchone()
        if row is None:
            raise AccountNotFound(f"unknown account: {code}")
        return _account_from_row(row)

    def list_accounts(self) -> tuple[Account, ...]:
        rows = self.connection.execute("SELECT * FROM accounts ORDER BY code").fetchall()
        return tuple(_account_from_row(row) for row in rows)

    def record_balance_snapshot(
        self,
        *,
        account_code: str,
        observed_at: datetime,
        ledger_balance_minor: int,
        available_to_spend_minor: int | None,
        source: str,
        external_id: str | None = None,
    ) -> BalanceSnapshot:
        account = self.get_account(account_code)
        if isinstance(ledger_balance_minor, bool) or not isinstance(ledger_balance_minor, int):
            raise ValueError("ledger_balance_minor must be an integer")
        if available_to_spend_minor is not None and (
            isinstance(available_to_spend_minor, bool)
            or not isinstance(available_to_spend_minor, int)
        ):
            raise ValueError("available_to_spend_minor must be an integer or None")
        source = _required_text(source, "balance source")
        external_id = _optional_text(external_id)
        observed_at_iso = _as_utc_iso(observed_at)
        created_at = _utc_now_iso()
        snapshot_id = str(uuid4())
        with self._write():
            if external_id is not None:
                existing = self.connection.execute(
                    """
                    SELECT * FROM balance_snapshots
                    WHERE account_id = ? AND source = ? AND external_id = ?
                    """,
                    (account.id, source, external_id),
                ).fetchone()
                if existing is not None:
                    return _snapshot_from_row(existing)
            self.connection.execute(
                """
                INSERT INTO balance_snapshots(
                    id, account_id, observed_at, ledger_balance_minor,
                    available_to_spend_minor, source, external_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    account.id,
                    observed_at_iso,
                    ledger_balance_minor,
                    available_to_spend_minor,
                    source,
                    external_id,
                    created_at,
                ),
            )
        row = self.connection.execute(
            "SELECT * FROM balance_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        assert row is not None
        return _snapshot_from_row(row)

    def get_account_overview(self, account_code: str) -> AccountOverview:
        row = self.connection.execute(
            """
            SELECT a.*, ap.kind, ap.credit_limit_minor,
                   coalesce(sum(
                       CASE WHEN t.status = 'POSTED' THEN p.amount_minor ELSE 0 END
                   ), 0) AS ledger_balance_minor
            FROM accounts a
            JOIN account_profiles ap ON ap.account_id = a.id
            LEFT JOIN postings p ON p.account_id = a.id
            LEFT JOIN transactions t ON t.id = p.transaction_id
            WHERE a.code = ?
            GROUP BY a.id
            """,
            (account_code,),
        ).fetchone()
        if row is None:
            raise AccountNotFound(f"unknown account: {account_code}")
        snapshot = self.connection.execute(
            """
            SELECT * FROM balance_snapshots
            WHERE account_id = ?
            ORDER BY observed_at DESC, created_at DESC
            LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        account = _account_from_row(row)
        kind = AccountKind(row["kind"])
        ledger_balance = row["ledger_balance_minor"]
        display_balance = -ledger_balance if account.type is AccountType.LIABILITY else ledger_balance
        if snapshot is not None and snapshot["available_to_spend_minor"] is not None:
            # Preserve the bank's pending/hold adjustment, then apply verified ledger
            # movement since the snapshot's reported ledger balance.
            available = (
                snapshot["available_to_spend_minor"]
                + ledger_balance
                - snapshot["ledger_balance_minor"]
            )
            source = f"{snapshot['source']}+LEDGER"
            observed_at = _parse_datetime(snapshot["observed_at"])
        elif kind is AccountKind.CREDIT_CARD and row["credit_limit_minor"] is not None:
            available = row["credit_limit_minor"] + ledger_balance
            source = "CALCULATED"
            observed_at = None
        elif account.type is AccountType.ASSET:
            available = ledger_balance
            source = "CALCULATED"
            observed_at = None
        else:
            available = None
            source = "CALCULATED"
            observed_at = None
        return AccountOverview(
            account=account,
            kind=kind,
            ledger_balance_minor=ledger_balance,
            display_balance_minor=display_balance,
            available_balance_minor=available,
            credit_limit_minor=row["credit_limit_minor"],
            balance_source=source,
            observed_at=observed_at,
        )

    def list_account_overviews(self, *, include_categories: bool = False) -> tuple[AccountOverview, ...]:
        rows = self.connection.execute(
            """
            SELECT a.code FROM accounts a
            JOIN account_profiles ap ON ap.account_id = a.id
            WHERE (? = 1 OR ap.kind <> 'CATEGORY')
            ORDER BY a.name
            """,
            (int(include_categories),),
        ).fetchall()
        return tuple(self.get_account_overview(row["code"]) for row in rows)

    def post_transaction(
        self,
        *,
        effective_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        currency: str = "USD",
        merchant_name: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
        correlation_id: str | None = None,
        transaction_id: str | None = None,
    ) -> JournalTransaction:
        return self._post_transaction(
            effective_at=effective_at,
            description=description,
            postings=postings,
            currency=currency,
            merchant_name=merchant_name,
            source_type=source_type,
            source_id=source_id,
            correlation_id=correlation_id,
            reversal_of_transaction_id=None,
            transaction_id=transaction_id,
        )

    def _post_transaction(
        self,
        *,
        effective_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        currency: str,
        merchant_name: str | None,
        source_type: str | None,
        source_id: str | None,
        correlation_id: str | None,
        reversal_of_transaction_id: str | None,
        transaction_id: str | None,
    ) -> JournalTransaction:
        description = _required_text(description, "description")
        currency = _validate_currency(currency)
        effective_at_iso = _as_utc_iso(effective_at)
        normalized_postings = tuple(postings)
        self._validate_posting_inputs(normalized_postings)

        source_type = _optional_text(source_type)
        source_id = _optional_text(source_id)
        if (source_type is None) != (source_id is None):
            raise InvalidTransaction("source_type and source_id must be supplied together")
        merchant_name = _optional_text(merchant_name)
        correlation_id = _optional_text(correlation_id)

        content_hash = _content_hash(
            effective_at=effective_at_iso,
            description=description,
            currency=currency,
            merchant_name=merchant_name,
            source_type=source_type,
            source_id=source_id,
            correlation_id=correlation_id,
            reversal_of_transaction_id=reversal_of_transaction_id,
            postings=normalized_postings,
        )

        with self._write():
            if source_type is not None:
                existing = self.connection.execute(
                    """
                    SELECT id, content_hash FROM transactions
                    WHERE source_type = ? AND source_id = ? AND status = 'POSTED'
                    """,
                    (source_type, source_id),
                ).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise IdempotencyConflict(
                            f"source identity {source_type}:{source_id} has different content"
                        )
                    existing_id = existing["id"]
                    # Commit the read-only transaction before hydrating the result.
                    transaction_id_to_return = existing_id
                else:
                    transaction_id_to_return = self._insert_and_post(
                        transaction_id=transaction_id or str(uuid4()),
                        effective_at_iso=effective_at_iso,
                        description=description,
                        currency=currency,
                        merchant_name=merchant_name,
                        source_type=source_type,
                        source_id=source_id,
                        content_hash=content_hash,
                        correlation_id=correlation_id,
                        reversal_of_transaction_id=reversal_of_transaction_id,
                        postings=normalized_postings,
                    )
            else:
                transaction_id_to_return = self._insert_and_post(
                    transaction_id=transaction_id or str(uuid4()),
                    effective_at_iso=effective_at_iso,
                    description=description,
                    currency=currency,
                    merchant_name=merchant_name,
                    source_type=None,
                    source_id=None,
                    content_hash=content_hash,
                    correlation_id=correlation_id,
                    reversal_of_transaction_id=reversal_of_transaction_id,
                    postings=normalized_postings,
                )

        return self.get_transaction(transaction_id_to_return)

    def _insert_and_post(
        self,
        *,
        transaction_id: str,
        effective_at_iso: str,
        description: str,
        currency: str,
        merchant_name: str | None,
        source_type: str | None,
        source_id: str | None,
        content_hash: str,
        correlation_id: str | None,
        reversal_of_transaction_id: str | None,
        postings: Sequence[PostingInput],
    ) -> str:
        account_rows: dict[str, sqlite3.Row] = {}
        for item in postings:
            row = self.connection.execute(
                "SELECT id, currency, is_active FROM accounts WHERE code = ?",
                (item.account_code,),
            ).fetchone()
            if row is None:
                raise AccountNotFound(f"unknown account: {item.account_code}")
            if row["currency"] != currency:
                raise CurrencyMismatch(
                    f"account {item.account_code} uses {row['currency']}, not {currency}"
                )
            if not row["is_active"]:
                raise InvalidTransaction(f"account is inactive: {item.account_code}")
            account_rows[item.account_code] = row
        self._check_period_lock(effective_at_iso, {row["id"] for row in account_rows.values()})

        created_at = _utc_now_iso()
        self.connection.execute(
            """
            INSERT INTO transactions(
                id, effective_at, description, currency, merchant_name,
                source_type, source_id, content_hash, correlation_id,
                reversal_of_transaction_id, status, created_at, posted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DRAFT', ?, NULL)
            """,
            (
                transaction_id,
                effective_at_iso,
                description,
                currency,
                merchant_name,
                source_type,
                source_id,
                content_hash,
                correlation_id,
                reversal_of_transaction_id,
                created_at,
            ),
        )

        for sort_order, item in enumerate(postings):
            posting_id = str(uuid4())
            self.connection.execute(
                """
                INSERT INTO postings(
                    id, transaction_id, account_id, amount_minor, memo, sort_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posting_id,
                    transaction_id,
                    account_rows[item.account_code]["id"],
                    item.amount_minor,
                    _optional_text(item.memo),
                    sort_order,
                    created_at,
                ),
            )
            for key, value in sorted(item.tags.items()):
                self.connection.execute(
                    "INSERT INTO posting_tags(posting_id, key, value) VALUES (?, ?, ?)",
                    (posting_id, key.strip(), value.strip()),
                )

        posted_at = _utc_now_iso()
        self.connection.execute(
            "UPDATE transactions SET status = 'POSTED', posted_at = ? WHERE id = ?",
            (posted_at, transaction_id),
        )
        return transaction_id

    def _check_period_lock(self, effective_at_iso: str, account_ids: set[str]) -> None:
        """Reject postings that would change a balance a completed statement already matched."""

        placeholders = ",".join("?" * len(account_ids))
        locked = self.connection.execute(
            f"""
            SELECT a.name, max(r.statement_date) AS locked_through
            FROM reconciliations r
            JOIN accounts a ON a.id = r.account_id
            WHERE r.account_id IN ({placeholders})
            GROUP BY a.id
            ORDER BY locked_through DESC
            """,
            tuple(account_ids),
        ).fetchall()
        for row in locked:
            locked_through = date.fromisoformat(row["locked_through"])
            boundary = datetime.combine(
                locked_through + timedelta(days=1), time.min, tzinfo=timezone.utc
            )
            if effective_at_iso < boundary.isoformat(timespec="microseconds"):
                raise PeriodLocked(
                    f"{row['name']} is reconciled through {locked_through.isoformat()}, so "
                    "entries dated on or before that day can't be added, edited, or deleted. "
                    "Use a later date instead."
                )

    def reverse_transaction(
        self,
        transaction_id: str,
        *,
        effective_at: datetime,
        reason: str,
    ) -> JournalTransaction:
        original = self.get_transaction(transaction_id)
        reason = _required_text(reason, "reversal reason")
        existing = self.connection.execute(
            """
            SELECT id FROM transactions
            WHERE reversal_of_transaction_id = ? AND status = 'POSTED'
            """,
            (transaction_id,),
        ).fetchone()
        if existing is not None:
            return self.get_transaction(existing["id"])

        return self._post_transaction(
            effective_at=effective_at,
            description=f"Reversal: {original.description} ({reason})",
            postings=tuple(
                PostingInput(
                    account_code=posting.account_code,
                    amount_minor=-posting.amount_minor,
                    memo=f"Reverses posting {posting.id}",
                    tags={**posting.tags, "system.reversal": "true"},
                )
                for posting in original.postings
            ),
            currency=original.currency,
            merchant_name=original.merchant_name,
            source_type="REVERSAL",
            source_id=original.id,
            correlation_id=original.correlation_id or original.id,
            reversal_of_transaction_id=original.id,
            transaction_id=None,
        )

    def replace_transaction(
        self,
        transaction_id: str,
        *,
        effective_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        merchant_name: str | None = None,
    ) -> JournalTransaction:
        """Edit a transaction without rewriting history.

        In one database transaction, the original is reversed on its own date (so it
        disappears from that period's reports) and the corrected version is posted.
        Both share the original's correlation id so the edit trail stays linked.
        """

        original = self.get_transaction(transaction_id)
        if original.reversal_of_transaction_id is not None:
            raise InvalidTransaction("a correction entry cannot be edited")
        description = _required_text(description, "description")
        replacement_postings = tuple(postings)
        self._validate_posting_inputs(replacement_postings)
        merchant_name = _optional_text(merchant_name)
        correlation_id = original.correlation_id or original.id
        effective_at_iso = _as_utc_iso(effective_at)

        reversal_postings = tuple(
            PostingInput(
                account_code=posting.account_code,
                amount_minor=-posting.amount_minor,
                memo=f"Reverses posting {posting.id}",
                tags={**posting.tags, "system.reversal": "true"},
            )
            for posting in original.postings
        )
        reversal_effective_iso = _as_utc_iso(original.effective_at)
        reversal_description = f"Edited: {original.description}"
        replacement_id = str(uuid4())
        with self._write():
            already_reversed = self.connection.execute(
                "SELECT 1 FROM transactions WHERE reversal_of_transaction_id = ?",
                (original.id,),
            ).fetchone()
            if already_reversed is not None:
                raise InvalidTransaction("this transaction was already deleted or edited")
            self._insert_and_post(
                transaction_id=str(uuid4()),
                effective_at_iso=reversal_effective_iso,
                description=reversal_description,
                currency=original.currency,
                merchant_name=original.merchant_name,
                source_type="REVERSAL",
                source_id=original.id,
                content_hash=_content_hash(
                    effective_at=reversal_effective_iso,
                    description=reversal_description,
                    currency=original.currency,
                    merchant_name=original.merchant_name,
                    source_type="REVERSAL",
                    source_id=original.id,
                    correlation_id=correlation_id,
                    reversal_of_transaction_id=original.id,
                    postings=reversal_postings,
                ),
                correlation_id=correlation_id,
                reversal_of_transaction_id=original.id,
                postings=reversal_postings,
            )
            self._insert_and_post(
                transaction_id=replacement_id,
                effective_at_iso=effective_at_iso,
                description=description,
                currency=original.currency,
                merchant_name=merchant_name,
                source_type="EDIT",
                source_id=original.id,
                content_hash=_content_hash(
                    effective_at=effective_at_iso,
                    description=description,
                    currency=original.currency,
                    merchant_name=merchant_name,
                    source_type="EDIT",
                    source_id=original.id,
                    correlation_id=correlation_id,
                    reversal_of_transaction_id=None,
                    postings=replacement_postings,
                ),
                correlation_id=correlation_id,
                reversal_of_transaction_id=None,
                postings=replacement_postings,
            )
        return self.get_transaction(replacement_id)

    def get_transaction(self, transaction_id: str) -> JournalTransaction:
        row = self.connection.execute(
            "SELECT * FROM transactions WHERE id = ? AND status = 'POSTED'",
            (transaction_id,),
        ).fetchone()
        if row is None:
            raise TransactionNotFound(f"unknown posted transaction: {transaction_id}")

        posting_rows = self.connection.execute(
            """
            SELECT p.*, a.code AS account_code
            FROM postings p JOIN accounts a ON a.id = p.account_id
            WHERE p.transaction_id = ?
            ORDER BY p.sort_order
            """,
            (transaction_id,),
        ).fetchall()
        postings: list[Posting] = []
        for posting_row in posting_rows:
            tag_rows = self.connection.execute(
                "SELECT key, value FROM posting_tags WHERE posting_id = ? ORDER BY key",
                (posting_row["id"],),
            ).fetchall()
            postings.append(
                Posting(
                    id=posting_row["id"],
                    transaction_id=transaction_id,
                    account_id=posting_row["account_id"],
                    account_code=posting_row["account_code"],
                    amount_minor=posting_row["amount_minor"],
                    memo=posting_row["memo"],
                    tags={tag["key"]: tag["value"] for tag in tag_rows},
                )
            )

        return JournalTransaction(
            id=row["id"],
            effective_at=_parse_datetime(row["effective_at"]),
            description=row["description"],
            currency=row["currency"],
            merchant_name=row["merchant_name"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            correlation_id=row["correlation_id"],
            reversal_of_transaction_id=row["reversal_of_transaction_id"],
            created_at=_parse_datetime(row["created_at"]),
            posted_at=_parse_datetime(row["posted_at"]),
            postings=tuple(postings),
        )

    def list_transactions(
        self, *, limit: int = 50, hide_corrected: bool = False
    ) -> tuple[JournalTransaction, ...]:
        """List posted transactions, newest first.

        ``hide_corrected`` leaves out reversed originals and their reversal entries,
        which is what a person expects to see after deleting or editing.
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self.connection.execute(
            f"""
            SELECT t.id FROM transactions t
            WHERE t.status = 'POSTED' {_HIDE_CORRECTED_SQL if hide_corrected else ""}
            ORDER BY t.effective_at DESC, t.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(self.get_transaction(row["id"]) for row in rows)

    def list_transactions_for_account(
        self, account_code: str, *, limit: int = 50, hide_corrected: bool = False
    ) -> tuple[JournalTransaction, ...]:
        account = self.get_account(account_code)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT t.id, t.effective_at, t.created_at
            FROM transactions t
            JOIN postings p ON p.transaction_id = t.id
            WHERE t.status = 'POSTED' AND p.account_id = ?
                {_HIDE_CORRECTED_SQL if hide_corrected else ""}
            ORDER BY t.effective_at DESC, t.created_at DESC
            LIMIT ?
            """,
            (account.id, limit),
        ).fetchall()
        return tuple(self.get_transaction(row["id"]) for row in rows)

    def trial_balance(self) -> tuple[AccountBalance, ...]:
        rows = self.connection.execute(
            """
            SELECT a.*,
                   coalesce(sum(
                       CASE WHEN t.status = 'POSTED' THEN p.amount_minor ELSE 0 END
                   ), 0) AS balance_minor
            FROM accounts a
            LEFT JOIN postings p ON p.account_id = a.id
            LEFT JOIN transactions t
                ON t.id = p.transaction_id AND t.status = 'POSTED'
            GROUP BY a.id
            ORDER BY a.code
            """
        ).fetchall()
        return tuple(
            AccountBalance(account=_account_from_row(row), balance_minor=row["balance_minor"])
            for row in rows
        )

    @staticmethod
    def _validate_posting_inputs(postings: Sequence[PostingInput]) -> None:
        if len(postings) < 2:
            raise InvalidTransaction("a transaction needs at least two postings")
        total = 0
        for posting in postings:
            if not isinstance(posting, PostingInput):
                raise TypeError("postings must contain PostingInput values")
            if not posting.account_code.strip():
                raise InvalidTransaction("posting account_code cannot be blank")
            if isinstance(posting.amount_minor, bool) or not isinstance(posting.amount_minor, int):
                raise InvalidTransaction("posting amounts must be integer minor units")
            if posting.amount_minor == 0:
                raise InvalidTransaction("zero-value postings are not allowed")
            for key, value in posting.tags.items():
                if not isinstance(key, str) or not key.strip():
                    raise InvalidTransaction("tag keys must be non-empty strings")
                if not isinstance(value, str) or not value.strip():
                    raise InvalidTransaction("tag values must be non-empty strings")
            total += posting.amount_minor
        if total != 0:
            raise InvalidTransaction(f"postings must balance to zero; difference is {total}")


_HIDE_CORRECTED_SQL = """
    AND t.reversal_of_transaction_id IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM transactions r
        WHERE r.reversal_of_transaction_id = t.id AND r.status = 'POSTED'
    )
"""


def _content_hash(**content: object) -> str:
    postings = content.pop("postings")
    assert isinstance(postings, Iterable)
    serializable = {
        **content,
        "postings": [
            {
                "account_code": item.account_code,
                "amount_minor": item.amount_minor,
                "memo": _optional_text(item.memo),
                "tags": dict(sorted(item.tags.items())),
            }
            for item in postings
        ],
    }
    canonical = json.dumps(serializable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} cannot be blank")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("optional text values must be strings")
    stripped = value.strip()
    return stripped or None


def _validate_currency(value: str) -> str:
    if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
        raise ValueError("currency must be a three-letter uppercase ISO-style code")
    return value


def _as_utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("effective_at must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _account_from_row(row: sqlite3.Row) -> Account:
    return Account(
        id=row["id"],
        code=row["code"],
        name=row["name"],
        type=AccountType(row["type"]),
        currency=row["currency"],
        is_active=bool(row["is_active"]),
        created_at=_parse_datetime(row["created_at"]),
    )


def _snapshot_from_row(row: sqlite3.Row) -> BalanceSnapshot:
    return BalanceSnapshot(
        id=row["id"],
        account_id=row["account_id"],
        observed_at=_parse_datetime(row["observed_at"]),
        ledger_balance_minor=row["ledger_balance_minor"],
        available_to_spend_minor=row["available_to_spend_minor"],
        source=row["source"],
    )


def _default_kind_for_account_type(account_type: AccountType) -> AccountKind:
    if account_type is AccountType.ASSET:
        return AccountKind.CUSTOM_ASSET
    if account_type is AccountType.LIABILITY:
        return AccountKind.CUSTOM_LIABILITY
    return AccountKind.CATEGORY


def _account_type_for_kind(kind: AccountKind) -> AccountType:
    if kind in {
        AccountKind.CHECKING,
        AccountKind.SAVINGS,
        AccountKind.CASH,
        AccountKind.INVESTMENT,
        AccountKind.GIFT_CARD,
        AccountKind.RECEIVABLE,
        AccountKind.CUSTOM_ASSET,
    }:
        return AccountType.ASSET
    if kind in {
        AccountKind.CREDIT_CARD,
        AccountKind.LOAN,
        AccountKind.CUSTOM_LIABILITY,
    }:
        return AccountType.LIABILITY
    raise ValueError(f"account kind is not a balance-sheet account: {kind.value}")
