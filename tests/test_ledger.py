"""The SQLite ledger: claim/commit/fail lifecycle, leases, TTLs, queries.

These tests drive the ledger directly (no wrapper) with an injected clock,
so lease expiry and TTL windows are exercised without a single sleep.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from oncekey import (
    EntryNotFoundError,
    InFlightError,
    KeyConflictError,
    LeaseLostError,
    Ledger,
    LedgerError,
    PreviouslyFailedError,
    ResultUnavailableError,
    fingerprint,
)

FP = fingerprint({"to": "a@example.test"})
ARGS = '{"to":"a@example.test"}'


def _claim(ledger, key="send:k1", **kw):
    return ledger.claim(key, "send", FP, args_json=ARGS, **kw)


def test_first_claim_executes_and_writes_in_flight(ledger):
    claim = _claim(ledger)
    assert claim.action == "execute"
    assert not claim.replayed
    assert claim.entry.status == "in_flight"
    assert claim.entry.attempts == 1
    assert claim.entry.owner == ledger.owner


def test_commit_records_result_and_duration(ledger):
    _claim(ledger)
    entry = ledger.commit("send:k1", result_json='{"id":1}', duration_ms=12.5)
    assert entry.status == "committed"
    assert entry.result() == {"id": 1}
    assert entry.duration_ms == 12.5
    assert entry.committed_at == ledger._clock()
    assert entry.owner is None  # the lease is released on commit


def test_second_claim_replays_committed_result(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json='{"id":1}')
    claim = _claim(ledger)
    assert claim.replayed
    assert claim.entry.result() == {"id": 1}
    assert claim.entry.replays == 1


def test_committed_null_json_result_replays_as_none(ledger):
    # A tool returning None is a real, recordable result ("null"), distinct
    # from an unrecordable one (result_json=None). Each replay also bumps
    # the suppression counter.
    _claim(ledger)
    ledger.commit("send:k1", result_json="null")
    for expected in (1, 2, 3):
        claim = _claim(ledger)
        assert claim.entry.result() is None
        assert claim.entry.replays == expected


def test_claim_with_different_fingerprint_raises_conflict(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json='{"id":1}')
    other = fingerprint({"to": "b@example.test"})
    with pytest.raises(KeyConflictError) as excinfo:
        ledger.claim("send:k1", "send", other)
    assert excinfo.value.stored_fingerprint == FP
    assert excinfo.value.attempted_fingerprint == other


def test_concurrent_claim_sees_in_flight_with_owner(ledger):
    _claim(ledger)
    with pytest.raises(InFlightError) as excinfo:
        _claim(ledger)
    assert excinfo.value.owner == ledger.owner
    assert excinfo.value.lease_expires_at == ledger._clock() + 60.0


def test_expired_lease_is_taken_over(ledger, clock):
    _claim(ledger)
    clock.advance(61)  # past lease_seconds=60: the first attempt "crashed"
    claim = _claim(ledger)
    assert claim.action == "execute"
    assert claim.entry.attempts == 2


def test_commit_after_lease_stolen_raises_lease_lost(db_path, clock):
    a = Ledger(db_path, clock=clock, owner="attempt-a")
    b = Ledger(db_path, clock=clock, owner="attempt-b")
    a.claim("send:k1", "send", FP)
    clock.advance(61)
    b.claim("send:k1", "send", FP)  # takes the expired lease over
    with pytest.raises(LeaseLostError) as excinfo:
        a.commit("send:k1", result_json="1")
    assert excinfo.value.actual_owner == "attempt-b"
    b.commit("send:k1", result_json="1")  # the rightful owner commits fine
    a.close(), b.close()


def test_fail_records_error_and_releases_lease(ledger):
    _claim(ledger)
    entry = ledger.fail("send:k1", "TimeoutError: upstream died")
    assert entry.status == "failed"
    assert entry.error == "TimeoutError: upstream died"
    assert entry.owner is None


def test_failed_effect_is_retryable_by_default(ledger):
    _claim(ledger)
    ledger.fail("send:k1", "boom")
    claim = _claim(ledger)
    assert claim.action == "execute"
    assert claim.entry.attempts == 2


def test_retry_failed_false_raises_with_stored_error(ledger):
    _claim(ledger)
    ledger.fail("send:k1", "boom")
    with pytest.raises(PreviouslyFailedError) as excinfo:
        _claim(ledger, retry_failed=False)
    assert excinfo.value.error == "boom"
    assert excinfo.value.attempts == 1


def test_failed_entry_with_different_fingerprint_still_conflicts(ledger):
    # Retrying a failed key with *different* arguments is a caller bug, not a
    # retry — the ledger must refuse rather than run the wrong payload.
    _claim(ledger)
    ledger.fail("send:k1", "boom")
    with pytest.raises(KeyConflictError):
        ledger.claim("send:k1", "send", fingerprint({"to": "b@example.test"}))


def test_unrecordable_result_refuses_replay_and_reexecution(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json=None)  # committed, result unknown
    with pytest.raises(ResultUnavailableError):
        _claim(ledger)


def test_ttl_replays_inside_the_window_and_reruns_after_it(ledger, clock):
    _claim(ledger, ttl=3600.0)
    ledger.commit("send:k1", result_json="1")
    clock.advance(3599)
    assert _claim(ledger, ttl=3600.0).replayed  # still inside the window
    clock.advance(2)
    claim = _claim(ledger, ttl=3600.0)
    assert claim.action == "execute"  # window over: the effect may run again
    assert claim.entry.attempts == 1  # a fresh incarnation, not a retry


def test_require_resolves_a_unique_prefix(ledger):
    assert ledger.get("send:nope") is None
    _claim(ledger)
    ledger.commit("send:k1", result_json="1")
    assert ledger.require("send:k").key == "send:k1"


def test_require_rejects_an_ambiguous_prefix(ledger):
    _claim(ledger, key="send:k1")
    ledger.commit("send:k1", result_json="1")
    _claim(ledger, key="send:k2")
    with pytest.raises(EntryNotFoundError, match="ambiguous"):
        ledger.require("send:k")


def test_require_treats_like_wildcards_literally(ledger):
    # A prefix containing % or _ must not glob across unrelated keys.
    _claim(ledger, key="send:a_b")
    ledger.commit("send:a_b", result_json="1")
    _claim(ledger, key="send:axb")
    with pytest.raises(EntryNotFoundError):
        ledger.require("send:a%")
    assert ledger.require("send:a_").key == "send:a_b"


def test_entries_filters_and_orders_newest_first(ledger, clock):
    _claim(ledger, key="send:k1")
    ledger.commit("send:k1", result_json="1")
    clock.advance(10)
    ledger.claim("charge:c1", "charge", FP)
    ledger.fail("charge:c1", "declined")
    assert [e.key for e in ledger.entries()] == ["charge:c1", "send:k1"]
    assert [e.key for e in ledger.entries(tool="send")] == ["send:k1"]
    assert [e.key for e in ledger.entries(status="failed")] == ["charge:c1"]
    assert len(ledger.entries(limit=1)) == 1


def test_entries_rejects_unknown_status(ledger):
    with pytest.raises(LedgerError, match="unknown status"):
        ledger.entries(status="done")


def test_stats_counts_totals_and_per_tool_replays(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json="1")
    _claim(ledger)  # replay
    ledger.claim("charge:c1", "charge", FP)
    ledger.fail("charge:c1", "declined")
    stats = ledger.stats()
    assert stats["entries"] == 2
    assert stats["committed"] == 1
    assert stats["failed"] == 1
    assert stats["replays"] == 1
    by_tool = {row["tool"]: row for row in stats["tools"]}
    assert by_tool["send"]["replays"] == 1
    assert by_tool["charge"]["failed"] == 1


def test_purge_by_age_uses_updated_at(ledger, clock):
    _claim(ledger, key="send:old")
    ledger.commit("send:old", result_json="1")
    clock.advance(1000)
    _claim(ledger, key="send:new")
    ledger.commit("send:new", result_json="1")
    assert ledger.purge(older_than=500) == 1
    assert ledger.get("send:old") is None
    assert ledger.get("send:new") is not None


def test_purge_by_status_only_touches_that_status(ledger):
    _claim(ledger, key="send:ok")
    ledger.commit("send:ok", result_json="1")
    _claim(ledger, key="send:bad")
    ledger.fail("send:bad", "boom")
    assert ledger.purge(status="failed") == 1
    assert ledger.get("send:ok") is not None


def test_purge_expired_removes_lapsed_ttl_entries(ledger, clock):
    _claim(ledger, key="send:ttl", ttl=100.0)
    ledger.commit("send:ttl", result_json="1")
    _claim(ledger, key="send:forever")
    ledger.commit("send:forever", result_json="1")
    clock.advance(101)
    assert ledger.purge(expired=True) == 1
    assert ledger.get("send:forever") is not None


def test_purge_without_filters_is_a_noop(ledger):
    # Wiping the ledger must never happen by accident.
    _claim(ledger)
    ledger.commit("send:k1", result_json="1")
    assert ledger.purge() == 0
    assert ledger.get("send:k1") is not None


def test_resolve_discard_lets_the_effect_run_again(ledger):
    _claim(ledger)
    ledger.fail("send:k1", "boom")
    assert ledger.resolve("send:k1", action="discard") is None
    assert ledger.get("send:k1") is None
    assert _claim(ledger).entry.attempts == 1


def test_resolve_commit_overrides_a_failed_entry(ledger):
    _claim(ledger)
    ledger.fail("send:k1", "boom")
    entry = ledger.resolve("send:k1", action="commit", result_json='{"manual":true}')
    assert entry.status == "committed"
    assert entry.error is None
    assert _claim(ledger).entry.result() == {"manual": True}


def test_resolve_fail_overrides_a_committed_entry(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json="1")
    entry = ledger.resolve("send:k1", action="fail", error="operator: it never arrived")
    assert entry.status == "failed"
    assert entry.error == "operator: it never arrived"


def test_resolve_unknown_action_and_missing_key_raise(ledger):
    with pytest.raises(EntryNotFoundError):
        ledger.resolve("send:nope", action="discard")
    _claim(ledger)
    with pytest.raises(LedgerError, match="unknown resolve action"):
        ledger.resolve("send:k1", action="retry")


def test_verify_reports_no_issues_on_a_healthy_ledger(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json='{"id":1}')
    assert ledger.verify() == []


def test_verify_detects_tampered_args_and_undecodable_results(ledger, db_path):
    _claim(ledger)
    ledger.commit("send:k1", result_json="1")
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "UPDATE effects SET args_json = ?, result_json = '{oops' WHERE key = 'send:k1'",
            ('{"to":"evil@example.test"}',),
        )
    issues = ledger.verify()
    assert any("fingerprint mismatch" in issue for issue in issues)
    assert any("result_json is not valid JSON" in issue for issue in issues)


def test_verify_flags_stale_in_flight_leases(ledger, clock):
    _claim(ledger)
    clock.advance(3600)
    issues = ledger.verify()
    assert any("stale in-flight lease" in issue for issue in issues)


def test_ledger_persists_across_reopen(db_path, clock):
    with Ledger(db_path, clock=clock) as led:
        led.claim("send:k1", "send", FP)
        led.commit("send:k1", result_json='{"id":1}')
    with Ledger(db_path, clock=clock) as led:
        claim = led.claim("send:k1", "send", FP)
        assert claim.replayed
        assert claim.entry.result() == {"id": 1}


def test_opening_a_non_database_file_raises_ledger_error(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("this is not a sqlite database, it is a text file padding padding")
    with pytest.raises(LedgerError, match="not a usable ledger"):
        Ledger(str(bogus))


def test_newer_schema_version_is_refused(db_path, clock):
    Ledger(db_path, clock=clock).close()
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE meta SET v = '99' WHERE k = 'schema_version'")
    with pytest.raises(LedgerError, match="schema version 99"):
        Ledger(db_path)


def test_lease_seconds_must_be_positive(db_path):
    with pytest.raises(LedgerError, match="positive"):
        Ledger(db_path, lease_seconds=0)


def test_export_yields_json_safe_dicts(ledger):
    _claim(ledger)
    ledger.commit("send:k1", result_json='{"id":1}')
    dumped = [json.dumps(record) for record in ledger.export()]
    assert len(dumped) == 1
    assert json.loads(dumped[0])["key"] == "send:k1"
