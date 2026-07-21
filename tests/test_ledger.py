from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine import (  # noqa: E402
    AccountType,
    CurrencyMismatch,
    IdempotencyConflict,
    InvalidTransaction,
    Ledger,
    PostingInput,
)


NOW = datetime(2026, 7, 19, 18, 0, tzinfo=timezone.utc)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = Ledger()
        self.ledger.initialize()

    def tearDown(self) -> None:
        self.ledger.close()

    def add_account(
        self,
        code: str,
        account_type: AccountType,
        currency: str = "USD",
    ) -> None:
        self.ledger.create_account(
            code=code,
            name=code,
            account_type=account_type,
            currency=currency,
        )

    def balances(self) -> dict[str, int]:
        return {
            item.account.code: item.balance_minor for item in self.ledger.trial_balance()
        }

    def test_split_tender_balances(self) -> None:
        self.add_account("Expenses:Clothing", AccountType.EXPENSE)
        self.add_account("Assets:GiftCard", AccountType.ASSET)
        self.add_account("Liabilities:Card", AccountType.LIABILITY)

        transaction = self.ledger.post_transaction(
            effective_at=NOW,
            description="Split tender",
            source_type="BANK_IMPORT",
            source_id="charge-001",
            postings=(
                PostingInput("Expenses:Clothing", 10_000),
                PostingInput("Assets:GiftCard", -2_000),
                PostingInput("Liabilities:Card", -8_000),
            ),
        )

        self.assertEqual(sum(p.amount_minor for p in transaction.postings), 0)
        self.assertEqual(
            self.balances(),
            {
                "Assets:GiftCard": -2_000,
                "Expenses:Clothing": 10_000,
                "Liabilities:Card": -8_000,
            },
        )

    def test_reimbursement_uses_receivable_and_does_not_inflate_income(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Assets:FriendReceivable", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)
        correlation_id = "dinner-2026-07-19"

        self.ledger.post_transaction(
            effective_at=NOW,
            description="Group dinner paid",
            correlation_id=correlation_id,
            postings=(
                PostingInput("Expenses:Dining", 5_000),
                PostingInput("Assets:FriendReceivable", 10_000),
                PostingInput("Assets:Checking", -15_000),
            ),
        )
        self.ledger.post_transaction(
            effective_at=NOW,
            description="Friends repaid dinner share",
            correlation_id=correlation_id,
            postings=(
                PostingInput("Assets:Checking", 10_000),
                PostingInput("Assets:FriendReceivable", -10_000),
            ),
        )

        self.assertEqual(
            self.balances(),
            {
                "Assets:Checking": -5_000,
                "Assets:FriendReceivable": 0,
                "Expenses:Dining": 5_000,
            },
        )

    def test_refundable_security_deposit_is_an_asset(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Assets:SecurityDeposit", AccountType.ASSET)

        self.ledger.post_transaction(
            effective_at=NOW,
            description="Apartment security deposit",
            postings=(
                PostingInput("Assets:SecurityDeposit", 120_000),
                PostingInput("Assets:Checking", -120_000),
            ),
        )

        self.assertEqual(self.balances()["Assets:SecurityDeposit"], 120_000)

    def test_unbalanced_transaction_is_rejected_without_partial_state(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)

        with self.assertRaises(InvalidTransaction):
            self.ledger.post_transaction(
                effective_at=NOW,
                description="Bad import",
                postings=(
                    PostingInput("Expenses:Dining", 1_000),
                    PostingInput("Assets:Checking", -999),
                ),
            )

        count = self.ledger.connection.execute(
            "SELECT count(*) FROM transactions"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_database_rejects_unbalanced_draft_and_reports_ignore_it(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)
        checking_id = self.ledger.get_account("Assets:Checking").id
        now = NOW.isoformat()
        self.ledger.connection.execute(
            """
            INSERT INTO transactions(
                id, effective_at, description, currency, source_type, source_id,
                content_hash, status, created_at, posted_at
            ) VALUES ('draft-1', ?, 'Incomplete proposal', 'USD', NULL, NULL,
                      'not-posted', 'DRAFT', ?, NULL)
            """,
            (now, now),
        )
        self.ledger.connection.execute(
            """
            INSERT INTO postings(
                id, transaction_id, account_id, amount_minor, sort_order, created_at
            ) VALUES ('draft-posting-1', 'draft-1', ?, 500, 0, ?)
            """,
            (checking_id, now),
        )

        self.assertEqual(self.balances()["Assets:Checking"], 0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                """
                UPDATE transactions
                SET status = 'POSTED', posted_at = ?
                WHERE id = 'draft-1'
                """,
                (now,),
            )

    def test_identical_source_replay_is_idempotent(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Income:Salary", AccountType.INCOME)
        arguments = dict(
            effective_at=NOW,
            description="Paycheck",
            source_type="BANK_IMPORT",
            source_id="pay-001",
            postings=(
                PostingInput("Assets:Checking", 200_000),
                PostingInput("Income:Salary", -200_000),
            ),
        )

        first = self.ledger.post_transaction(**arguments)
        replay = self.ledger.post_transaction(**arguments)

        self.assertEqual(first.id, replay.id)
        count = self.ledger.connection.execute(
            "SELECT count(*) FROM transactions WHERE status = 'POSTED'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_changed_source_replay_raises_conflict(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)
        self.ledger.post_transaction(
            effective_at=NOW,
            description="Dinner",
            source_type="BANK_IMPORT",
            source_id="charge-001",
            postings=(
                PostingInput("Expenses:Dining", 1_000),
                PostingInput("Assets:Checking", -1_000),
            ),
        )

        with self.assertRaises(IdempotencyConflict):
            self.ledger.post_transaction(
                effective_at=NOW,
                description="Dinner",
                source_type="BANK_IMPORT",
                source_id="charge-001",
                postings=(
                    PostingInput("Expenses:Dining", 1_100),
                    PostingInput("Assets:Checking", -1_100),
                ),
            )

    def test_cross_currency_transaction_requires_explicit_fx_support(self) -> None:
        self.add_account("Assets:USD", AccountType.ASSET, "USD")
        self.add_account("Assets:EUR", AccountType.ASSET, "EUR")

        with self.assertRaises(CurrencyMismatch):
            self.ledger.post_transaction(
                effective_at=NOW,
                description="Implicit FX is unsafe",
                currency="USD",
                postings=(
                    PostingInput("Assets:USD", -10_000),
                    PostingInput("Assets:EUR", 10_000),
                ),
            )

    def test_posted_rows_are_immutable_at_database_boundary(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)
        transaction = self.ledger.post_transaction(
            effective_at=NOW,
            description="Dinner",
            postings=(
                PostingInput("Expenses:Dining", 1_000),
                PostingInput("Assets:Checking", -1_000),
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.connection.execute(
                "UPDATE postings SET amount_minor = 999 WHERE transaction_id = ?",
                (transaction.id,),
            )

    def test_reversal_offsets_original_without_mutating_it(self) -> None:
        self.add_account("Assets:Checking", AccountType.ASSET)
        self.add_account("Expenses:Dining", AccountType.EXPENSE)
        original = self.ledger.post_transaction(
            effective_at=NOW,
            description="Duplicate charge",
            postings=(
                PostingInput("Expenses:Dining", 1_000),
                PostingInput("Assets:Checking", -1_000),
            ),
        )

        reversal = self.ledger.reverse_transaction(
            original.id,
            effective_at=NOW,
            reason="Duplicate bank import",
        )

        self.assertEqual(reversal.reversal_of_transaction_id, original.id)
        self.assertEqual(self.balances()["Expenses:Dining"], 0)
        self.assertEqual(self.balances()["Assets:Checking"], 0)


if __name__ == "__main__":
    unittest.main()
