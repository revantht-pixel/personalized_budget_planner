"""Provider-neutral AI boundary. Core operation never imports an AI SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Protocol, Sequence

from .errors import AIIntegrationDisabled
from .models import PostingInput, TransactionProposal
from .proposals import ProposalService


@dataclass(frozen=True, slots=True)
class FinancialDocument:
    id: str
    text: str
    received_at: datetime
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AIProposalDraft:
    effective_at: datetime
    description: str
    currency: str
    postings: Sequence[PostingInput]
    confidence: float | None
    merchant_name: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)


class AIProposalAdapter(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def propose_transaction(self, document: FinancialDocument) -> AIProposalDraft: ...


class DisabledAIAdapter:
    provider_name = "disabled"
    model_name = "disabled"

    def propose_transaction(self, document: FinancialDocument) -> AIProposalDraft:
        raise AIIntegrationDisabled(
            "AI is disabled; configure an adapter and explicit data-sharing consent first"
        )


class AIIntegrationService:
    """Converts adapter output into a review item; it cannot post transactions."""

    def __init__(self, proposals: ProposalService, adapter: AIProposalAdapter) -> None:
        self.proposals = proposals
        self.adapter = adapter

    def analyze(self, document: FinancialDocument) -> TransactionProposal:
        draft = self.adapter.propose_transaction(document)
        return self.proposals.submit(
            source_type=f"AI:{self.adapter.provider_name}",
            source_id=document.id,
            effective_at=draft.effective_at,
            description=draft.description,
            currency=draft.currency,
            merchant_name=draft.merchant_name,
            postings=draft.postings,
            confidence=draft.confidence,
            evidence=draft.evidence,
            provider_metadata={
                "provider": self.adapter.provider_name,
                "model": self.adapter.model_name,
            },
        )
