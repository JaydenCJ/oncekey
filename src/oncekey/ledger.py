"""The SQLite effect ledger: claim, commit, fail, replay, query.

Design notes
------------

* **One row per idempotency key.** The row is the whole truth about an
  effect: its status, its attempt count, the recorded result, and how many
  duplicate calls were suppressed by replaying it.
* **Leases instead of locks.** Claiming a key writes an ``in_flight`` row
  with an owner and a lease deadline. A concurrent duplicate sees the live
  lease and gets :class:`InFlightError`; a *crashed* attempt is taken over
  once its lease expires, so a wedged process can never block an effect
  forever. Commit and fail verify ownership and raise
  :class:`LeaseLostError` when the lease was stolen in the meantime — the
  honest signal that the effect may have run more than once.
* **Cross-process safety comes from SQLite.** Every state transition runs in
  a ``BEGIN IMMEDIATE`` transaction, so two processes claiming the same key
  serialize on the database write lock; in-process threads additionally
  share one connection behind an ``RLock``.
* **The clock is injectable** (``clock=time.time``) so lease expiry and TTL
  behavior are deterministically testable without sleeping.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from . import models
from .errors import (
    EntryNotFoundError,
    InFlightError,
    KeyConflictError,
    LeaseLostError,
    LedgerError,
    PreviouslyFailedError,
)
from .keys import fingerprint as _fingerprint
from .models import Claim, Entry

__all__ = ["DEFAULT_LEASE_SECONDS", "Ledger", "SCHEMA_VERSION"]

SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 60.0
_MAX_ERROR_CHARS = 4000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    key              TEXT PRIMARY KEY,
    tool             TEXT NOT NULL,
    fingerprint      TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('in_flight', 'committed', 'failed')),
    attempts         INTEGER NOT NULL DEFAULT 1,
    replays          INTEGER NOT NULL DEFAULT 0,
    args_json        TEXT,
    result_json      TEXT,
    error            TEXT,
    owner            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    committed_at     REAL,
    lease_expires_at REAL,
    expires_at       REAL,
    duration_ms      REAL
);
CREATE INDEX IF NOT EXISTS idx_effects_tool ON effects (tool);
CREATE INDEX IF NOT EXISTS idx_effects_status ON effects (status);
CREATE INDEX IF NOT EXISTS idx_effects_created ON effects (created_at);
"""

_COLUMNS = (
    "key, tool, fingerprint, status, attempts, replays, args_json, result_json, "
    "error, owner, created_at, updated_at, committed_at, lease_expires_at, "
    "expires_at, duration_ms"
)


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        key=row["key"],
        tool=row["tool"],
        fingerprint=row["fingerprint"],
        status=row["status"],
        attempts=row["attempts"],
        replays=row["replays"],
        args_json=row["args_json"],
        result_json=row["result_json"],
        error=row["error"],
        owner=row["owner"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        committed_at=row["committed_at"],
        lease_expires_at=row["lease_expires_at"],
        expires_at=row["expires_at"],
        duration_ms=row["duration_ms"],
    )


