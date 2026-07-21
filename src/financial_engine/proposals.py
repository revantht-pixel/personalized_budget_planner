"""Reviewable transaction proposals shared by rules, imports, and optional AI."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import uuid4

from .errors import IdempotencyConflict, ProposalNotFound, ProposalStateError
from .ledger import Ledger, _as_utc_iso, _optional_text, _required_text, _utc_now_iso, _validate_currency
from .models import JournalTransaction, PostingInput, ProposalStatus, TransactionProposal


class ProposalService:
    """The only route from untrusted suggestions to reviewed ledger facts."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def submit(
        self,
        *,
        source_type: str,
        source_id: str,
        effective_at: datetime,
        description: str,
        postings: Sequence[PostingInput],
        currency: str = "USD",
        merchant_name: str | None = None,
        correlation_id: str | None = None,
        confidence: float | None = None,
        evidence: Mapping[str, str] | None = None,
        provider_metadata: Mapping[str, str] | None = None,
    ) -> TransactionProposal:
        source_type = _required_text(source_type, "proposal source type").upper()
        source_id = _required_text(source_id, "proposal source id")
        description = _required_text(description, "description")
        currency = _validate_currency(currency)
        effective_at_iso = _as_utc_iso(effective_at)
        merchant_name = _optional_text(merchant_name)
        correlation_id = _optional_text(correlation_id)
        postings = tuple(postings)
        self.ledger._validate_posting_inputs(postings)
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ValueError("confidence must be between 0 and 1")
        evidence = _clean_string_mapping(evidence or {}, "evidence")
        provider_metadata = _clean_string_mapping(
            provider_metadata or {}, "provider metadata"
        )

        payload = {
            "effective_at": effective_at_iso,
            "description": description,
            "currency": currency,
            "merchant_name": merchant_name,
            "correlation_id": correlation_id,
            "postings": [
                {
                    "account_code": item.account_code,
                    "amount_minor": item.amount_minor,
                    "memo": _optional_text(item.memo),
                    "tags": dict(sorted(item.tags.items())),
                }
                for item in postings
            ],
        }
        canonical = json.dumps(
            {
                "source_type": source_type,
                "source_id": source_id,
                "payload": payload,
                "evidence": evidence,
                "provider_metadata": provider_metadata,
                "confidence": confidence,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        proposal_id = str(uuid4())
        created_at = _utc_now_iso()

        with self.ledger._write():
            existing = self.ledger.connection.execute(
                """
                SELECT id, content_hash FROM transaction_proposals
                WHERE source_type = ? AND source_id = ?
                """,
                (source_type, source_id),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise IdempotencyConflict(
                        f"proposal source {source_type}:{source_id} has different content"
                    )
                return self.get(existing["id"])
            self.ledger.connection.execute(
                """
                INSERT INTO transaction_proposals(
                    id, source_type, source_id, status, confidence, payload_json,
                    evidence_json, provider_metadata_json, content_hash, created_at
                ) VALUES (?, ?, ?, 'NEEDS_REVIEW', ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    source_type,
                    source_id,
                    float(confidence) if confidence is not None else None,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                    json.dumps(provider_metadata, sort_keys=True, separators=(",", ":")),
                    content_hash,
                    created_at,
                ),
            )
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> TransactionProposal:
        row = self.ledger.connection.execute(
            "SELECT * FROM transaction_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise ProposalNotFound(f"unknown proposal: {proposal_id}")
        return _proposal_from_row(row)

    def review_queue(self, *, limit: int = 100) -> tuple[TransactionProposal, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        rows = self.ledger.connection.execute(
            """
            SELECT id FROM transaction_proposals
            WHERE status = 'NEEDS_REVIEW'
            ORDER BY created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return tuple(self.get(row["id"]) for row in rows)

    def approve(
        self, proposal_id: str, *, reviewed_by: str, note: str | None = None
    ) -> JournalTransaction:
        proposal = self.get(proposal_id)
        if proposal.status is ProposalStatus.APPROVED:
            assert proposal.approved_transaction_id is not None
            return self.ledger.get_transaction(proposal.approved_transaction_id)
        if proposal.status is ProposalStatus.REJECTED:
            raise ProposalStateError("a rejected proposal cannot be approved")
        reviewed_by = _required_text(reviewed_by, "reviewer")
        transaction = self.ledger.post_transaction(
            effective_at=proposal.effective_at,
            description=proposal.description,
            currency=proposal.currency,
            merchant_name=proposal.merchant_name,
            source_type="PROPOSAL",
            source_id=proposal.id,
            correlation_id=proposal.correlation_id,
            postings=proposal.postings,
        )
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                UPDATE transaction_proposals
                SET status = 'APPROVED', approved_transaction_id = ?, reviewed_by = ?,
                    review_note = ?, reviewed_at = ?
                WHERE id = ? AND status = 'NEEDS_REVIEW'
                """,
                (transaction.id, reviewed_by, _optional_text(note), _utc_now_iso(), proposal.id),
            )
        return transaction

    def reject(self, proposal_id: str, *, reviewed_by: str, note: str) -> TransactionProposal:
        proposal = self.get(proposal_id)
        if proposal.status is ProposalStatus.APPROVED:
            raise ProposalStateError("an approved proposal cannot be rejected; reverse its transaction")
        if proposal.status is ProposalStatus.REJECTED:
            return proposal
        reviewed_by = _required_text(reviewed_by, "reviewer")
        note = _required_text(note, "rejection note")
        with self.ledger._write():
            self.ledger.connection.execute(
                """
                UPDATE transaction_proposals
                SET status = 'REJECTED', reviewed_by = ?, review_note = ?, reviewed_at = ?
                WHERE id = ? AND status = 'NEEDS_REVIEW'
                """,
                (reviewed_by, note, _utc_now_iso(), proposal.id),
            )
        return self.get(proposal.id)


def _clean_string_mapping(values: Mapping[str, str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label} keys must be non-empty strings")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} values must be non-empty strings")
        result[key.strip()] = value.strip()
    return result


def _proposal_from_row(row: object) -> TransactionProposal:
    payload = json.loads(row["payload_json"])
    return TransactionProposal(
        id=row["id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        status=ProposalStatus(row["status"]),
        confidence=row["confidence"],
        effective_at=datetime.fromisoformat(payload["effective_at"]),
        description=payload["description"],
        currency=payload["currency"],
        merchant_name=payload["merchant_name"],
        correlation_id=payload["correlation_id"],
        postings=tuple(
            PostingInput(
                account_code=item["account_code"],
                amount_minor=item["amount_minor"],
                memo=item["memo"],
                tags=item["tags"],
            )
            for item in payload["postings"]
        ),
        evidence=json.loads(row["evidence_json"]),
        provider_metadata=json.loads(row["provider_metadata_json"]),
        approved_transaction_id=row["approved_transaction_id"],
        reviewed_by=row["reviewed_by"],
        review_note=row["review_note"],
        created_at=datetime.fromisoformat(row["created_at"]),
        reviewed_at=(
            datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None
        ),
    )

