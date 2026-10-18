"""Non-AI CSV statement staging, matching, and reviewed ledger posting."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import PurePath
from uuid import uuid4

from .errors import (
    IdempotencyConflict,
    StatementImportNotFound,
    StatementImportStateError,
)
from .ledger import Ledger, _required_text, _utc_now_iso
from .models import (
    Account,
    AccountType,
    AmountConvention,
    BankTransactionStatus,
    ImportRowStatus,
    PostingInput,
    StatementImportBatch,
    StatementImportRow,
)
from .rules import RuleService

_DATE_HEADERS = ("date", "transactiondate", "posteddate", "postingdate")
_DESCRIPTION_HEADERS = ("description", "merchant", "name", "memo", "details")
_AMOUNT_HEADERS = ("amount", "transactionamount")
_DEBIT_HEADERS = ("debit", "withdrawal", "charge")
_CREDIT_HEADERS = ("credit", "deposit", "payment")
_STATUS_HEADERS = ("status", "state")
_ID_HEADERS = ("transactionid", "id", "reference", "referencenumber")


class StatementImportService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def import_csv(
        self,
        *,
        account_code: str,
        filename: str,
        csv_text: str,
        amount_convention: AmountConvention | str = AmountConvention.ACCOUNT_SIGNED,
    ) -> StatementImportBatch:
        account = self.ledger.get_account(account_code)
        if account.type not in {AccountType.ASSET, AccountType.LIABILITY}:
            raise ValueError("statements can only be imported into asset or liability accounts")
        filename = PurePath(_required_text(filename, "filename")).name
        if not isinstance(csv_text, str) or not csv_text.strip():
            raise ValueError("csv_text cannot be blank")
        convention = AmountConvention(amount_convention)
        content_hash = hashlib.sha256(csv_text.encode("utf-8-sig")).hexdigest()
        existing = self.ledger.connection.execute(
            """
            SELECT id FROM statement_import_batches
            WHERE account_id = ? AND content_hash = ?
            """,
            (account.id, content_hash),
        ).fetchone()
        if existing is not None:
            return self.get_batch(existing["id"])

        parsed_rows = _parse_csv(csv_text, convention)
        rules = RuleService(self.ledger)
        batch_id = str(uuid4())
        created_at = _utc_now_iso()
        staged: list[dict[str, object]] = []
        # Overlapping statement exports repeat rows already staged. The nth copy of a
        # row in this file is a duplicate only if the account already has n such rows,
        # so two identical same-day charges in one file still both import.
        seen: dict[tuple[str, str], int] = {}
        for row_number, parsed in enumerate(parsed_rows, 1):
            parsed["row_hash"] = _row_hash(parsed)
            key = (
                ("external_id", parsed["external_id"])
                if parsed["external_id"]
                else ("row_hash", parsed["row_hash"])
            )
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= self._count_staged(account, *key):
                continue
            match_id = None
            workflow_status = (
                ImportRowStatus.PENDING
                if parsed["bank_status"] is BankTransactionStatus.PENDING
                else ImportRowStatus.NEEDS_REVIEW
            )
            if workflow_status is ImportRowStatus.NEEDS_REVIEW:
                match_id = self._find_exact_ledger_match(
                    account,
                    parsed["effective_date"],
                    parsed["amount_minor"],
                )
                if match_id is not None:
                    workflow_status = ImportRowStatus.MATCHED
            suggested_code = rules.suggest(
                parsed["description"], exclude_account_id=account.id
            ) or _default_suggestion(account, parsed["amount_minor"])
            suggested = (
                self.ledger.connection.execute(
                    "SELECT id FROM accounts WHERE code = ?", (suggested_code,)
                ).fetchone()
                if suggested_code
                else None
            )
            staged.append(
                {
                    **parsed,
                    "row_number": row_number,
                    "workflow_status": workflow_status,
                    "match_id": match_id,
                    "suggested_id": suggested["id"] if suggested else None,
                }
            )

        with self.ledger._write():
            self.ledger.connection.execute(
                """
                INSERT INTO statement_import_batches(
                    id, account_id, filename, amount_convention, content_hash,
                    row_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    account.id,
                    filename,
                    convention.value,
                    content_hash,
                    len(staged),
                    created_at,
                ),
            )
            for item in staged:
                self.ledger.connection.execute(
                    """
                    INSERT INTO statement_import_rows(
                        id, batch_id, account_id, row_number, external_id, row_hash,
                        effective_date, description, account_amount_minor, bank_status,
                        workflow_status, matched_transaction_id, suggested_account_id,
                        raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        batch_id,
                        account.id,
                        item["row_number"],
                        item["external_id"],
                        item["row_hash"],
                        item["effective_date"].isoformat(),
                        item["description"],
                        item["amount_minor"],
                        item["bank_status"].value,
                        item["workflow_status"].value,
                        item["match_id"],
                        item["suggested_id"],
                        json.dumps(item["raw"], sort_keys=True),
                        created_at,
                    ),
                )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> StatementImportBatch:
        row = self.ledger.connection.execute(
            """
            SELECT b.*, a.code AS account_code
            FROM statement_import_batches b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.id = ?
            """,
            (batch_id,),
        ).fetchone()
        if row is None:
            raise StatementImportNotFound(f"unknown statement import: {batch_id}")
        account = self.ledger.get_account(row["account_code"])
        rows = self.ledger.connection.execute(
            """
            SELECT r.*, suggested.code AS suggested_account_code
            FROM statement_import_rows r
            LEFT JOIN accounts suggested ON suggested.id = r.suggested_account_id
            WHERE r.batch_id = ?
            ORDER BY r.row_number
            """,
            (batch_id,),
        ).fetchall()
        return StatementImportBatch(
            id=row["id"],
            account=account,
            filename=row["filename"],
            amount_convention=AmountConvention(row["amount_convention"]),
            row_count=row["row_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            rows=tuple(_row_from_record(item, account) for item in rows),
        )

    def review_queue(self, *, limit: int = 200) -> tuple[StatementImportRow, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self.ledger.connection.execute(
            """
            SELECT r.*, a.code AS account_code,
                   suggested.code AS suggested_account_code
            FROM statement_import_rows r
            JOIN accounts a ON a.id = r.account_id
            LEFT JOIN accounts suggested ON suggested.id = r.suggested_account_id
            WHERE r.workflow_status = 'NEEDS_REVIEW'
            ORDER BY r.effective_date, r.created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(
            _row_from_record(row, self.ledger.get_account(row["account_code"]))
            for row in rows
        )

    def post_row(
        self,
        row_id: str,
        *,
        category_code: str,
        reviewed_by: str,
    ) -> StatementImportRow:
        row, account = self._get_row_record(row_id)
        status = ImportRowStatus(row["workflow_status"])
        if status is ImportRowStatus.POSTED:
            return self.get_row(row_id)
        if status is not ImportRowStatus.NEEDS_REVIEW:
            raise StatementImportStateError(
                f"only cleared rows needing review can be posted; row is {status.value}"
            )
        category = self.ledger.get_account(category_code)
        amount = row["account_amount_minor"]
        if category.id == account.id:
            raise ValueError("statement account and counter-account must be different")
        if category.type is AccountType.EQUITY:
            raise ValueError("statement rows cannot be posted directly against equity")
        if category.currency != account.currency:
            raise ValueError("category and statement account currencies must match")
        reviewed_by = _required_text(reviewed_by, "reviewed_by")
        effective_at = datetime.combine(
            date.fromisoformat(row["effective_date"]), time(hour=12), tzinfo=timezone.utc
        )
        transaction = self.ledger.post_transaction(
            effective_at=effective_at,
            description=row["description"],
            currency=account.currency,
            source_type="STATEMENT_IMPORT",
            source_id=row["id"],
            postings=(
                PostingInput(account.code, amount),
                PostingInput(category.code, -amount),
            ),
        )
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                UPDATE statement_import_rows
                SET workflow_status = 'POSTED', posted_transaction_id = ?,
                    reviewed_by = ?, reviewed_at = ?
                WHERE id = ? AND workflow_status = 'NEEDS_REVIEW'
                """,
                (transaction.id, reviewed_by, _utc_now_iso(), row_id),
            )
        RuleService(self.ledger).learn(row["description"], category.code)
        return self.get_row(row_id)

    def ignore_row(self, row_id: str, *, reviewed_by: str) -> StatementImportRow:
        row, _account = self._get_row_record(row_id)
        status = ImportRowStatus(row["workflow_status"])
        if status is ImportRowStatus.IGNORED:
            return self.get_row(row_id)
        if status not in {ImportRowStatus.NEEDS_REVIEW, ImportRowStatus.PENDING}:
            raise StatementImportStateError(f"row cannot be ignored from {status.value}")
        reviewed_by = _required_text(reviewed_by, "reviewed_by")
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                UPDATE statement_import_rows
                SET workflow_status = 'IGNORED', reviewed_by = ?, reviewed_at = ?
                WHERE id = ?
                """,
                (reviewed_by, _utc_now_iso(), row_id),
            )
        return self.get_row(row_id)

    def get_row(self, row_id: str) -> StatementImportRow:
        row, account = self._get_row_record(row_id)
        return _row_from_record(row, account)

    def _get_row_record(self, row_id: str) -> tuple[object, Account]:
        row = self.ledger.connection.execute(
            """
            SELECT r.*, a.code AS account_code,
                   suggested.code AS suggested_account_code
            FROM statement_import_rows r
            JOIN accounts a ON a.id = r.account_id
            LEFT JOIN accounts suggested ON suggested.id = r.suggested_account_id
            WHERE r.id = ?
            """,
            (row_id,),
        ).fetchone()
        if row is None:
            raise StatementImportNotFound(f"unknown statement row: {row_id}")
        return row, self.ledger.get_account(row["account_code"])

    def _count_staged(self, account: Account, column: str, value: str) -> int:
        assert column in {"external_id", "row_hash"}
        row = self.ledger.connection.execute(
            f"""
            SELECT count(*) AS n FROM statement_import_rows
            WHERE account_id = ? AND {column} = ? AND workflow_status <> 'IGNORED'
            """,
            (account.id, value),
        ).fetchone()
        return row["n"]

    def _find_exact_ledger_match(
        self, account: Account, effective_date: date, amount_minor: int
    ) -> str | None:
        start = datetime.combine(
            effective_date - timedelta(days=2), time.min, tzinfo=timezone.utc
        )
        end = datetime.combine(
            effective_date + timedelta(days=3), time.min, tzinfo=timezone.utc
        )
        rows = self.ledger.connection.execute(
            """
            SELECT DISTINCT t.id
            FROM transactions t
            JOIN postings p ON p.transaction_id = t.id
            WHERE t.status = 'POSTED'
              AND p.account_id = ?
              AND p.amount_minor = ?
              AND t.effective_at >= ?
              AND t.effective_at < ?
              AND (t.source_type IS NULL OR t.source_type <> 'OPENING_BALANCE')
              AND NOT EXISTS (
                  SELECT 1 FROM transactions reversal
                  WHERE reversal.reversal_of_transaction_id = t.id
                    AND reversal.status = 'POSTED'
              )
            LIMIT 2
            """,
            (
                account.id,
                amount_minor,
                start.isoformat(timespec="microseconds"),
                end.isoformat(timespec="microseconds"),
            ),
        ).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None


