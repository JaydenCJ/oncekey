"""Exception hierarchy for oncekey.

Every exception raised by this package derives from :class:`OnceKeyError`, so
callers can catch one type at the tool boundary. The subclasses are precise on
purpose: an agent runtime should treat "someone else is doing this right now"
(:class:`InFlightError`) very differently from "this key was already used with
different arguments" (:class:`KeyConflictError`), and both differently from
"this effect failed before and needs a human" (:class:`PreviouslyFailedError`).
"""

from __future__ import annotations

from typing import Optional


class OnceKeyError(Exception):
    """Base class for all oncekey errors."""


class KeyDerivationError(OnceKeyError):
    """Arguments (or a result) could not be canonicalized to JSON.

    Raised *before* the wrapped tool executes when its arguments contain
    values that have no stable JSON form (sets, NaN, custom objects, circular
    references, non-string dict keys). Failing early keeps a bad key from
    silently deduplicating unrelated calls.
    """


class KeyConflictError(OnceKeyError):
    """The same idempotency key was reused with different arguments.

    This almost always means a bug: an explicit key (an order id, a request
    id) was attached to two logically different effects. oncekey refuses to
    either replay or re-execute, because both answers would be wrong.
    """

    def __init__(self, key: str, *, stored: str, attempted: str, tool: str):
        self.key = key
        self.stored_fingerprint = stored
        self.attempted_fingerprint = attempted
        self.tool = tool
        super().__init__(
            f"idempotency key {key!r} (tool {tool!r}) was already used with "
            f"different arguments: stored fingerprint {stored[:12]}..., "
            f"attempted {attempted[:12]}...; refusing to replay or re-execute"
        )


class InFlightError(OnceKeyError):
    """Another attempt currently holds the lease for this key.

    Either a concurrent process is executing the effect right now, or a
    previous attempt crashed and its lease has not expired yet. Retrying
    after ``lease_expires_at`` will take the lease over; ``oncekey resolve``
    lets a human settle it sooner.
    """

    def __init__(self, key: str, *, owner: Optional[str], lease_expires_at: Optional[float]):
        self.key = key
        self.owner = owner
        self.lease_expires_at = lease_expires_at
        super().__init__(
            f"effect {key!r} is in flight (owner {owner!r}); lease expires at "
            f"{lease_expires_at}; retry after expiry or settle it with `oncekey resolve`"
        )


class PreviouslyFailedError(OnceKeyError):
    """A previous attempt failed and automatic retries are disabled.

    Raised by ``once(..., retry_failed=False)`` wrappers. Use it for tools
    that are not atomic — where "the call raised" does not prove "the effect
    did not happen" — and resolve the entry manually with ``oncekey resolve``.
    """

    def __init__(self, key: str, *, error: Optional[str], attempts: int):
        self.key = key
        self.error = error
        self.attempts = attempts
        super().__init__(
            f"effect {key!r} failed on a previous attempt ({attempts} so far: "
            f"{error}); retry_failed=False, resolve it with `oncekey resolve`"
        )


class LeaseLostError(OnceKeyError):
    """The attempt's lease was taken over before it could commit.

    The effect DID execute in this process, but another attempt stole the
    expired lease in the meantime, so the ledger no longer belongs to us.
    This is the honest signal of a near-miss double execution: log it, alert
    on it, and lengthen the lease if it fires in practice.
    """

    def __init__(self, key: str, *, expected_owner: str, actual_owner: Optional[str], status: str):
        self.key = key
        self.expected_owner = expected_owner
        self.actual_owner = actual_owner
        self.status = status
        super().__init__(
            f"lease for {key!r} was lost: this attempt is {expected_owner!r} but the "
            f"entry is now {status} owned by {actual_owner!r}; the effect may have "
            f"executed more than once"
        )


class ResultUnavailableError(OnceKeyError):
    """The effect committed, but its result was not JSON-serializable.

    The original call returned the live object; replays cannot. The effect
    will NOT run again — inspect the entry with ``oncekey show`` and decide.
    """

    def __init__(self, key: str):
        self.key = key
        super().__init__(
            f"effect {key!r} is committed but its result was not recorded "
            f"(not JSON-serializable); refusing to re-execute"
        )


class EntryNotFoundError(OnceKeyError):
    """No ledger entry exists for the given key."""

    def __init__(self, key: str):
        self.key = key
        super().__init__(f"no ledger entry for key {key!r}")


class LedgerError(OnceKeyError):
    """The ledger database is unusable (corrupt, or from a newer oncekey)."""
