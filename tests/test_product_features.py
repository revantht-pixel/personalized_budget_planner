from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine import (  # noqa: E402
    AccountKind,
    AnalyticsService,
    BudgetService,
    Ledger,
    PostingInput,
    ProposalService,
    ProposalStatus,
    ReconciliationService,
)
from financial_engine.ai import (  # noqa: E402
    AIIntegrationService,
    AIProposalDraft,
    DisabledAIAdapter,
    FinancialDocument,
)
from financial_engine.bootstrap import ensure_default_categories  # noqa: E402
from financial_engine.errors import AIIntegrationDisabled, ReconciliationMismatch  # noqa: E402


def instant(year: int, month: int, day: int = 15) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


class ProductFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()
        self.ledger.initialize()
        ensure_default_categories(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_opening_balance_and_postings_drive_available_balance(self) -> None:
        overview = self.ledger.onboard_account(
            code="Assets:Checking",
            name="Main Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=245_000,
            balance_as_of=instant(2026, 7, 1),
        )
        self.assertEqual(overview.available_balance_minor, 245_000)

        self.ledger.post_transaction(
            effective_at=instant(2026, 7, 19),
            description="Groceries",
            postings=(
                PostingInput("Expenses:Groceries", 5_250),
                PostingInput("Assets:Checking", -5_250),
            ),
        )

        updated = self.ledger.get_account_overview("Assets:Checking")
        self.assertEqual(updated.ledger_balance_minor, 239_750)
        self.assertEqual(updated.available_balance_minor, 239_750)
        opening = [
            transaction
            for transaction in self.ledger.list_transactions()
            if transaction.source_type == "OPENING_BALANCE"
        ]
        self.assertEqual(len(opening), 1)

    def test_credit_card_shows_amount_owed_and_available_credit(self) -> None:
        self.ledger.onboard_account(
            code="Liabilities:Card",
            name="Rewards Card",
            kind=AccountKind.CREDIT_CARD,
            opening_balance_minor=64_000,
            credit_limit_minor=500_000,
            balance_as_of=instant(2026, 7, 1),
        )

        overview = self.ledger.get_account_overview("Liabilities:Card")
        self.assertEqual(overview.ledger_balance_minor, -64_000)
        self.assertEqual(overview.display_balance_minor, 64_000)
        self.assertEqual(overview.available_balance_minor, 436_000)

        self.ledger.post_transaction(
            effective_at=instant(2026, 7, 19),
            description="Dinner",
            postings=(
                PostingInput("Expenses:Dining", 1_000),
                PostingInput("Liabilities:Card", -1_000),
            ),
        )
        updated = self.ledger.get_account_overview("Liabilities:Card")
        self.assertEqual(updated.display_balance_minor, 65_000)
        self.assertEqual(updated.available_balance_minor, 435_000)

    def test_bank_snapshot_overrides_calculated_available_amount_without_mutating_ledger(self) -> None:
        self.ledger.onboard_account(
            code="Assets:Checking",
            name="Main Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=100_000,
            balance_as_of=instant(2026, 7, 1),
        )
        self.ledger.record_balance_snapshot(
            account_code="Assets:Checking",
            observed_at=instant(2026, 7, 19),
            ledger_balance_minor=100_000,
            available_to_spend_minor=92_500,
            source="BANK",
            external_id="snapshot-1",
        )

        overview = self.ledger.get_account_overview("Assets:Checking")
        self.assertEqual(overview.ledger_balance_minor, 100_000)
        self.assertEqual(overview.available_balance_minor, 92_500)
        self.assertEqual(overview.balance_source, "BANK+LEDGER")

        self.ledger.post_transaction(
            effective_at=instant(2026, 7, 20),
            description="Purchase after snapshot",
            postings=(
                PostingInput("Expenses:Other", 5_000),
                PostingInput("Assets:Checking", -5_000),
            ),
        )
        updated = self.ledger.get_account_overview("Assets:Checking")
        self.assertEqual(updated.available_balance_minor, 87_500)

    def test_monthly_budget_averages_and_explainable_insights(self) -> None:
        self.ledger.onboard_account(
            code="Assets:Checking",
            name="Main Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=1_000_000,
            balance_as_of=instant(2026, 4, 1),
        )
        for month, dining in ((4, 5_000), (5, 6_000), (6, 7_000), (7, 13_000)):
            self.ledger.post_transaction(
                effective_at=instant(2026, month),
                description="Dining",
                postings=(
                    PostingInput("Expenses:Dining", dining),
                    PostingInput("Assets:Checking", -dining),
                ),
            )
        self.ledger.post_transaction(
            effective_at=instant(2026, 7),
            description="Groceries",
            postings=(
                PostingInput("Expenses:Groceries", 20_000),
                PostingInput("Assets:Checking", -20_000),
            ),
        )
        BudgetService(self.ledger).set_monthly_budget(
            month="2026-07",
            allocations={"Expenses:Dining": 10_000, "Expenses:Groceries": 30_000},
        )

        summary = AnalyticsService(self.ledger).monthly_spending(month="2026-07")

        self.assertEqual(summary.spent_minor, 33_000)
        self.assertEqual(summary.budget_minor, 40_000)
        self.assertEqual(summary.remaining_minor, 7_000)
        dining = next(item for item in summary.categories if item.account_code == "Expenses:Dining")
        self.assertEqual(dining.average_minor, 6_000)
        self.assertTrue(
            any(item.kind == "CATEGORY_OVER_BUDGET" for item in summary.insights)
        )

    def test_ai_adapter_can_only_create_a_reviewable_proposal(self) -> None:
        self.ledger.onboard_account(
            code="Assets:Checking",
            name="Main Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=50_000,
            balance_as_of=instant(2026, 7, 1),
        )

        class StubAdapter:
            provider_name = "test-provider"
            model_name = "test-model"

            def propose_transaction(self, document: FinancialDocument) -> AIProposalDraft:
                return AIProposalDraft(
                    effective_at=instant(2026, 7, 19),
                    description="Suggested groceries",
                    currency="USD",
                    postings=(
                        PostingInput("Expenses:Groceries", 2_500),
                        PostingInput("Assets:Checking", -2_500),
                    ),
                    confidence=0.91,
                    evidence={"receipt_total": "$25.00"},
                )

        proposals = ProposalService(self.ledger)
        service = AIIntegrationService(proposals, StubAdapter())
        proposal = service.analyze(
            FinancialDocument("receipt-1", "Grocery receipt", instant(2026, 7, 19))
        )

        self.assertEqual(proposal.status, ProposalStatus.NEEDS_REVIEW)
        self.assertEqual(len(self.ledger.list_transactions()), 1)  # opening balance only
        transaction = proposals.approve(proposal.id, reviewed_by="user")
        self.assertEqual(transaction.source_type, "PROPOSAL")
        self.assertEqual(len(proposals.review_queue()), 0)

    def test_disabled_ai_has_no_fallback_or_side_effect(self) -> None:
        proposals = ProposalService(self.ledger)
        service = AIIntegrationService(proposals, DisabledAIAdapter())
        with self.assertRaises(AIIntegrationDisabled):
            service.analyze(FinancialDocument("doc-1", "private", instant(2026, 7, 19)))
        self.assertEqual(len(proposals.review_queue()), 0)

    def test_reconciliation_requires_an_exact_statement_match(self) -> None:
        self.ledger.onboard_account(
            code="Assets:Checking",
            name="Main Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=10_000,
            balance_as_of=instant(2026, 7, 1),
        )
        self.ledger.post_transaction(
            effective_at=instant(2026, 7, 19),
            description="Groceries",
            postings=(
                PostingInput("Expenses:Groceries", 500),
                PostingInput("Assets:Checking", -500),
            ),
        )
        service = ReconciliationService(self.ledger)
        statement_date = instant(2026, 7, 31).date()

        mismatch = service.preview(
            account_code="Assets:Checking",
            statement_date=statement_date,
            statement_balance_minor=9_400,
        )
        self.assertEqual(mismatch.difference_minor, 100)
        with self.assertRaises(ReconciliationMismatch):
            service.complete(
                account_code="Assets:Checking",
                statement_date=statement_date,
                statement_balance_minor=9_400,
                completed_by="user",
            )

        record = service.complete(
            account_code="Assets:Checking",
            statement_date=statement_date,
            statement_balance_minor=9_500,
            completed_by="user",
        )
        self.assertEqual(record.ledger_balance_minor, 9_500)
        self.assertEqual(service.history("Assets:Checking"), (record,))


if __name__ == "__main__":
    unittest.main()
