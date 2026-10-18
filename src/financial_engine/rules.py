"""Learned merchant-to-category rules that pre-fill review suggestions.

Rules only change which category is *suggested*; every statement row still needs
an explicit review before it reaches the ledger.
"""

from __future__ import annotations

import re
from datetime import datetime
from uuid import uuid4

from .errors import AccountNotFound
from .ledger import Ledger, _utc_now_iso
from .models import AccountType, CategorizationRule

# Card-network and payment-processor words that say nothing about the merchant.
_NOISE_TOKENS = frozenset(
    {
        "POS", "DEBIT", "CREDIT", "PURCHASE", "CHECKCARD", "ACH", "RECURRING",
        "ONLINE", "PMT", "WWW", "COM", "SQ", "TST", "SP", "PY", "PAYPAL",
    }
)
_KEY_TOKENS = 2


def merchant_key(text: str | None) -> str | None:
    """Collapse a bank description into a stable merchant identity.

    ``AMAZON MKTPL*2K4`` and ``AMAZON MKTPL*8J1`` both become ``AMAZON MKTPL``;
    ``SHELL OIL 5741`` becomes ``SHELL OIL``.
    """

    if not text:
        return None
    tokens = [
        token
        for token in re.sub(r"[^A-Z0-9]+", " ", text.upper()).split()
        if not any(char.isdigit() for char in token) and token not in _NOISE_TOKENS
        and len(token) > 1
    ]
    return " ".join(tokens[:_KEY_TOKENS]) or None


def _candidate_keys(text: str | None) -> tuple[str, ...]:
    """Keys to try, most specific first: ``COSTCO RUN`` falls back to ``COSTCO``."""

    key = merchant_key(text)
    if key is None:
        return ()
    first = key.split()[0]
    return (key,) if first == key else (key, first)


class RuleService:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def suggest(self, text: str | None, *, exclude_account_id: str | None = None) -> str | None:
        """Return the account code a matching rule suggests, if any."""

        for key in _candidate_keys(text):
            row = self.ledger.connection.execute(
                """
                SELECT a.id, a.code FROM categorization_rules r
                JOIN accounts a ON a.id = r.account_id
                WHERE r.merchant_key = ? AND a.is_active = 1
                """,
                (key,),
            ).fetchone()
            if row is not None:
                return None if row["id"] == exclude_account_id else row["code"]
        return None

    def learn(self, text: str | None, account_code: str) -> CategorizationRule | None:
        """Remember that this merchant belongs in ``account_code``."""

        key = merchant_key(text)
        if key is None:
            return None
        account = self.ledger.get_account(account_code)
        if account.type is AccountType.EQUITY:
            return None
        now = _utc_now_iso()
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                INSERT INTO categorization_rules(
                    id, merchant_key, account_id, times_used, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(merchant_key) DO UPDATE SET
                    account_id = excluded.account_id,
                    times_used = CASE
                        WHEN categorization_rules.account_id = excluded.account_id
                        THEN categorization_rules.times_used + 1 ELSE 1 END,
                    updated_at = excluded.updated_at
                """,
                (str(uuid4()), key, account.id, now, now),
            )
            self._refresh_pending_suggestions(key, account.id)
        return self._get_by_key(key)

    def list_rules(self) -> tuple[CategorizationRule, ...]:
        rows = self.ledger.connection.execute(
            f"{_RULE_SELECT} ORDER BY r.merchant_key"
        ).fetchall()
        return tuple(_rule_from_row(row) for row in rows)

    def delete_rule(self, rule_id: str) -> None:
        with self.ledger._write():
            deleted = self.ledger.connection.execute(
                "DELETE FROM categorization_rules WHERE id = ?", (rule_id,)
            ).rowcount
        if not deleted:
            raise AccountNotFound(f"unknown rule: {rule_id}")

    def _get_by_key(self, key: str) -> CategorizationRule:
        row = self.ledger.connection.execute(
            f"{_RULE_SELECT} WHERE r.merchant_key = ?", (key,)
        ).fetchone()
        assert row is not None
        return _rule_from_row(row)

    def _refresh_pending_suggestions(self, key: str, account_id: str) -> None:
        """Point other unreviewed rows from the same merchant at the learned category."""

        rows = self.ledger.connection.execute(
            """
            SELECT id, description, account_id FROM statement_import_rows
            WHERE workflow_status = 'NEEDS_REVIEW'
            """
        ).fetchall()
        for row in rows:
            if row["account_id"] != account_id and key in _candidate_keys(row["description"]):
                self.ledger.connection.execute(
                    "UPDATE statement_import_rows SET suggested_account_id = ? WHERE id = ?",
                    (account_id, row["id"]),
                )


_RULE_SELECT = """
    SELECT r.id, r.merchant_key, r.times_used, r.updated_at,
           a.code AS account_code, a.name AS account_name
    FROM categorization_rules r JOIN accounts a ON a.id = r.account_id
"""


def _rule_from_row(row: object) -> CategorizationRule:
    return CategorizationRule(
        id=row["id"],
        merchant_key=row["merchant_key"],
        account_code=row["account_code"],
        account_name=row["account_name"],
        times_used=row["times_used"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
