from __future__ import annotations

import json
import sys
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine.webapp import (  # noqa: E402
    FinancialAppServer,
    prepare_database,
)
from financial_engine import AccountKind, Ledger, PostingInput, ProposalService  # noqa: E402


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Path.cwd() / f".web-test-{uuid4()}.db"
        prepare_database(self.database)
        self.server = FinancialAppServer(("127.0.0.1", 0), self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.database.unlink(missing_ok=True)

    def request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def test_account_and_transaction_flow_updates_dashboard(self) -> None:
        account_result = self.request(
            "/api/accounts",
            {
                "name": "Main Checking",
                "kind": "CHECKING",
                "opening_balance_minor": 10_000,
                "credit_limit_minor": None,
                "balance_as_of": "2026-07-01",
            },
        )
        account_code = account_result["account"]["code"]
        transaction_result = self.request(
            "/api/transactions",
            {
                "kind": "expense",
                "description": "Groceries",
                "merchant_name": "Market",
                "amount_minor": 500,
                "effective_at": "2026-07-19",
                "account_code": account_code,
                "category_code": "Expenses:Groceries",
                "request_id": "web-test-expense-1",
            },
        )

        overview = self.request("/api/overview?month=2026-07")

        self.assertEqual(overview["total_available_minor"], 9_500)
        self.assertEqual(overview["spending"]["spent_minor"], 500)
        self.assertEqual(overview["accounts"][0]["available_balance_minor"], 9_500)
        self.assertFalse(overview["ai"]["enabled"])

        self.request(
            "/api/budgets",
            {
                "month": "2026-07",
                "allocations": {"Expenses:Groceries": 2_000},
            },
        )
        budgeted = self.request("/api/overview?month=2026-07")
        self.assertEqual(budgeted["spending"]["budget_minor"], 2_000)

        detail = self.request(f"/api/accounts/{quote(account_code, safe='')}")
        self.assertEqual(detail["account"]["display_balance_minor"], 9_500)
        self.assertEqual(len(detail["transactions"]), 2)
        transaction = self.request(
            f"/api/transactions/{transaction_result['transaction_id']}"
        )
        self.assertEqual(len(transaction["postings"]), 2)

        preview = self.request(
            "/api/reconciliations/preview",
            {
                "account_code": account_code,
                "statement_date": "2026-07-31",
                "statement_balance_minor": 9_500,
            },
        )
        self.assertTrue(preview["balanced"])
        self.request(
            "/api/reconciliations/complete",
            {
                "account_code": account_code,
                "statement_date": "2026-07-31",
                "statement_balance_minor": 9_500,
                "completed_by": "test-user",
            },
        )
        reconciled_detail = self.request(f"/api/accounts/{quote(account_code, safe='')}")
        self.assertEqual(len(reconciled_detail["reconciliations"]), 1)

        self.request(
            f"/api/transactions/{transaction_result['transaction_id']}/reverse",
            {"reason": "Test correction", "effective_at": "2026-07-20"},
        )
        corrected = self.request("/api/overview?month=2026-07")
        self.assertEqual(corrected["total_available_minor"], 10_000)
        self.assertEqual(corrected["spending"]["spent_minor"], 0)

    def test_review_api_keeps_proposal_out_of_ledger_until_approval(self) -> None:
        with Ledger(self.database) as ledger:
            ledger.onboard_account(
                code="Assets:ReviewChecking",
                name="Review Checking",
                kind=AccountKind.CHECKING,
                opening_balance_minor=20_000,
                balance_as_of=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
            proposal = ProposalService(ledger).submit(
                source_type="IMPORT",
                source_id="review-test-1",
                effective_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
                description="Review groceries",
                postings=(
                    PostingInput("Expenses:Groceries", 1_000),
                    PostingInput("Assets:ReviewChecking", -1_000),
                ),
                confidence=0.8,
            )

        queue = self.request("/api/review")
        self.assertEqual(queue["proposals"][0]["id"], proposal.id)
        self.assertEqual(
            self.request("/api/overview?month=2026-07")["spending"]["spent_minor"],
            0,
        )
        self.request(
            f"/api/proposals/{proposal.id}/approve",
            {"reviewed_by": "test-user", "note": "Approved in test"},
        )
        self.assertEqual(self.request("/api/review")["proposals"], [])
        self.assertEqual(
            self.request("/api/overview?month=2026-07")["spending"]["spent_minor"],
            1_000,
        )

    def test_statement_import_stages_matches_and_reviews_csv_rows(self) -> None:
        account_result = self.request(
            "/api/accounts",
            {
                "name": "Import Checking",
                "kind": "CHECKING",
                "opening_balance_minor": 50_000,
                "credit_limit_minor": None,
                "balance_as_of": "2026-07-01",
            },
        )
        account_code = account_result["account"]["code"]
        self.request(
            "/api/transactions",
            {
                "kind": "expense",
                "description": "Existing market entry",
                "merchant_name": "Market",
                "amount_minor": 500,
                "effective_at": "2026-07-10",
                "account_code": account_code,
                "category_code": "Expenses:Groceries",
                "request_id": "existing-market-1",
            },
        )
        csv_text = """Date,Description,Amount,Status
07/10/2026,MARKET,-5.00,Cleared
07/12/2026,COFFEE,-3.00,Cleared
07/13/2026,PENDING FUEL,-2.00,Pending
"""
        imported = self.request(
            "/api/imports",
            {
                "account_code": account_code,
                "filename": "statement.csv",
                "csv_text": csv_text,
                "amount_convention": "ACCOUNT_SIGNED",
            },
        )["import"]

        self.assertEqual(imported["rows"][0]["workflow_status"], "MATCHED")
        self.assertEqual(imported["rows"][1]["workflow_status"], "NEEDS_REVIEW")
        self.assertEqual(imported["rows"][2]["workflow_status"], "PENDING")
        review = self.request("/api/review")
        self.assertEqual(len(review["import_rows"]), 1)
        row_id = review["import_rows"][0]["id"]
        self.request(
            f"/api/import-rows/{row_id}/post",
            {"category_code": "Expenses:Dining", "reviewed_by": "test-user"},
        )

        overview = self.request("/api/overview?month=2026-07")
        self.assertEqual(overview["spending"]["spent_minor"], 800)
        self.assertEqual(overview["review_count"], 0)
        replay = self.request(
            "/api/imports",
            {
                "account_code": account_code,
                "filename": "statement-renamed.csv",
                "csv_text": csv_text,
                "amount_convention": "ACCOUNT_SIGNED",
            },
        )["import"]
        self.assertEqual(replay["id"], imported["id"])

    def test_dashboard_static_entrypoint_is_served(self) -> None:
        with urlopen(self.base_url + "/", timeout=5) as response:
            content = response.read().decode("utf-8")
        self.assertIn("Financial Engine", content)
        self.assertIn("Rules, not AI", content)

    def test_health_endpoint_checks_database(self) -> None:
        self.assertEqual(self.request("/healthz"), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