def _parse_csv(
    csv_text: str, convention: AmountConvention
) -> list[dict[str, object]]:
    sample = csv_text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), dialect=dialect)
    if reader.fieldnames is None:
        raise ValueError("CSV must contain a header row")
    normalized = {_normalize_header(name): name for name in reader.fieldnames if name}
    date_header = _find_header(normalized, _DATE_HEADERS, required=True)
    description_header = _find_header(normalized, _DESCRIPTION_HEADERS, required=True)
    amount_header = _find_header(normalized, _AMOUNT_HEADERS, required=False)
    debit_header = _find_header(normalized, _DEBIT_HEADERS, required=False)
    credit_header = _find_header(normalized, _CREDIT_HEADERS, required=False)
    if amount_header is None and debit_header is None and credit_header is None:
        raise ValueError("CSV needs Amount or Debit/Credit columns")
    status_header = _find_header(normalized, _STATUS_HEADERS, required=False)
    id_header = _find_header(normalized, _ID_HEADERS, required=False)

    parsed: list[dict[str, object]] = []
    for line_number, raw in enumerate(reader, 2):
        if not any(str(value or "").strip() for value in raw.values()):
            continue
        try:
            effective_date = _parse_date(str(raw.get(date_header) or ""))
            description = _required_text(
                str(raw.get(description_header) or ""), "description"
            )
            if amount_header is not None:
                amount_minor = _parse_money(str(raw.get(amount_header) or ""))
                if convention is AmountConvention.CHARGES_POSITIVE:
                    amount_minor = -amount_minor
            else:
                debit = _parse_money(str(raw.get(debit_header) or ""), blank_zero=True)
                credit = _parse_money(str(raw.get(credit_header) or ""), blank_zero=True)
                # Debit/Credit columns hold magnitudes; the column gives the direction.
                amount_minor = abs(credit) - abs(debit)
            if amount_minor == 0:
                raise ValueError("zero-value rows are not importable")
        except ValueError as exc:
            raise ValueError(f"CSV line {line_number}: {exc}") from exc
        status_text = str(raw.get(status_header) or "cleared").strip().lower()
        bank_status = (
            BankTransactionStatus.PENDING
            if status_text in {"pending", "authorized", "processing"}
            else BankTransactionStatus.CLEARED
        )
        external_id = str(raw.get(id_header) or "").strip() or None
        parsed.append(
            {
                "effective_date": effective_date,
                "description": description,
                "amount_minor": amount_minor,
                "bank_status": bank_status,
                "external_id": external_id,
                "raw": {str(key): str(value or "") for key, value in raw.items()},
            }
        )
    if not parsed:
        raise ValueError("CSV does not contain any transaction rows")
    if len(parsed) > 5_000:
        raise ValueError("one import is limited to 5,000 rows")
    return parsed


