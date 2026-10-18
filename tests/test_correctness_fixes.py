from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine import (  # noqa: E402
    AccountKind,
    ImportRowStatus,
    Ledger,
    PeriodLocked,
    PostingInput,
    ReconciliationService,
    StatementImportService,
)
from financial_engine.bootstrap import ensure_default_categories  # noqa: E402
from financial_engine.statement_imports import _parse_money  # noqa: E402


def instant(month: int, day: int) -> datetime:
    return datetime(2026, month, day, 12, tzinfo=timezone.utc)


class CorrectnessFixTests(unittest.TestCase):
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
        self.ledger.onboard_account(
            code="Liabilities:Card",
            name="Visa",
            kind=AccountKind.CREDIT_CARD,
            opening_balance_minor=0,
            balance_as_of=instant(7, 1),
        )
        self.reconciliations = ReconciliationService(self.ledger)
        self.imports = StatementImportService(self.ledger)

    def tearDown(self) -> None:
        self.ledger.close()

    def spend(self, account_code: str, amount: int, when: datetime) -> str:
        return self.ledger.post_transaction(
            effective_at=when,
            description="Groceries",
            postings=(
                PostingInput("Expenses:Groceries", amount),
                PostingInput(account_code, -amount),
            ),
        ).id

    def reconcile(self, account_code: str, statement_date: date, balance: int):
        return self.reconciliations.complete(
            account_code=account_code,
            statement_date=statement_date,
            statement_balance_minor=balance,
            completed_by="user",
        )

    def test_reconfirming_a_liability_statement_is_idempotent(self) -> None:
        self.spend("Liabilities:Card", 2_500, instant(7, 10))
        first = self.reconcile("Liabilities:Card", date(2026, 7, 31), 2_500)
        again = self.reconcile("Liabilities:Card", date(2026, 7, 31), 2_500)
        self.assertEqual(again, first)
        self.assertEqual(again.statement_balance_minor, 2_500)
        self.assertEqual(self.reconciliations.history("Liabilities:Card"), (first,))

    def test_postings_in_a_reconciled_period_are_rejected(self) -> None:
        charge = self.spend("Assets:Checking", 1_000, instant(7, 10))
        self.reconcile("Assets:Checking", date(2026, 7, 31), 99_000)

        for when in (instant(7, 31), datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)):
            with self.assertRaises(PeriodLocked) as caught:
                self.spend("Assets:Checking", 500, when)
        self.assertIn("Checking is reconciled through 2026-07-31", str(caught.exception))
        with self.assertRaises(PeriodLocked):
            self.ledger.reverse_transaction(charge, effective_at=instant(7, 20), reason="oops")
        with self.assertRaises(PeriodLocked):
            self.ledger.replace_transaction(
                charge,
                effective_at=instant(8, 2),
                description="Groceries",
                postings=(
                    PostingInput("Expenses:Groceries", 900),
                    PostingInput("Assets:Checking", -900),
                ),
            )
        # Nothing partially landed, and the matched statement still matches.
        preview = self.reconciliations.preview(
            account_code="Assets:Checking",
            statement_date=date(2026, 7, 31),
            statement_balance_minor=99_000,
        )
        self.assertEqual(preview.difference_minor, 0)

        # Later dates and unreconciled accounts are unaffected.
        self.spend("Assets:Checking", 500, datetime(2026, 8, 1, tzinfo=timezone.utc))
        self.ledger.reverse_transaction(charge, effective_at=instant(8, 2), reason="refund")
        self.spend("Liabilities:Card", 500, instant(7, 15))

    def test_overlapping_csv_imports_skip_already_staged_rows(self) -> None:
        first = self.imports.import_csv(
            account_code="Assets:Checking",
            filename="july-a.csv",
            csv_text="""Date,Description,Amount,Transaction ID
07/10/2026,COFFEE,-4.75,bank-1
07/11/2026,LUNCH,-12.00,bank-2
""",
        )
        self.assertEqual(len(first.rows), 2)
        self.imports.ignore_row(first.rows[1].id, reviewed_by="user")

        overlap = self.imports.import_csv(
            account_code="Assets:Checking",
            filename="july-b.csv",
            csv_text="""Date,Description,Amount,Transaction ID
07/10/2026,COFFEE (renamed by bank),-4.75,bank-1
07/11/2026,LUNCH,-12.00,bank-2
07/12/2026,BOOKS,-30.00,bank-3
""",
        )
        # bank-1 is a duplicate by external id; bank-2 was ignored, so it may come back.
        self.assertEqual(
            [row.external_id for row in overlap.rows], ["bank-2", "bank-3"]
        )
        self.assertEqual(overlap.row_count, 2)

    def test_identical_rows_without_ids_are_counted_not_collapsed(self) -> None:
        two = """Date,Description,Amount
07/10/2026,PARKING,-2.00
07/10/2026,PARKING,-2.00
"""
        batch = self.imports.import_csv(
            account_code="Assets:Checking", filename="a.csv", csv_text=two
        )
        self.assertEqual(len(batch.rows), 2)

        three = two + "07/10/2026,PARKING,-2.00\n07/11/2026,PARKING,-2.00\n"
        batch = self.imports.import_csv(
            account_code="Assets:Checking", filename="b.csv", csv_text=three
        )
        self.assertEqual(
            [(row.effective_date.day, row.workflow_status) for row in batch.rows],
            [(10, ImportRowStatus.NEEDS_REVIEW), (11, ImportRowStatus.NEEDS_REVIEW)],
        )

    def test_money_parsing_handles_bank_suffixes_and_rejects_non_finite(self) -> None:
        self.assertEqual(_parse_money("12.00 DR"), -1_200)
        self.assertEqual(_parse_money("12.00dr"), -1_200)
        self.assertEqual(_parse_money("12.00 CR"), 1_200)
        self.assertEqual(_parse_money("12.00-"), -1_200)
        self.assertEqual(_parse_money("$1,234.56"), 123_456)
        self.assertEqual(_parse_money("(4.75)"), -475)
        self.assertEqual(_parse_money("1000000000000.00"), 100_000_000_000_000)
        for bad in ("1e30", "NaN", "Infinity", "-inf", "12.00--", "1000000000000.01", "9" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _parse_money(bad)

    def test_debit_credit_columns_ignore_sign_markers(self) -> None:
        batch = self.imports.import_csv(
            account_code="Assets:Checking",
            filename="split.csv",
            csv_text="""Date,Description,Debit,Credit
07/10/2026,COFFEE,4.75 DR,
07/11/2026,REFUND,,3.00 CR
07/12/2026,FEE,-1.00,
""",
        )
        self.assertEqual([row.account_amount_minor for row in batch.rows], [-475, 300, -100])

    def test_bad_csv_amount_is_a_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "CSV line 2: invalid amount"):
            self.imports.import_csv(
                account_code="Assets:Checking",
                filename="bad.csv",
                csv_text="Date,Description,Amount\n07/10/2026,X,1e30\n",
            )


if __name__ == "__main__":
    unittest.main()
