"""Small immutable domain objects used by the ledger API."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class AccountKind(StrEnum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    CASH = "CASH"
    CREDIT_CARD = "CREDIT_CARD"
    LOAN = "LOAN"
    INVESTMENT = "INVESTMENT"
    GIFT_CARD = "GIFT_CARD"
    RECEIVABLE = "RECEIVABLE"
    CUSTOM_ASSET = "CUSTOM_ASSET"
    CUSTOM_LIABILITY = "CUSTOM_LIABILITY"
    CATEGORY = "CATEGORY"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    code: str
    name: str
    type: AccountType
    currency: str
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PostingInput:
    """A proposed debit (positive) or credit (negative), in minor units."""

    account_code: str
    amount_minor: int
    memo: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copy callers' mutable dictionaries so the input cannot change mid-post.
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))


@dataclass(frozen=True, slots=True)
class Posting:
    id: str
    transaction_id: str
    account_id: str
    account_code: str
    amount_minor: int
    memo: str | None
    tags: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tags", MappingProxyType(dict(self.tags)))


@dataclass(frozen=True, slots=True)
class JournalTransaction:
    id: str
    effective_at: datetime
    description: str
    currency: str
    merchant_name: str | None
    source_type: str | None
    source_id: str | None
    correlation_id: str | None
    reversal_of_transaction_id: str | None
    created_at: datetime
    posted_at: datetime
    postings: tuple[Posting, ...]


@dataclass(frozen=True, slots=True)
class AccountBalance:
    account: Account
    balance_minor: int


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    id: str
    account_id: str
    observed_at: datetime
    ledger_balance_minor: int
    available_to_spend_minor: int | None
    source: str


@dataclass(frozen=True, slots=True)
class AccountOverview:
    account: Account
    kind: AccountKind
    ledger_balance_minor: int
    display_balance_minor: int
    available_balance_minor: int | None
    credit_limit_minor: int | None
    balance_source: str
    observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class BudgetAllocation:
    account_code: str
    account_name: str
    amount_minor: int
    is_flexible: bool


@dataclass(frozen=True, slots=True)
class MonthlyBudget:
    id: str
    month: str
    currency: str
    name: str
    allocations: tuple[BudgetAllocation, ...]


@dataclass(frozen=True, slots=True)
class CategorySpend:
    account_code: str
    account_name: str
    spent_minor: int
    average_minor: int
    budget_minor: int | None
    remaining_minor: int | None
    is_flexible: bool


@dataclass(frozen=True, slots=True)
class SavingsInsight:
    id: str
    kind: str
    severity: str
    title: str
    explanation: str
    account_code: str | None
    potential_savings_minor: int | None


@dataclass(frozen=True, slots=True)
class MonthlySpendingSummary:
    month: str
    currency: str
    spent_minor: int
    average_minor: int
    budget_minor: int | None
    remaining_minor: int | None
    categories: tuple[CategorySpend, ...]
    insights: tuple[SavingsInsight, ...]


class ProposalStatus(StrEnum):
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class TransactionProposal:
    id: str
    source_type: str
    source_id: str
    status: ProposalStatus
    confidence: float | None
    effective_at: datetime
    description: str
    currency: str
    merchant_name: str | None
    correlation_id: str | None
    postings: tuple[PostingInput, ...]
    evidence: Mapping[str, str]
    provider_metadata: Mapping[str, str]
    approved_transaction_id: str | None
    reviewed_by: str | None
    review_note: str | None
    created_at: datetime
    reviewed_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "provider_metadata", MappingProxyType(dict(self.provider_metadata)))


@dataclass(frozen=True, slots=True)
class ReconciliationPreview:
    account: Account
    statement_date: date
    statement_balance_minor: int
    ledger_balance_minor: int
    difference_minor: int


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    id: str
    account: Account
    statement_date: date
    statement_balance_minor: int
    ledger_balance_minor: int
    completed_by: str
    created_at: datetime


class BankTransactionStatus(StrEnum):
    PENDING = "PENDING"
    CLEARED = "CLEARED"


class ImportRowStatus(StrEnum):
    PENDING = "PENDING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    MATCHED = "MATCHED"
    POSTED = "POSTED"
    IGNORED = "IGNORED"


class AmountConvention(StrEnum):
    ACCOUNT_SIGNED = "ACCOUNT_SIGNED"
    CHARGES_POSITIVE = "CHARGES_POSITIVE"


@dataclass(frozen=True, slots=True)
class StatementImportRow:
    id: str
    batch_id: str
    account: Account
    row_number: int
    external_id: str | None
    effective_date: date
    description: str
    account_amount_minor: int
    bank_status: BankTransactionStatus
    workflow_status: ImportRowStatus
    matched_transaction_id: str | None
    suggested_account_code: str | None
    posted_transaction_id: str | None
    reviewed_by: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StatementImportBatch:
    id: str
    account: Account
    filename: str
    amount_convention: AmountConvention
    row_count: int
    created_at: datetime
    rows: tuple[StatementImportRow, ...]
