"""Monthly budget configuration, deliberately separate from ledger facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from uuid import uuid4

from .errors import AccountNotFound
from .ledger import Ledger, _utc_now_iso, _validate_currency
from .models import AccountType, BudgetAllocation, MonthlyBudget

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class BudgetService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def set_monthly_budget(
        self,
        *,
        month: str,
        allocations: Mapping[str, int],
        currency: str = "USD",
        name: str | None = None,
        flexible_categories: set[str] | None = None,
    ) -> MonthlyBudget:
        month = validate_month(month)
        currency = _validate_currency(currency)
        if not allocations:
            raise ValueError("a budget needs at least one category allocation")
        flexible_categories = flexible_categories or set(allocations)

        validated: list[tuple[str, str, int, bool]] = []
        for account_code, amount_minor in allocations.items():
            try:
                account = self.ledger.get_account(account_code)
            except AccountNotFound:
                raise
            if account.type is not AccountType.EXPENSE:
                raise ValueError(f"budget account must be an expense category: {account_code}")
            if account.currency != currency:
                raise ValueError(f"budget category uses {account.currency}: {account_code}")
            if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor < 0:
                raise ValueError("budget allocations must be non-negative integer minor units")
            validated.append(
                (account.id, account.code, amount_minor, account.code in flexible_categories)
            )

        now = _utc_now_iso()
        with self.ledger._write():
            row = self.ledger.connection.execute(
                "SELECT id FROM budgets WHERE month = ? AND currency = ?",
                (month, currency),
            ).fetchone()
            if row is None:
                budget_id = str(uuid4())
                self.ledger.connection.execute(
                    """
                    INSERT INTO budgets(id, month, currency, name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (budget_id, month, currency, name or f"{month} budget", now, now),
                )
            else:
                budget_id = row["id"]
                self.ledger.connection.execute(
                    "UPDATE budgets SET name = ?, updated_at = ? WHERE id = ?",
                    (name or f"{month} budget", now, budget_id),
                )
                self.ledger.connection.execute(
                    "DELETE FROM budget_allocations WHERE budget_id = ?", (budget_id,)
                )

            for account_id, _code, amount_minor, is_flexible in validated:
                self.ledger.connection.execute(
                    """
                    INSERT INTO budget_allocations(
                        id, budget_id, account_id, amount_minor, is_flexible
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), budget_id, account_id, amount_minor, int(is_flexible)),
                )
        budget = self.get_monthly_budget(month=month, currency=currency)
        assert budget is not None
        return budget

    def get_monthly_budget(
        self, *, month: str, currency: str = "USD"
    ) -> MonthlyBudget | None:
        month = validate_month(month)
        currency = _validate_currency(currency)
        row = self.ledger.connection.execute(
            "SELECT * FROM budgets WHERE month = ? AND currency = ?",
            (month, currency),
        ).fetchone()
        if row is None:
            return None
        allocation_rows = self.ledger.connection.execute(
            """
            SELECT a.code, a.name, ba.amount_minor, ba.is_flexible
            FROM budget_allocations ba
            JOIN accounts a ON a.id = ba.account_id
            WHERE ba.budget_id = ?
            ORDER BY a.name
            """,
            (row["id"],),
        ).fetchall()
        return MonthlyBudget(
            id=row["id"],
            month=row["month"],
            currency=row["currency"],
            name=row["name"],
            allocations=tuple(
                BudgetAllocation(
                    account_code=item["code"],
                    account_name=item["name"],
                    amount_minor=item["amount_minor"],
                    is_flexible=bool(item["is_flexible"]),
                )
                for item in allocation_rows
            ),
        )


def validate_month(month: str) -> str:
    if not isinstance(month, str) or not _MONTH_RE.fullmatch(month):
        raise ValueError("month must use YYYY-MM format")
    return month