def _row_hash(parsed: dict[str, object]) -> str:
    content = f"{parsed['effective_date']}|{parsed['description']}|{parsed['amount_minor']}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _default_suggestion(account: Account, amount_minor: int) -> str | None:
    if account.type is AccountType.ASSET:
        return "Expenses:Other" if amount_minor < 0 else "Income:Other"
    if account.type is AccountType.LIABILITY and amount_minor < 0:
        return "Expenses:Other"
    return None


def _row_from_record(row: object, account: Account) -> StatementImportRow:
    return StatementImportRow(
        id=row["id"],
        batch_id=row["batch_id"],
        account=account,
        row_number=row["row_number"],
        external_id=row["external_id"],
        effective_date=date.fromisoformat(row["effective_date"]),
        description=row["description"],
        account_amount_minor=row["account_amount_minor"],
        bank_status=BankTransactionStatus(row["bank_status"]),
        workflow_status=ImportRowStatus(row["workflow_status"]),
        matched_transaction_id=row["matched_transaction_id"],
        suggested_account_code=row["suggested_account_code"],
        posted_transaction_id=row["posted_transaction_id"],
        reviewed_by=row["reviewed_by"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _find_header(
    normalized: dict[str, str], candidates: tuple[str, ...], *, required: bool
) -> str | None:
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    if required:
        raise ValueError(f"CSV is missing a supported {candidates[0]} column")
    return None


def _parse_date(value: str) -> date:
    value = value.strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    raise ValueError(f"unsupported date: {value!r}")


_MAX_AMOUNT_MINOR = 100_000_000_000_000


def _parse_money(value: str, *, blank_zero: bool = False) -> int:
    cleaned = value.strip()
    if not cleaned and blank_zero:
        return 0
    negate = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()").replace("$", "").replace(",", "").strip()
    suffix = re.search(r"\s*(CR|DR)$", cleaned, flags=re.IGNORECASE)
    if suffix is not None:
        cleaned = cleaned[: suffix.start()]
    if cleaned.endswith("-") and not cleaned.startswith(("-", "+")):
        negate = True
        cleaned = cleaned[:-1].strip()
    # Plain decimals only: Decimal() would also accept "1e30", "NaN", and "Infinity".
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", cleaned):
        raise ValueError(f"invalid amount: {value!r}")
    amount = Decimal(cleaned)
    if negate:
        amount = -amount
    if suffix is not None:
        # DR/CR state the direction outright, whatever sign the number carries.
        amount = -abs(amount) if suffix.group(1).upper() == "DR" else abs(amount)
    if abs(amount) * 100 > _MAX_AMOUNT_MINOR:
        raise ValueError(f"amount is too large: {value!r}")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