class Ledger:
    """A local, queryable, exactly-once effect ledger backed by SQLite.

    ``path`` may be a filesystem path or ``":memory:"`` (tests). The ledger
    is safe to share across threads and to open from multiple processes
    pointing at the same file.
    """

    def __init__(
        self,
        path: str = "oncekey.db",
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
        owner: Optional[str] = None,
    ) -> None:
        if lease_seconds <= 0:
            raise LedgerError(f"lease_seconds must be positive, got {lease_seconds!r}")
        self.path = str(path)
        self.lease_seconds = float(lease_seconds)
        self.owner = owner or f"pid{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                self.path, check_same_thread=False, isolation_level=None, timeout=10.0
            )
        except sqlite3.Error as exc:  # e.g. unreadable path
            raise LedgerError(f"cannot open ledger at {self.path!r}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ setup

    def _init_schema(self) -> None:
        # Runs in autocommit mode: executescript issues an implicit COMMIT,
        # so schema setup must not sit inside an explicit transaction.
        try:
            with self._lock:
                conn = self._conn
                if self.path != ":memory:":
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.executescript(_SCHEMA)
                row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO meta (k, v) VALUES ('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(row["v"]) > SCHEMA_VERSION:
                    raise LedgerError(
                        f"ledger {self.path!r} has schema version {row['v']}, but this "
                        f"oncekey only supports up to {SCHEMA_VERSION}; upgrade oncekey"
                    )
        except sqlite3.DatabaseError as exc:
            raise LedgerError(f"{self.path!r} is not a usable ledger database: {exc}") from exc

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        """One serialized, immediate-write transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------- lifecycle

    def claim(
        self,
        key: str,
        tool: str,
        fp: str,
        *,
        args_json: Optional[str] = None,
        ttl: Optional[float] = None,
        retry_failed: bool = True,
    ) -> Claim:
        """Claim ``key`` for execution, or learn that it must be replayed.

        Exactly one of the following happens, atomically:

        * no entry (or an expired one) exists → an ``in_flight`` row is
          written and the claim says **execute**;
        * a committed entry with the same fingerprint exists → its replay
          counter is bumped and the claim says **replay**;
        * a committed entry exists but its result was unrecordable →
          :class:`ResultUnavailableError` (the effect will not run again);
        * the fingerprint differs → :class:`KeyConflictError`;
        * a live lease is held elsewhere → :class:`InFlightError`;
        * an expired lease is found → it is taken over (execute);
        * a failed entry exists → retried (execute), unless
          ``retry_failed=False`` → :class:`PreviouslyFailedError`.
        """
        now = self._clock()
        expires_at = (now + ttl) if ttl is not None else None
        with self._txn() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM effects WHERE key = ?", (key,)).fetchone()
            if row is not None:
                entry = _row_to_entry(row)
                # A TTL'd entry that has lapsed is treated as absent: the
                # effect's dedup window is over and it may run again.
                if entry.status != models.IN_FLIGHT and entry.is_expired(now):
                    conn.execute("DELETE FROM effects WHERE key = ?", (key,))
                    row = None
            if row is None:
                conn.execute(
                    "INSERT INTO effects (key, tool, fingerprint, status, attempts, replays,"
                    " args_json, owner, created_at, updated_at, lease_expires_at, expires_at)"
                    " VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        tool,
                        fp,
                        models.IN_FLIGHT,
                        args_json,
                        self.owner,
                        now,
                        now,
                        now + self.lease_seconds,
                        expires_at,
                    ),
                )
                return Claim(models.EXECUTE, self._fetch(conn, key))

            if entry.fingerprint != fp:
                raise KeyConflictError(key, stored=entry.fingerprint, attempted=fp, tool=entry.tool)

            if entry.status == models.COMMITTED:
                if entry.result_json is None:
                    # Committed but the result had no JSON form: refuse to
                    # replay AND refuse to re-execute. See ResultUnavailableError.
                    entry.result()  # raises ResultUnavailableError
                conn.execute(
                    "UPDATE effects SET replays = replays + 1, updated_at = ? WHERE key = ?",
                    (now, key),
                )
                return Claim(models.REPLAY, self._fetch(conn, key))

            if entry.status == models.IN_FLIGHT:
                if entry.lease_expires_at is not None and entry.lease_expires_at > now:
                    raise InFlightError(
                        key, owner=entry.owner, lease_expires_at=entry.lease_expires_at
                    )
                # Lease expired: the previous attempt crashed or stalled.
                # Take the lease over and run the effect ourselves.
                return self._reclaim(conn, key, now, expires_at)

            # status == FAILED
            if not retry_failed:
                raise PreviouslyFailedError(key, error=entry.error, attempts=entry.attempts)
            return self._reclaim(conn, key, now, expires_at)

    def _reclaim(
        self, conn: sqlite3.Connection, key: str, now: float, expires_at: Optional[float]
    ) -> Claim:
        """Take an existing row for a fresh attempt (expired lease or failed retry)."""
        conn.execute(
            "UPDATE effects SET status = ?, attempts = attempts + 1, owner = ?,"
            " updated_at = ?, lease_expires_at = ?, expires_at = ? WHERE key = ?",
            (models.IN_FLIGHT, self.owner, now, now + self.lease_seconds, expires_at, key),
        )
        return Claim(models.EXECUTE, self._fetch(conn, key))

    def commit(
        self, key: str, *, result_json: Optional[str], duration_ms: Optional[float] = None
    ) -> Entry:
        """Record success. ``result_json`` must be canonical JSON (or ``None``
        when the result had no JSON form and replays should refuse)."""
        now = self._clock()
        with self._txn() as conn:
            self._check_owned(conn, key)
            conn.execute(
                "UPDATE effects SET status = ?, result_json = ?, error = NULL, owner = NULL,"
                " lease_expires_at = NULL, committed_at = ?, updated_at = ?, duration_ms = ?"
                " WHERE key = ?",
                (models.COMMITTED, result_json, now, now, duration_ms, key),
            )
            return self._fetch(conn, key)

    def fail(self, key: str, error: str) -> Entry:
        """Record a failed attempt; the effect stays retryable by default."""
        now = self._clock()
        with self._txn() as conn:
            self._check_owned(conn, key)
            conn.execute(
                "UPDATE effects SET status = ?, error = ?, owner = NULL,"
                " lease_expires_at = NULL, updated_at = ? WHERE key = ?",
                (models.FAILED, str(error)[:_MAX_ERROR_CHARS], now, key),
            )
            return self._fetch(conn, key)

    def _check_owned(self, conn: sqlite3.Connection, key: str) -> None:
        row = conn.execute(
            "SELECT status, owner FROM effects WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise LeaseLostError(key, expected_owner=self.owner, actual_owner=None, status="absent")
        if row["status"] != models.IN_FLIGHT or row["owner"] != self.owner:
            raise LeaseLostError(
                key, expected_owner=self.owner, actual_owner=row["owner"], status=row["status"]
            )

    def _fetch(self, conn: sqlite3.Connection, key: str) -> Entry:
        row = conn.execute(f"SELECT {_COLUMNS} FROM effects WHERE key = ?", (key,)).fetchone()
        assert row is not None  # only called inside a transaction that saw the row
        return _row_to_entry(row)

    # ----------------------------------------------------------------- query

    def get(self, key: str) -> Optional[Entry]:
        """Return the entry for ``key``, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE key = ?", (key,)
            ).fetchone()
        return _row_to_entry(row) if row else None

    def require(self, key: str) -> Entry:
        """Like :meth:`get`, but ``key`` may be a unique prefix; raises
        :class:`EntryNotFoundError` on zero or ambiguous matches."""
        entry = self.get(key)
        if entry is not None:
            return entry
        with self._lock:
            # The ESCAPE clause keeps user underscores and percents literal.
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM effects WHERE key LIKE ? ESCAPE '\\'"
                " ORDER BY key LIMIT 3",
                (key.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%",),
            ).fetchall()
        if len(rows) == 1:
            return _row_to_entry(rows[0])
        if len(rows) > 1:
            candidates = ", ".join(sorted(r["key"] for r in rows))
            raise EntryNotFoundError(f"{key} (ambiguous prefix; candidates: {candidates})")
        raise EntryNotFoundError(key)

    def entries(
        self,
        *,
        tool: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Entry]:
        """List entries, newest first, optionally filtered."""
        clauses, params = [], []  # type: List[str], List[Any]
        if tool is not None:
            clauses.append("tool = ?")
            params.append(tool)
        if status is not None:
            if status not in models.STATUSES:
                raise LedgerError(
                    f"unknown status {status!r}; expected one of {', '.join(models.STATUSES)}"
                )
            clauses.append("status = ?")
            params.append(status)
        sql = f"SELECT {_COLUMNS} FROM effects"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, key"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def stats(self) -> Dict[str, Any]:
        """Aggregate counts: totals plus a per-tool breakdown.

        ``replays`` is the number of duplicate calls that were answered from
        the ledger instead of re-executing — i.e. double-sends prevented.
        """
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) AS entries,"
                " COALESCE(SUM(status = 'committed'), 0) AS committed,"
                " COALESCE(SUM(status = 'failed'), 0) AS failed,"
                " COALESCE(SUM(status = 'in_flight'), 0) AS in_flight,"
                " COALESCE(SUM(replays), 0) AS replays,"
                " COALESCE(SUM(attempts), 0) AS attempts"
                " FROM effects"
            ).fetchone()
            per_tool = self._conn.execute(
                "SELECT tool, COUNT(*) AS entries,"
                " COALESCE(SUM(status = 'committed'), 0) AS committed,"
                " COALESCE(SUM(status = 'failed'), 0) AS failed,"
                " COALESCE(SUM(status = 'in_flight'), 0) AS in_flight,"
                " COALESCE(SUM(replays), 0) AS replays"
                " FROM effects GROUP BY tool ORDER BY tool"
            ).fetchall()
        return {
            "entries": totals["entries"],
            "committed": totals["committed"],
            "failed": totals["failed"],
            "in_flight": totals["in_flight"],
            "replays": totals["replays"],
            "attempts": totals["attempts"],
            "tools": [dict(row) for row in per_tool],
        }

    # ------------------------------------------------------------ management

    def purge(
        self,
        *,
        older_than: Optional[float] = None,
        status: Optional[str] = None,
        expired: bool = False,
    ) -> int:
        """Delete entries and return how many were removed.

        ``older_than`` is an age in seconds (measured against ``updated_at``);
        ``status`` restricts to one status; ``expired=True`` removes entries
        whose TTL has lapsed. With no filters at all, nothing is deleted —
        wiping a ledger must be an explicit decision (pass ``status`` and/or
        ``older_than=0``).
        """
        now = self._clock()
        clauses, params = [], []  # type: List[str], List[Any]
        if older_than is not None:
            clauses.append("updated_at <= ?")
            params.append(now - older_than)
        if status is not None:
            if status not in models.STATUSES:
                raise LedgerError(
                    f"unknown status {status!r}; expected one of {', '.join(models.STATUSES)}"
                )
            clauses.append("status = ?")
            params.append(status)
        if expired:
            clauses.append("expires_at IS NOT NULL AND expires_at <= ?")
            params.append(now)
        if not clauses:
            return 0
        with self._txn() as conn:
            cursor = conn.execute(
                "DELETE FROM effects WHERE " + " AND ".join(clauses), params
            )
            return cursor.rowcount

    def resolve(
        self,
        key: str,
        *,
        action: str,
        result_json: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Optional[Entry]:
        """Manually settle an entry (the human override behind ``oncekey resolve``).

        ``action`` is ``"commit"`` (mark done, optionally recording a result),
        ``"fail"`` (mark failed, recording ``error``), or ``"discard"``
        (delete the entry so the effect may run again).
        """
        now = self._clock()
        with self._txn() as conn:
            row = conn.execute("SELECT key FROM effects WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise EntryNotFoundError(key)
            if action == "discard":
                conn.execute("DELETE FROM effects WHERE key = ?", (key,))
                return None
            if action == "commit":
                conn.execute(
                    "UPDATE effects SET status = ?, result_json = ?, error = NULL,"
                    " owner = NULL, lease_expires_at = NULL, committed_at = ?, updated_at = ?"
                    " WHERE key = ?",
                    (models.COMMITTED, result_json, now, now, key),
                )
                return self._fetch(conn, key)
            if action == "fail":
                conn.execute(
                    "UPDATE effects SET status = ?, error = ?, owner = NULL,"
                    " lease_expires_at = NULL, updated_at = ? WHERE key = ?",
                    (models.FAILED, (error or "resolved as failed")[:_MAX_ERROR_CHARS], now, key),
                )
                return self._fetch(conn, key)
            raise LedgerError(f"unknown resolve action {action!r}; use commit, fail, or discard")

    def verify(self) -> List[str]:
        """Cross-check every entry; return a list of human-readable issues.

        An empty list means the ledger is internally consistent. Issues cover
        undecodable JSON, fingerprints that no longer match the stored
        arguments, committed entries missing a commit timestamp, and stale
        in-flight leases (a crashed attempt nobody has retried yet).
        """
        issues: List[str] = []
        now = self._clock()
        for entry in self.entries():
            prefix = f"{entry.key}:"
            for label, blob in (("args_json", entry.args_json), ("result_json", entry.result_json)):
                if blob is not None:
                    try:
                        json.loads(blob)
                    except ValueError:
                        issues.append(f"{prefix} {label} is not valid JSON")
            if entry.args_json is not None:
                try:
                    recomputed = _fingerprint(json.loads(entry.args_json))
                    if recomputed != entry.fingerprint:
                        issues.append(
                            f"{prefix} fingerprint mismatch (stored {entry.fingerprint[:12]}..., "
                            f"recomputed {recomputed[:12]}...)"
                        )
                except Exception:
                    pass  # already reported as invalid JSON above
            if entry.status == models.COMMITTED and entry.committed_at is None:
                issues.append(f"{prefix} committed but has no committed_at timestamp")
            if entry.status == models.IN_FLIGHT and (
                entry.lease_expires_at is None or entry.lease_expires_at <= now
            ):
                issues.append(
                    f"{prefix} stale in-flight lease (crashed attempt?); "
                    f"next claim will take it over"
                )
        return issues

    def export(self) -> Iterator[Dict[str, Any]]:
        """Yield every entry as a plain dict, newest first (for JSONL export)."""
        for entry in self.entries():
            yield entry.to_dict()
