"""Domain-specific errors raised by the ledger."""


class LedgerError(Exception):
    """Base class for expected ledger failures."""


class InvalidTransaction(LedgerError):
    """The proposed journal transaction violates a ledger invariant."""


class CurrencyMismatch(InvalidTransaction):
    """A transaction mixes currencies without explicit FX handling."""


class IdempotencyConflict(LedgerError):
    """A source identifier was reused for different financial content."""


class AccountNotFound(LedgerError):
    """A posting references an unknown account code."""


class TransactionNotFound(LedgerError):
    """The requested transaction does not exist or is not posted."""


class DuplicateAccount(LedgerError):
    """An account code is already in use."""


class ProposalNotFound(LedgerError):
    """The requested transaction proposal does not exist."""


class ProposalStateError(LedgerError):
    """A proposal cannot make the requested state transition."""


class AIIntegrationDisabled(LedgerError):
    """AI was requested without an explicitly configured adapter."""


class ReconciliationMismatch(LedgerError):
    """A statement cannot be reconciled while its balance differs from the ledger."""


class StatementImportNotFound(LedgerError):
    """The requested statement import or staged row does not exist."""


class StatementImportStateError(LedgerError):
    """A staged statement row cannot make the requested transition."""
