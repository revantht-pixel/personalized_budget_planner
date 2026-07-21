"""Safe, idempotent setup for the default chart-of-accounts categories."""

from .errors import DuplicateAccount
from .ledger import Ledger
from .models import AccountKind, AccountType

DEFAULT_EXPENSE_CATEGORIES = (
    ("Expenses:Housing", "Housing"),
    ("Expenses:Groceries", "Groceries"),
    ("Expenses:Dining", "Dining"),
    ("Expenses:Transport", "Transport"),
    ("Expenses:Utilities", "Utilities"),
    ("Expenses:Health", "Health"),
    ("Expenses:Shopping", "Shopping"),
    ("Expenses:Entertainment", "Entertainment"),
    ("Expenses:Other", "Other"),
)


def ensure_default_categories(ledger: Ledger, *, currency: str = "USD") -> None:
    existing_codes = {account.code for account in ledger.list_accounts()}
    for code, name in DEFAULT_EXPENSE_CATEGORIES:
        if code in existing_codes:
            continue
        try:
            ledger.create_account(
                code=code,
                name=name,
                account_type=AccountType.EXPENSE,
                currency=currency,
                kind=AccountKind.CATEGORY,
            )
        except DuplicateAccount:
            pass
        else:
            existing_codes.add(code)
    if "Income:Other" in existing_codes:
        return
    try:
        ledger.create_account(
            code="Income:Other",
            name="Income",
            account_type=AccountType.INCOME,
            currency=currency,
            kind=AccountKind.CATEGORY,
        )
    except DuplicateAccount:
        pass
