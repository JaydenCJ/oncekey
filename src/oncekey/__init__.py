"""oncekey — idempotency keys and a SQLite ledger for agent tool effects.

Wrap a side-effecting tool with :func:`once` and a retried call replays the
recorded result instead of re-sending the email or re-charging the card. The
ledger is a single local SQLite file, queryable with the ``oncekey`` CLI.

Quick taste::

    from oncekey import Ledger, once

    ledger = Ledger("effects.db")

    @once(ledger, tool="send_email")
    def send_email(to: str, subject: str) -> dict:
        ...  # the actual send

    send_email("ops@example.test", "hi")  # executes
    send_email("ops@example.test", "hi")  # replays; nothing is sent twice
"""

from .errors import (
    EntryNotFoundError,
    InFlightError,
    KeyConflictError,
    KeyDerivationError,
    LeaseLostError,
    LedgerError,
    OnceKeyError,
    PreviouslyFailedError,
    ResultUnavailableError,
)
from .keys import bind_args, canonical_json, derive_key, fingerprint, select_fields
from .ledger import DEFAULT_LEASE_SECONDS, SCHEMA_VERSION, Ledger
from .models import COMMITTED, FAILED, IN_FLIGHT, STATUSES, Claim, Entry
from .wrapper import once, wrap_tool, wrap_toolkit

__version__ = "0.1.0"

__all__ = [
    "COMMITTED",
    "Claim",
    "DEFAULT_LEASE_SECONDS",
    "Entry",
    "EntryNotFoundError",
    "FAILED",
    "IN_FLIGHT",
    "InFlightError",
    "KeyConflictError",
    "KeyDerivationError",
    "LeaseLostError",
    "Ledger",
    "LedgerError",
    "OnceKeyError",
    "PreviouslyFailedError",
    "ResultUnavailableError",
    "SCHEMA_VERSION",
    "STATUSES",
    "__version__",
    "bind_args",
    "canonical_json",
    "derive_key",
    "fingerprint",
    "once",
    "select_fields",
    "wrap_tool",
    "wrap_toolkit",
]
