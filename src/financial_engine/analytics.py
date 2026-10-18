"""Deterministic spending analytics and explainable savings insights."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .budgets import BudgetService, validate_month
from .ledger import Ledger, _validate_currency
from .models import CategorySpend, MonthlyCashFlow, MonthlySpendingSummary, SavingsInsight


class AnalyticsService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.budgets = BudgetService(ledger)

    def monthly_spending(
        self,
        *,
        month: str,
        currency: str = "USD",
        average_months: int = 3,
    ) -> MonthlySpendingSummary:
        month = validate_month(month)
        currency = _validate_currency(currency)
        if isinstance(average_months, bool) or not isinstance(average_months, int):
            raise ValueError("average_months must be an integer")
        if not 1 <= average_months <= 24:
            raise ValueError("average_months must be between 1 and 24")

        current_start, current_end = _month_bounds(month)
        average_start = _shift_month(current_start, -average_months)
        current = self._expense_totals(current_start, current_end, currency)
        historic_totals = self._expense_totals(average_start, current_start, currency)
        averages = {
            code: int(round(item["amount_minor"] / average_months))
            for code, item in historic_totals.items()
        }

        budget = self.budgets.get_monthly_budget(month=month, currency=currency)
        budget_by_code = (
            {item.account_code: item for item in budget.allocations} if budget else {}
        )
        all_codes = set(current) | set(averages) | set(budget_by_code)
        categories: list[CategorySpend] = []
        for code in all_codes:
            current_item = current.get(code)
            budget_item = budget_by_code.get(code)
            name = (
                current_item["account_name"]
                if current_item
                else budget_item.account_name
                if budget_item
                else code
            )
            spent = current_item["amount_minor"] if current_item else 0
            budget_minor = budget_item.amount_minor if budget_item else None
            categories.append(
                CategorySpend(
                    account_code=code,
                    account_name=name,
                    spent_minor=spent,
                    average_minor=averages.get(code, 0),
                    budget_minor=budget_minor,
                    remaining_minor=budget_minor - spent if budget_minor is not None else None,
                    is_flexible=budget_item.is_flexible if budget_item else True,
                )
            )
        categories.sort(key=lambda item: (item.spent_minor, item.account_name), reverse=True)

        spent_total = sum(item.spent_minor for item in categories)
        average_total = sum(item.average_minor for item in categories)
        budget_total = (
            sum(item.amount_minor for item in budget.allocations) if budget is not None else None
        )
        insights = _build_insights(
            month, categories, spent_total, budget_total, average_months
        )
        return MonthlySpendingSummary(
            month=month,
            currency=currency,
            spent_minor=spent_total,
            average_minor=average_total,
            budget_minor=budget_total,
            remaining_minor=budget_total - spent_total if budget_total is not None else None,
            categories=tuple(categories),
            insights=insights,
        )

    def cash_flow(
        self, *, month: str, currency: str = "USD", months: int = 6
    ) -> tuple[MonthlyCashFlow, ...]:
        """Income, spending, and net for ``month`` and the months before it, oldest first."""

        month = validate_month(month)
        currency = _validate_currency(currency)
        if isinstance(months, bool) or not isinstance(months, int) or not 1 <= months <= 24:
            raise ValueError("months must be between 1 and 24")
        current_start, _ = _month_bounds(month)
        start = _shift_month(current_start, -(months - 1))
        end = _shift_month(current_start, 1)
        rows = self.ledger.connection.execute(
            """
            SELECT substr(t.effective_at, 1, 7) AS month, a.type,
                   coalesce(sum(p.amount_minor), 0) AS amount_minor
            FROM postings p
            JOIN accounts a ON a.id = p.account_id
            JOIN transactions t ON t.id = p.transaction_id
            WHERE t.status = 'POSTED'
              AND a.type IN ('INCOME', 'EXPENSE')
              AND a.currency = ?
              AND t.effective_at >= ?
              AND t.effective_at < ?
            GROUP BY 1, 2
            """,
            (currency, start.isoformat(timespec="microseconds"), end.isoformat(timespec="microseconds")),
        ).fetchall()
        totals = {(row["month"], row["type"]): row["amount_minor"] for row in rows}
        result: list[MonthlyCashFlow] = []
        for offset in range(months):
            key = _shift_month(start, offset).strftime("%Y-%m")
            # Income accounts carry credit (negative) balances; flip to a positive amount.
            income = -totals.get((key, "INCOME"), 0)
            spent = totals.get((key, "EXPENSE"), 0)
            net = income - spent
            result.append(
                MonthlyCashFlow(
                    month=key,
                    currency=currency,
                    income_minor=income,
                    spent_minor=spent,
                    net_minor=net,
                    savings_rate=round(net / income, 4) if income > 0 else None,
                )
            )
        return tuple(result)

    def _expense_totals(
        self, start: datetime, end: datetime, currency: str
    ) -> dict[str, dict[str, int | str]]:
        rows = self.ledger.connection.execute(
            """
            SELECT a.code, a.name, coalesce(sum(p.amount_minor), 0) AS amount_minor
            FROM postings p
            JOIN accounts a ON a.id = p.account_id
            JOIN transactions t ON t.id = p.transaction_id
            WHERE t.status = 'POSTED'
              AND a.type = 'EXPENSE'
              AND a.currency = ?
              AND t.effective_at >= ?
              AND t.effective_at < ?
            GROUP BY a.id
            """,
            (currency, start.isoformat(timespec="microseconds"), end.isoformat(timespec="microseconds")),
        ).fetchall()
        return {
            row["code"]: {"account_name": row["name"], "amount_minor": row["amount_minor"]}
            for row in rows
        }


def _build_insights(
    month: str,
    categories: list[CategorySpend],
    spent_total: int,
    budget_total: int | None,
    average_months: int,
) -> tuple[SavingsInsight, ...]:
    insights: list[SavingsInsight] = []
    for category in categories:
        if (
            category.is_flexible
            and category.budget_minor is not None
            and category.spent_minor > category.budget_minor
        ):
            overage = category.spent_minor - category.budget_minor
            insights.append(
                _insight(
                    month,
                    "CATEGORY_OVER_BUDGET",
                    "attention",
                    f"{category.account_name} is over its target",
                    f"Spending is {_money(overage)} above the category budget.",
                    category.account_code,
                    overage,
                )
            )
        elif (
            category.is_flexible
            and category.average_minor > 0
            and category.spent_minor >= int(category.average_minor * 1.2)
            and category.spent_minor - category.average_minor >= 1_000
        ):
            increase = category.spent_minor - category.average_minor
            percent = round((category.spent_minor / category.average_minor - 1) * 100)
            insights.append(
                _insight(
                    month,
                    "CATEGORY_ABOVE_AVERAGE",
                    "notice",
                    f"{category.account_name} is above its recent average",
                    f"It is {percent}% ({_money(increase)}) above the previous {average_months}-month average.",
                    category.account_code,
                    increase,
                )
            )
    if budget_total is not None and spent_total > budget_total:
        overage = spent_total - budget_total
        insights.insert(
            0,
            _insight(
                month,
                "TOTAL_OVER_BUDGET",
                "attention",
                "Monthly spending is over plan",
                f"Net expense is {_money(overage)} above the total monthly budget.",
                None,
                overage,
            ),
        )
    positive_categories = [item for item in categories if item.spent_minor > 0]
    if not insights and positive_categories and spent_total > 0:
        largest = positive_categories[0]
        insights.append(
            _insight(
                month,
                "LARGEST_CATEGORY",
                "info",
                f"{largest.account_name} is the largest category",
                f"It currently represents the largest share of this month's {_money(spent_total)} net spend.",
                largest.account_code,
                None,
            )
        )
    return tuple(insights[:6])


def _insight(
    month: str,
    kind: str,
    severity: str,
    title: str,
    explanation: str,
    account_code: str | None,
    potential_savings_minor: int | None,
) -> SavingsInsight:
    identity = f"{month}|{kind}|{account_code or ''}"
    return SavingsInsight(
        id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        kind=kind,
        severity=severity,
        title=title,
        explanation=explanation,
        account_code=account_code,
        potential_savings_minor=potential_savings_minor,
    )


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, month_number = (int(part) for part in month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    end = _shift_month(start, 1)
    return start, end


def _shift_month(value: datetime, offset: int) -> datetime:
    absolute = value.year * 12 + value.month - 1 + offset
    return datetime(absolute // 12, absolute % 12 + 1, 1, tzinfo=timezone.utc)


def _money(amount_minor: int) -> str:
    return f"${amount_minor / 100:,.2f}"
