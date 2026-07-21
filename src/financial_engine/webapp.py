"""Dependency-free local web application for the non-AI financial platform."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
from datetime import date, datetime, time, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from .analytics import AnalyticsService
from .bootstrap import ensure_default_categories
from .budgets import BudgetService, validate_month
from .errors import LedgerError
from .ledger import Ledger
from .models import AccountKind, AccountType, PostingInput
from .proposals import ProposalService
from .reconciliation import ReconciliationService
from .statement_imports import StatementImportService

STATIC_ROOT = Path(__file__).with_name("static")
ALLOWED_STATIC = {"index.html", "styles.css", "app.js"}


class FinancialAppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], database: Path) -> None:
        self.database = database
        super().__init__(address, FinancialRequestHandler)


class FinancialRequestHandler(BaseHTTPRequestHandler):
    server: FinancialAppServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/healthz":
                with Ledger(self.server.database) as ledger:
                    ledger.connection.execute("SELECT 1").fetchone()
                self._send_json({"status": "ok"})
                return
            if parsed.path == "/api/overview":
                month = parse_qs(parsed.query).get("month", [_current_month()])[0]
                self._send_json(self._overview(month))
                return
            if parsed.path == "/api/review":
                with Ledger(self.server.database) as ledger:
                    proposals = ProposalService(ledger).review_queue()
                    import_rows = StatementImportService(ledger).review_queue()
                    self._send_json(
                        {
                            "proposals": [_proposal_json(item) for item in proposals],
                            "import_rows": [_import_row_json(item) for item in import_rows],
                        }
                    )
                return
            import_match = re.fullmatch(r"/api/imports/([^/]+)", parsed.path)
            if import_match:
                with Ledger(self.server.database) as ledger:
                    batch = StatementImportService(ledger).get_batch(import_match.group(1))
                    self._send_json({"import": _import_batch_json(batch)})
                return
            account_match = re.fullmatch(r"/api/accounts/(.+)", parsed.path)
            if account_match:
                self._send_json(self._account_detail(unquote(account_match.group(1))))
                return
            transaction_match = re.fullmatch(r"/api/transactions/([^/]+)", parsed.path)
            if transaction_match:
                self._send_json(self._transaction_detail(transaction_match.group(1)))
                return
            if parsed.path == "/api/reconciliations":
                account_code = parse_qs(parsed.query).get("account_code", [""])[0]
                self._send_json(self._reconciliation_history(account_code))
                return
            filename = "index.html" if parsed.path in {"", "/"} else parsed.path.lstrip("/")
            if filename not in ALLOWED_STATIC:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_static(filename)
        except Exception as exc:  # pragma: no cover - final HTTP safety net
            self._send_error_json(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/accounts":
                self._create_account(payload)
                return
            if parsed.path == "/api/transactions":
                self._create_transaction(payload)
                return
            if parsed.path == "/api/budgets":
                self._set_budget(payload)
                return
            if parsed.path == "/api/imports":
                self._create_statement_import(payload)
                return
            if parsed.path == "/api/reconciliations/preview":
                self._preview_reconciliation(payload)
                return
            if parsed.path == "/api/reconciliations/complete":
                self._complete_reconciliation(payload)
                return
            reverse_match = re.fullmatch(r"/api/transactions/([^/]+)/reverse", parsed.path)
            if reverse_match:
                self._reverse_transaction(reverse_match.group(1), payload)
                return
            import_row_match = re.fullmatch(
                r"/api/import-rows/([^/]+)/(post|ignore)", parsed.path
            )
            if import_row_match:
                self._review_import_row(
                    import_row_match.group(1), import_row_match.group(2), payload
                )
                return
            match = re.fullmatch(r"/api/proposals/([^/]+)/(approve|reject)", parsed.path)
            if match:
                self._review_proposal(match.group(1), match.group(2), payload)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_error_json(exc)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _overview(self, month: str) -> dict[str, Any]:
        validate_month(month)
        with Ledger(self.server.database) as ledger:
            accounts = ledger.list_account_overviews()
            summary = AnalyticsService(ledger).monthly_spending(month=month)
            transactions = ledger.list_transactions(limit=12)
            review_count = len(ProposalService(ledger).review_queue(limit=500)) + len(
                StatementImportService(ledger).review_queue(limit=500)
            )
            category_accounts = tuple(
                account
                for account in ledger.list_accounts()
                if account.type is AccountType.EXPENSE
            )
            income_accounts = tuple(
                account
                for account in ledger.list_accounts()
                if account.type is AccountType.INCOME
            )
            balance_accounts = {item.account.code: item for item in accounts}
            spendable_kinds = {
                AccountKind.CHECKING,
                AccountKind.SAVINGS,
                AccountKind.CASH,
                AccountKind.GIFT_CARD,
            }
            total_available = sum(
                item.available_balance_minor or 0
                for item in accounts
                if item.kind in spendable_kinds
            )
            return {
                "month": month,
                "currency": summary.currency,
                "total_available_minor": total_available,
                "accounts": [_account_json(item) for item in accounts],
                "spending": _summary_json(summary),
                "transactions": [
                    _transaction_json(ledger, item, balance_accounts) for item in transactions
                ],
                "expense_categories": [
                    {"code": item.code, "name": item.name} for item in category_accounts
                ],
                "income_categories": [
                    {"code": item.code, "name": item.name} for item in income_accounts
                ],
                "review_count": review_count,
                "ai": {"enabled": False, "status": "Not configured"},
            }

    def _account_detail(self, account_code: str) -> dict[str, Any]:
        with Ledger(self.server.database) as ledger:
            overview = ledger.get_account_overview(account_code)
            all_overviews = {item.account.code: item for item in ledger.list_account_overviews()}
            transactions = ledger.list_transactions_for_account(account_code, limit=30)
            reconciliations = ReconciliationService(ledger).history(account_code)
            return {
                "account": _account_json(overview),
                "transactions": [
                    _transaction_json(
                        ledger, item, all_overviews, focus_account_code=account_code
                    )
                    for item in transactions
                ],
                "reconciliations": [
                    _reconciliation_json(item) for item in reconciliations
                ],
            }

    def _transaction_detail(self, transaction_id: str) -> dict[str, Any]:
        with Ledger(self.server.database) as ledger:
            transaction = ledger.get_transaction(transaction_id)
            reversal = ledger.connection.execute(
                """
                SELECT id FROM transactions
                WHERE reversal_of_transaction_id = ? AND status = 'POSTED'
                """,
                (transaction.id,),
            ).fetchone()
            postings = []
            for posting in transaction.postings:
                account = ledger.get_account(posting.account_code)
                postings.append(
                    {
                        "id": posting.id,
                        "account_code": posting.account_code,
                        "account_name": account.name,
                        "account_type": account.type.value,
                        "amount_minor": posting.amount_minor,
                        "memo": posting.memo,
                        "tags": dict(posting.tags),
                    }
                )
            return {
                "id": transaction.id,
                "description": transaction.description,
                "merchant_name": transaction.merchant_name,
                "effective_at": transaction.effective_at.isoformat(),
                "currency": transaction.currency,
                "source_type": transaction.source_type,
                "source_id": transaction.source_id,
                "correlation_id": transaction.correlation_id,
                "reversal_of_transaction_id": transaction.reversal_of_transaction_id,
                "reversed_by_transaction_id": reversal["id"] if reversal else None,
                "postings": postings,
            }

    def _reconciliation_history(self, account_code: str) -> dict[str, Any]:
        if not account_code:
            raise ValueError("account_code is required")
        with Ledger(self.server.database) as ledger:
            records = ReconciliationService(ledger).history(account_code)
            return {"reconciliations": [_reconciliation_json(item) for item in records]}

    def _create_account(self, payload: dict[str, Any]) -> None:
        name = _string(payload, "name")
        kind = AccountKind(_string(payload, "kind"))
        opening = _integer(payload, "opening_balance_minor", minimum=0)
        credit_limit = _optional_integer(payload.get("credit_limit_minor"), minimum=0)
        balance_as_of = _date_at_noon(_string(payload, "balance_as_of"))
        with Ledger(self.server.database) as ledger:
            code = _unique_account_code(ledger, name, kind)
            overview = ledger.onboard_account(
                code=code,
                name=name,
                kind=kind,
                opening_balance_minor=opening,
                credit_limit_minor=credit_limit,
                balance_as_of=balance_as_of,
            )
            self._send_json({"account": _account_json(overview)}, status=HTTPStatus.CREATED)

    def _create_transaction(self, payload: dict[str, Any]) -> None:
        transaction_kind = _string(payload, "kind").lower()
        amount = _integer(payload, "amount_minor", minimum=1)
        effective_at = _date_at_noon(_string(payload, "effective_at"))
        description = _string(payload, "description")
        request_id = str(payload.get("request_id") or uuid4())
        with Ledger(self.server.database) as ledger:
            if transaction_kind == "expense":
                account_code = _string(payload, "account_code")
                category_code = _string(payload, "category_code")
                postings = (
                    PostingInput(category_code, amount),
                    PostingInput(account_code, -amount),
                )
            elif transaction_kind == "income":
                account_code = _string(payload, "account_code")
                postings = (
                    PostingInput(account_code, amount),
                    PostingInput("Income:Other", -amount),
                )
            elif transaction_kind == "transfer":
                from_code = _string(payload, "from_account_code")
                to_code = _string(payload, "to_account_code")
                if from_code == to_code:
                    raise ValueError("transfer accounts must be different")
                postings = (PostingInput(from_code, -amount), PostingInput(to_code, amount))
            else:
                raise ValueError("transaction kind must be expense, income, or transfer")
            transaction = ledger.post_transaction(
                effective_at=effective_at,
                description=description,
                merchant_name=str(payload.get("merchant_name") or "").strip() or None,
                source_type="WEB_UI",
                source_id=request_id,
                postings=postings,
            )
            self._send_json({"transaction_id": transaction.id}, status=HTTPStatus.CREATED)

    def _set_budget(self, payload: dict[str, Any]) -> None:
        month = _string(payload, "month")
        raw_allocations = payload.get("allocations")
        if not isinstance(raw_allocations, dict):
            raise ValueError("allocations must be an object keyed by category code")
        allocations = {
            str(code): _optional_integer(value, minimum=0) or 0
            for code, value in raw_allocations.items()
            if value is not None
        }
        with Ledger(self.server.database) as ledger:
            budget = BudgetService(ledger).set_monthly_budget(
                month=month, allocations=allocations
            )
            self._send_json(
                {"budget_id": budget.id, "month": budget.month}, status=HTTPStatus.CREATED
            )

    def _create_statement_import(self, payload: dict[str, Any]) -> None:
        with Ledger(self.server.database) as ledger:
            batch = StatementImportService(ledger).import_csv(
                account_code=_string(payload, "account_code"),
                filename=_string(payload, "filename"),
                csv_text=_string(payload, "csv_text"),
                amount_convention=_string(payload, "amount_convention"),
            )
            self._send_json(
                {"import": _import_batch_json(batch)}, status=HTTPStatus.CREATED
            )

    def _review_import_row(
        self, row_id: str, action: str, payload: dict[str, Any]
    ) -> None:
        with Ledger(self.server.database) as ledger:
            imports = StatementImportService(ledger)
            if action == "post":
                row = imports.post_row(
                    row_id,
                    category_code=_string(payload, "category_code"),
                    reviewed_by=str(payload.get("reviewed_by") or "local-user"),
                )
            else:
                row = imports.ignore_row(
                    row_id,
                    reviewed_by=str(payload.get("reviewed_by") or "local-user"),
                )
            self._send_json({"row": _import_row_json(row)})

    def _reverse_transaction(self, transaction_id: str, payload: dict[str, Any]) -> None:
        effective_at = _date_at_noon(str(payload.get("effective_at") or _today()))
        reason = _string(payload, "reason")
        with Ledger(self.server.database) as ledger:
            transaction = ledger.reverse_transaction(
                transaction_id, effective_at=effective_at, reason=reason
            )
            self._send_json({"transaction_id": transaction.id}, status=HTTPStatus.CREATED)

    def _preview_reconciliation(self, payload: dict[str, Any]) -> None:
        with Ledger(self.server.database) as ledger:
            preview = ReconciliationService(ledger).preview(
                account_code=_string(payload, "account_code"),
                statement_date=_date_only(_string(payload, "statement_date")),
                statement_balance_minor=_signed_integer(payload, "statement_balance_minor"),
            )
            self._send_json(_reconciliation_preview_json(preview))

    def _complete_reconciliation(self, payload: dict[str, Any]) -> None:
        with Ledger(self.server.database) as ledger:
            record = ReconciliationService(ledger).complete(
                account_code=_string(payload, "account_code"),
                statement_date=_date_only(_string(payload, "statement_date")),
                statement_balance_minor=_signed_integer(payload, "statement_balance_minor"),
                completed_by=str(payload.get("completed_by") or "local-user"),
            )
            self._send_json(
                {"reconciliation": _reconciliation_json(record)},
                status=HTTPStatus.CREATED,
            )

    def _review_proposal(
        self, proposal_id: str, action: str, payload: dict[str, Any]
    ) -> None:
        with Ledger(self.server.database) as ledger:
            proposals = ProposalService(ledger)
            if action == "approve":
                transaction = proposals.approve(
                    proposal_id,
                    reviewed_by=str(payload.get("reviewed_by") or "local-user"),
                    note=str(payload.get("note") or "").strip() or None,
                )
                self._send_json({"transaction_id": transaction.id})
            else:
                proposal = proposals.reject(
                    proposal_id,
                    reviewed_by=str(payload.get("reviewed_by") or "local-user"),
                    note=_string(payload, "note"),
                )
                self._send_json({"proposal": _proposal_json(proposal)})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if not 0 < length <= 5_000_000:
            raise ValueError("request body must be between 1 byte and 5 MB")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value

    def _send_json(self, payload: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _send_error_json(self, exc: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, (ValueError, LedgerError)) else HTTPStatus.INTERNAL_SERVER_ERROR
        message = str(exc) if status is HTTPStatus.BAD_REQUEST else "An unexpected error occurred"
        self._send_json({"error": message}, status=status)

    def _send_static(self, filename: str) -> None:
        content = (STATIC_ROOT / filename).read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(content)


def prepare_database(database: Path) -> None:
    with Ledger(database) as ledger:
        ledger.initialize()
        ensure_default_categories(ledger)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Financial Engine web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--db", default="financial_engine.db", type=Path)
    args = parser.parse_args()
    database = args.db.resolve()
    prepare_database(database)
    server = FinancialAppServer((args.host, args.port), database)
    print(f"Financial Engine: http://{args.host}:{args.port}")
    print(f"Database: {database}")
    print("AI integration: disabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server")
    finally:
        server.server_close()


def _account_json(item: object) -> dict[str, Any]:
    return {
        "code": item.account.code,
        "name": item.account.name,
        "kind": item.kind.value,
        "account_type": item.account.type.value,
        "currency": item.account.currency,
        "ledger_balance_minor": item.ledger_balance_minor,
        "display_balance_minor": item.display_balance_minor,
        "available_balance_minor": item.available_balance_minor,
        "credit_limit_minor": item.credit_limit_minor,
        "balance_source": item.balance_source,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
    }


def _summary_json(summary: object) -> dict[str, Any]:
    return {
        "spent_minor": summary.spent_minor,
        "average_minor": summary.average_minor,
        "budget_minor": summary.budget_minor,
        "remaining_minor": summary.remaining_minor,
        "categories": [
            {
                "code": item.account_code,
                "name": item.account_name,
                "spent_minor": item.spent_minor,
                "average_minor": item.average_minor,
                "budget_minor": item.budget_minor,
                "remaining_minor": item.remaining_minor,
                "is_flexible": item.is_flexible,
            }
            for item in summary.categories
        ],
        "insights": [
            {
                "id": item.id,
                "kind": item.kind,
                "severity": item.severity,
                "title": item.title,
                "explanation": item.explanation,
                "account_code": item.account_code,
                "potential_savings_minor": item.potential_savings_minor,
            }
            for item in summary.insights
        ],
    }


def _transaction_json(
    ledger: Ledger,
    transaction: object,
    balance_accounts: dict[str, object],
    focus_account_code: str | None = None,
) -> dict[str, Any]:
    display_amount = 0
    category = None
    account_name = None
    for posting in transaction.postings:
        account = ledger.get_account(posting.account_code)
        if account.type is AccountType.EXPENSE:
            display_amount -= posting.amount_minor
            category = account.name
        elif account.type is AccountType.INCOME:
            display_amount -= posting.amount_minor
            category = account.name
        elif posting.account_code in balance_accounts and account_name is None:
            account_name = balance_accounts[posting.account_code].account.name
    if display_amount == 0:
        if focus_account_code is not None:
            focused = next(
                (
                    posting
                    for posting in transaction.postings
                    if posting.account_code == focus_account_code
                ),
                None,
            )
            if focused is not None:
                display_amount = focused.amount_minor
        for posting in transaction.postings:
            if display_amount != 0:
                break
            if posting.account_code in balance_accounts:
                display_amount = posting.amount_minor
                break
    return {
        "id": transaction.id,
        "description": transaction.description,
        "merchant_name": transaction.merchant_name,
        "effective_at": transaction.effective_at.isoformat(),
        "display_amount_minor": display_amount,
        "category": category,
        "account_name": account_name,
        "is_reversal": transaction.reversal_of_transaction_id is not None,
    }


def _proposal_json(item: object) -> dict[str, Any]:
    return {
        "id": item.id,
        "source_type": item.source_type,
        "status": item.status.value,
        "confidence": item.confidence,
        "description": item.description,
        "merchant_name": item.merchant_name,
        "effective_at": item.effective_at.isoformat(),
        "currency": item.currency,
        "postings": [
            {
                "account_code": posting.account_code,
                "amount_minor": posting.amount_minor,
                "tags": dict(posting.tags),
            }
            for posting in item.postings
        ],
        "evidence": dict(item.evidence),
    }


def _reconciliation_preview_json(item: object) -> dict[str, Any]:
    return {
        "account_code": item.account.code,
        "account_name": item.account.name,
        "statement_date": item.statement_date.isoformat(),
        "statement_balance_minor": item.statement_balance_minor,
        "ledger_balance_minor": item.ledger_balance_minor,
        "difference_minor": item.difference_minor,
        "balanced": item.difference_minor == 0,
    }


def _reconciliation_json(item: object) -> dict[str, Any]:
    return {
        "id": item.id,
        "account_code": item.account.code,
        "statement_date": item.statement_date.isoformat(),
        "statement_balance_minor": item.statement_balance_minor,
        "ledger_balance_minor": item.ledger_balance_minor,
        "completed_by": item.completed_by,
        "created_at": item.created_at.isoformat(),
    }


def _import_batch_json(item: object) -> dict[str, Any]:
    return {
        "id": item.id,
        "account_code": item.account.code,
        "account_name": item.account.name,
        "filename": item.filename,
        "amount_convention": item.amount_convention.value,
        "row_count": item.row_count,
        "created_at": item.created_at.isoformat(),
        "rows": [_import_row_json(row) for row in item.rows],
    }


def _import_row_json(item: object) -> dict[str, Any]:
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "account_code": item.account.code,
        "account_name": item.account.name,
        "currency": item.account.currency,
        "row_number": item.row_number,
        "external_id": item.external_id,
        "effective_date": item.effective_date.isoformat(),
        "description": item.description,
        "account_amount_minor": item.account_amount_minor,
        "direction": "inflow" if item.account_amount_minor > 0 else "outflow",
        "bank_status": item.bank_status.value,
        "workflow_status": item.workflow_status.value,
        "matched_transaction_id": item.matched_transaction_id,
        "suggested_account_code": item.suggested_account_code,
        "posted_transaction_id": item.posted_transaction_id,
    }


def _unique_account_code(ledger: Ledger, name: str, kind: AccountKind) -> str:
    prefix = "Liabilities" if kind in {
        AccountKind.CREDIT_CARD,
        AccountKind.LOAN,
        AccountKind.CUSTOM_LIABILITY,
    } else "Assets"
    slug = re.sub(r"[^A-Za-z0-9]+", "", name.title()) or "Account"
    base = f"{prefix}:{slug}"
    existing = {account.code for account in ledger.list_accounts()}
    if base not in existing:
        return base
    number = 2
    while f"{base}{number}" in existing:
        number += 1
    return f"{base}{number}"


def _date_at_noon(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format") from exc
    return datetime.combine(parsed, time(hour=12), tzinfo=timezone.utc)


def _date_only(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format") from exc


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def _integer(payload: dict[str, Any], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    result = _optional_integer(value, minimum=minimum)
    if result is None:
        raise ValueError(f"{key} is required")
    return result


def _signed_integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_integer(value: object, *, minimum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"value must be an integer greater than or equal to {minimum}")
    return value


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
