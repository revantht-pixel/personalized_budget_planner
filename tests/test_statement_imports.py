from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine import (  # noqa: E402
    AccountKind,
    AmountConvention,
    ImportRowStatus,
    Ledger,
    PostingInput,
    StatementImportService,
)
from financial_engine.bootstrap import ensure_default_categories  # noqa: E402
from financial_engine.errors import StatementImportStateError  # noqa: E402


def instant(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 12, tzinfo=timezone.utc)


class StatementImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()
        self.ledger.initialize()
        ensure_default_categories(self.ledger)
        self.ledger.onboard_account(
            code="Assets:Checking",
            name="Checking",
            kind=AccountKind.CHECKING,
            opening_balance_minor=100_000,
            balance_as_of=instant(7, 1),
        )
        self.imports = StatementImportService(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def test_csv_rows_are_matched_staged_and_posted_only_after_review(self) -> None:
        existing = self.ledger.post_transaction(
            effective_at=instant(7, 10),
            description="Existing grocery entry",
            postings=(
                PostingInput("Expenses:Groceries", 1_250),
                PostingInput("Assets:Checking", -1_250),
            ),
        )
        csv_text = """Date,Description,Amount,Status,Transaction ID
07/10/2026,GROCERY STORE,-12.50,Cleared,bank-1
07/12/2026,COFFEE SHOP,-4.75,Cleared,bank-2
07/13/2026,PENDING FUEL,-9.99,Pending,bank-3
07/15/2026,PAYCHECK,100.00,Cleared,bank-4
"""

        batch = self.imports.import_csv(
            account_code="Assets:Checking",
            filename="checking.csv",
            csv_text=csv_text,
        )

        self.assertEqual(batch.row_count, 4)
        self.assertEqual(batch.rows[0].workflow_status, ImportRowStatus.MATCHED)
        self.assertEqual(batch.rows[0].matched_transaction_id, existing.id)
        self.assertEqual(batch.rows[1].workflow_status, ImportRowStatus.NEEDS_REVIEW)
        self.assertEqual(batch.rows[2].workflow_status, ImportRowStatus.PENDING)
        self.assertEqual(batch.rows[3].suggested_account_code, "Income:Other")
        self.assertEqual(len(self.imports.review_queue()), 2)

        coffee = self.imports.post_row(
            batch.rows[1].id,
            category_code="Expenses:Dining",
            reviewed_by="user",
        )
        paycheck = self.imports.post_row(
            batch.rows[3].id,
            category_code="Income:Other",
            reviewed_by="user",
        )
        self.assertEqual(coffee.workflow_status, ImportRowStatus.POSTED)
        self.assertEqual(paycheck.workflow_status, ImportRowStatus.POSTED)
        with self.assertRaises(StatementImportStateError):
            self.imports.post_row(
                batch.rows[2].id,
                category_code="Expenses:Transport",
                reviewed_by="user",
            )
        ignored = self.imports.ignore_row(batch.rows[2].id, reviewed_by="user")
        self.assertEqual(ignored.workflow_status, ImportRowStatus.IGNORED)

        replay = self.imports.import_csv(
            account_code="Assets:Checking",
            filename="renamed.csv",
            csv_text=csv_text,
        )
        self.assertEqual(replay.id, batch.id)
        overview = self.ledger.get_account_overview("Assets:Checking")
        self.assertEqual(overview.ledger_balance_minor, 108_275)

    def test_credit_card_charge_positive_convention_flips_account_impact(self) -> None:
        self.ledger.onboard_account(
            code="Liabilities:Card",
            name="Card",
            kind=AccountKind.CREDIT_CARD,
            opening_balance_minor=0,
            credit_limit_minor=100_000,
            balance_as_of=instant(7, 1),
        )
        batch = self.imports.import_csv(
            account_code="Liabilities:Card",
            filename="card.csv",
            amount_convention=AmountConvention.CHARGES_POSITIVE,
            csv_text="Date,Description,Amount\n07/19/2026,BOOK STORE,25.00\n",
        )
        row = batch.rows[0]
        self.assertEqual(row.account_amount_minor, -2_500)
        self.assertEqual(row.suggested_account_code, "Expenses:Other")

        self.imports.post_row(
            row.id, category_code="Expenses:Shopping", reviewed_by="user"
        )
        card = self.ledger.get_account_overview("Liabilities:Card")
        self.assertEqual(card.display_balance_minor, 2_500)
        self.assertEqual(card.available_balance_minor, 97_500)


if __name__ == "__main__":
    unittest.main()
