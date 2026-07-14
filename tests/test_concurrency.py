"""Concurrent duplicates: threads and multiple ledger handles on one file.

The invariant under test is brutal and simple: however many duplicates race,
the side effect runs exactly once. Threads use real contention (no sleeps);
cross-"process" cases use two Ledger instances on the same database file with
a shared fake clock.
"""

from __future__ import annotations

import threading

import pytest

from oncekey import InFlightError, LeaseLostError, Ledger, fingerprint, once

FP = fingerprint({"n": 1})


def test_racing_threads_execute_the_effect_exactly_once(ledger):
    executions = []
    lock = threading.Lock()

    @once(ledger, tool="send")
    def send(n):
        with lock:
            executions.append(n)
        return {"n": n}

    barrier = threading.Barrier(8)
    outcomes = []

    def attempt():
        barrier.wait()
        try:
            outcomes.append(("ok", send(1)))
        except InFlightError:
            outcomes.append(("in_flight", None))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(executions) == 1  # THE invariant
    assert len(outcomes) == 8
    # Every non-blocked duplicate saw the same recorded result.
    assert all(result == {"n": 1} for kind, result in outcomes if kind == "ok")
    assert ledger.get("send:" + fingerprint({"n": 1})).status == "committed"


def test_threads_on_distinct_keys_do_not_interfere(ledger):
    executions = []
    lock = threading.Lock()

    @once(ledger, tool="send")
    def send(n):
        with lock:
            executions.append(n)
        return n

    threads = [threading.Thread(target=send, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(executions) == list(range(10))
    assert ledger.stats()["committed"] == 10


def test_second_process_gets_in_flight_then_replays_after_commit(db_path, clock):
    with Ledger(db_path, clock=clock, owner="proc-a") as a, Ledger(
        db_path, clock=clock, owner="proc-b"
    ) as b:
        a.claim("send:k1", "send", FP)
        with pytest.raises(InFlightError) as excinfo:
            b.claim("send:k1", "send", FP)
        assert excinfo.value.owner == "proc-a"
        a.commit("send:k1", result_json='{"id":1}')
        claim = b.claim("send:k1", "send", FP)
        assert claim.replayed
        assert claim.entry.result() == {"id": 1}


def test_crashed_holder_is_taken_over_and_cannot_commit_late(db_path, clock):
    # proc-a claims and "crashes"; after the lease expires proc-b takes over
    # and commits; a's late commit must fail loudly, not overwrite b's truth.
    with Ledger(db_path, clock=clock, owner="proc-a") as a, Ledger(
        db_path, clock=clock, owner="proc-b"
    ) as b:
        a.claim("send:k1", "send", FP)
        clock.advance(61)
        takeover = b.claim("send:k1", "send", FP)
        assert takeover.action == "execute"
        assert takeover.entry.attempts == 2
        b.commit("send:k1", result_json='{"id":"b"}')
        with pytest.raises(LeaseLostError):
            a.commit("send:k1", result_json='{"id":"a"}')
        assert b.get("send:k1").result() == {"id": "b"}
