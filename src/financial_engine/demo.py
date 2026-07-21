"""Executable split-tender example from the architecture plan."""

from datetime import datetime, timezone

from .ledger import Ledger
from .models import AccountType, PostingInput


def main() -> None:
    with Ledger() as ledger:
        ledger.initialize()
        ledger.create_account(
            code="Expenses:Clothing", name="Clothing", account_type=AccountType.EXPENSE
        )
        ledger.create_account(
            code="Assets:GiftCard", name="Gift card", account_type=AccountType.ASSET
        )
        ledger.create_account(
            code="Liabilities:CreditCard",
            name="Credit card",
            account_type=AccountType.LIABILITY,
        )

        transaction = ledger.post_transaction(
            effective_at=datetime.now(timezone.utc),
            description="Split-tender clothing purchase",
            merchant_name="Example Store",
            source_type="DEMO",
            source_id="split-tender-1",
            postings=(
                PostingInput("Expenses:Clothing", 10_000, tags={"budget": "clothing"}),
                PostingInput("Assets:GiftCard", -2_000),
                PostingInput("Liabilities:CreditCard", -8_000),
            ),
        )

        print(f"Posted {transaction.id}: {transaction.description}")
        for posting in transaction.postings:
            print(f"  {posting.account_code:30} {posting.amount_minor / 100:9.2f}")


if __name__ == "__main__":
    main()

