"""Data model for ledger entries.

One :class:`Entry` per idempotency key, mirroring one row of the ``effects``
table. Entries are immutable snapshots: mutating the ledger goes through
:class:`oncekey.ledger.Ledger`, never through an ``Entry``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from .errors import ResultUnavailableError

__all__ = ["COMMITTED", "FAILED", "IN_FLIGHT", "STATUSES", "Claim", "Entry"]

IN_FLIGHT = "in_flight"
COMMITTED = "committed"
FAILED = "failed"
STATUSES = (IN_FLIGHT, COMMITTED, FAILED)

#: Claim actions returned by :meth:`oncekey.ledger.Ledger.claim`.
EXECUTE = "execute"
REPLAY = "replay"


@dataclass(frozen=True)
class Entry:
    """An immutable snapshot of one effect record.

    Timestamps are Unix epoch seconds (floats). ``result_json`` is the
    canonical JSON of the recorded result; ``None`` means the effect committed
    but its result had no JSON form, so replays must refuse
    (:class:`ResultUnavailableError`) rather than invent a value.
    """

    key: str
    tool: str
    fingerprint: str
    status: str
    attempts: int
    replays: int
    args_json: Optional[str]
    result_json: Optional[str]
    error: Optional[str]
    owner: Optional[str]
    created_at: float
    updated_at: float
    committed_at: Optional[float]
    lease_expires_at: Optional[float]
    expires_at: Optional[float]
    duration_ms: Optional[float]

    def result(self) -> Any:
        """Decode the recorded result, or raise :class:`ResultUnavailableError`."""
        if self.result_json is None:
            raise ResultUnavailableError(self.key)
        return json.loads(self.result_json)

    def args(self) -> Optional[Any]:
        """Decode the recorded (key-relevant) arguments, if any were stored."""
        if self.args_json is None:
            return None
        return json.loads(self.args_json)

    def is_expired(self, now: float) -> bool:
        """True when the entry has a TTL and it has elapsed."""
        return self.expires_at is not None and self.expires_at <= now

    def to_dict(self) -> Dict[str, Any]:
        """Plain-dict form, suitable for ``json.dumps`` (used by ``export``)."""
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    """The outcome of claiming a key: what the caller must do next.

    ``action`` is ``"execute"`` when this attempt owns the lease and must run
    the tool (then :meth:`~oncekey.ledger.Ledger.commit` or
    :meth:`~oncekey.ledger.Ledger.fail`), or ``"replay"`` when a committed
    result exists and must be served instead of re-running the effect.
    """

    action: str
    entry: Entry

    @property
    def replayed(self) -> bool:
        return self.action == REPLAY
