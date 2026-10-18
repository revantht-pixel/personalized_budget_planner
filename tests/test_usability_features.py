from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from financial_engine import merchant_key  # noqa: E402
from financial_engine.webapp import FinancialAppServer, prepare_database  # noqa: E402

CARD_CSV = "\n".join(
    [
        "Date,Description,Amount",
        "10/02/2026,AMAZON MKTPL*2K4,34.99",
        "10/07/2026,AMAZON MKTPL*8J1,18.50",
        "10/05/2026,SHELL OIL 5741,42.10",
        "10/16/2026,PAYMENT THANK YOU,-200.00",
    ]
)


class UsabilityFeatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = Path.cwd() / f".usability-test-{uuid4()}.db"
        prepare_database(self.database)
        self.server = FinancialAppServer(("127.0.0.1", 0), self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.checking = self.post(
            "/api/accounts",
            {"name": "Checking", "kind": "CHECKING", "opening_balance_minor": 500_000,
             "balance_as_of": "2026-09-01"},
        )["account"]["code"]
        self.card = self.post(
            "/api/accounts",
            {"name": "Card", "kind": "CREDIT_CARD", "opening_balance_minor": 0,
             "credit_limit_minor": 100_000, "balance_as_of": "2026-09-01"},
        )["account"]["code"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.database.unlink(missing_ok=True)

    def call(self, path: str, payload: dict[str, object] | None = None) -> tuple[int, dict]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def post(self, path: str, payload: dict[str, object]) -> dict:
        status, body = self.call(path, payload)
        self.assertLess(status, 300, body)
        return body

    def get(self, path: str) -> dict:
        status, body = self.call(path)
        self.assertEqual(status, 200, body)
        return body

    def import_card(self) -> dict:
        return self.post(
            "/api/imports",
            {"account_code": self.card, "filename": "card.csv", "csv_text": CARD_CSV,
             "amount_convention": "CHARGES_POSITIVE"},
        )["import"]

    def expense(self, **overrides: object) -> dict[str, object]:
        return {
            "kind": "expense", "amount_minor": 4_500, "effective_at": "2026-10-06",
            "description": "Sushi night", "account_code": self.checking,
            "category_code": "Expenses:Dining", **overrides,
        }

    # Merchant rules -----------------------------------------------------------

    def test_merchant_key_ignores_store_numbers_and_processor_noise(self) -> None:
        self.assertEqual(merchant_key("AMAZON MKTPL*2K4"), merchant_key("AMAZON MKTPL*8J1"))
        self.assertEqual(merchant_key("SHELL OIL 5741"), "SHELL OIL")
        self.assertEqual(merchant_key("SQ *BLUE BOTTLE COFFEE"), "BLUE BOTTLE")
        self.assertIsNone(merchant_key("#1234"))

    def test_posting_a_row_learns_a_rule_and_updates_matching_rows(self) -> None:
        rows = {row["description"]: row for row in self.import_card()["rows"]}
        first, second = rows["AMAZON MKTPL*2K4"], rows["AMAZON MKTPL*8J1"]
        self.assertEqual(second["suggested_account_code"], "Expenses:Other")

        self.post(f"/api/import-rows/{first['id']}/post", {"category_code": "Expenses:Shopping"})

        queue = {row["id"]: row for row in self.get("/api/review")["import_rows"]}
        self.assertEqual(queue[second["id"]]["suggested_account_code"], "Expenses:Shopping")
        rules = self.get("/api/rules")["rules"]
        self.assertEqual(
            [(rule["merchant_key"], rule["account_code"]) for rule in rules],
            [("AMAZON MKTPL", "Expenses:Shopping")],
        )

    def test_rules_prefill_future_imports_and_can_be_deleted(self) -> None:
        self.post("/api/transactions", self.expense(description="Shell Oil 1123",
                                                    category_code="Expenses:Transport"))
        self.assertEqual(
            self.get("/api/rules/suggest?text=SHELL%20OIL%205741")["account_code"],
            "Expenses:Transport",
        )
        rows = {row["description"]: row for row in self.import_card()["rows"]}
        self.assertEqual(rows["SHELL OIL 5741"]["suggested_account_code"], "Expenses:Transport")

        rule_id = self.get("/api/rules")["rules"][0]["id"]
        self.post(f"/api/rules/{rule_id}/delete", {})
        self.assertEqual(self.get("/api/rules")["rules"], [])

    # Bulk review --------------------------------------------------------------

    def test_bulk_review_posts_and_ignores_independently(self) -> None:
        rows = {row["description"]: row for row in self.import_card()["rows"]}
        result = self.post(
            "/api/import-rows/bulk",
            {"rows": [
                {"id": rows["AMAZON MKTPL*2K4"]["id"], "action": "post",
                 "category_code": "Expenses:Shopping"},
                {"id": rows["SHELL OIL 5741"]["id"], "action": "post",
                 "category_code": "Expenses:Transport"},
                {"id": rows["PAYMENT THANK YOU"]["id"], "action": "post",
                 "category_code": self.card},
                {"id": rows["AMAZON MKTPL*8J1"]["id"], "action": "ignore"},
            ]},
        )
        self.assertEqual((result["posted"], result["ignored"]), (2, 1))
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["id"], rows["PAYMENT THANK YOU"]["id"])
        remaining = [row["description"] for row in self.get("/api/review")["import_rows"]]
        self.assertEqual(remaining, ["PAYMENT THANK YOU"])

    # Edit and delete ---------------------------------------------------------

    def test_edit_replaces_transaction_and_hides_the_correction_pair(self) -> None:
        original = self.post("/api/transactions", self.expense())["transaction_id"]
        detail = self.get(f"/api/transactions/{original}")
        self.assertEqual(detail["edit"]["kind"], "expense")
        self.assertEqual(detail["edit"]["amount_minor"], 4_500)

        edited = self.post(
            f"/api/transactions/{original}/edit",
            self.expense(amount_minor=6_800, category_code="Expenses:Groceries"),
        )["transaction_id"]

        overview = self.get("/api/overview?month=2026-10")
        listed = [item["id"] for item in overview["transactions"]]
        self.assertIn(edited, listed)
        self.assertNotIn(original, listed)
        self.assertFalse(any(item["is_reversal"] for item in overview["transactions"]))
        spent = {item["code"]: item["spent_minor"] for item in overview["spending"]["categories"]}
        self.assertEqual(spent.get("Expenses:Groceries"), 6_800)
        self.assertEqual(spent.get("Expenses:Dining", 0), 0)
        account = self.get(f"/api/accounts/{self.checking}")["account"]
        self.assertEqual(account["ledger_balance_minor"], 500_000 - 6_800)

        status, body = self.call(f"/api/transactions/{original}/edit", self.expense())
        self.assertEqual(status, 400)
        self.assertIn("already", body["error"])

    def test_deleted_transaction_disappears_from_activity(self) -> None:
        original = self.post("/api/transactions", self.expense())["transaction_id"]
        self.post(f"/api/transactions/{original}/reverse",
                  {"reason": "Deleted by user", "effective_at": "2026-10-06"})
        listed = self.get("/api/overview?month=2026-10")["transactions"]
        self.assertEqual([item["description"] for item in listed], ["Opening balance: Checking"])
        spent = self.get("/api/overview?month=2026-10")["spending"]["spent_minor"]
        self.assertEqual(spent, 0)

    def test_opening_balances_are_not_editable(self) -> None:
        opening = self.get(f"/api/accounts/{self.checking}")["transactions"][0]["id"]
        self.assertIsNone(self.get(f"/api/transactions/{opening}")["edit"])
        status, _ = self.call(f"/api/transactions/{opening}/edit", self.expense())
        self.assertEqual(status, 400)

    def test_transaction_accounts_must_have_the_right_type(self) -> None:
        status, body = self.call("/api/transactions", self.expense(category_code=self.card))
        self.assertEqual(status, 400)
        self.assertIn("category", body["error"])
        status, _ = self.call(
            "/api/transactions",
            {"kind": "transfer", "amount_minor": 100, "effective_at": "2026-10-01",
             "description": "x", "from_account_code": self.checking,
             "to_account_code": "Equity:OpeningBalances:USD"},
        )
        self.assertEqual(status, 400)

    # Cash flow ----------------------------------------------------------------

    def test_overview_reports_monthly_cash_flow(self) -> None:
        self.post("/api/transactions", {
            "kind": "income", "amount_minor": 280_000, "effective_at": "2026-10-01",
            "description": "Stipend", "account_code": self.checking,
        })
        self.post("/api/transactions", self.expense(amount_minor=70_000))
        self.post("/api/transactions", self.expense(amount_minor=10_000, effective_at="2026-08-15"))

        flow = self.get("/api/overview?month=2026-10")["cash_flow"]
        self.assertEqual([item["month"] for item in flow],
                         ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"])
        october = flow[-1]
        self.assertEqual((october["income_minor"], october["spent_minor"], october["net_minor"]),
                         (280_000, 70_000, 210_000))
        self.assertEqual(october["savings_rate"], 0.75)
        self.assertEqual(flow[3]["net_minor"], -10_000)
        self.assertIsNone(flow[3]["savings_rate"])


if __name__ == "__main__":
    unittest.main()
