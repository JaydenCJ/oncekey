"""The ``once`` decorator and wrapping helpers: the exactly-once contract.

Every test counts *actual side effects* (list appends) — the one number this
package exists to keep at 1.
"""

from __future__ import annotations

import asyncio

import pytest

from oncekey import (
    KeyConflictError,
    KeyDerivationError,
    PreviouslyFailedError,
    ResultUnavailableError,
    once,
    wrap_tool,
    wrap_toolkit,
)


def test_retried_call_replays_instead_of_reexecuting(ledger):
    sent = []

    @once(ledger, tool="send_email")
    def send_email(to, subject):
        sent.append(to)
        return {"message_id": len(sent), "to": to}

    first = send_email("a@example.test", "hi")
    second = send_email("a@example.test", "hi")
    assert first == second == {"message_id": 1, "to": "a@example.test"}
    assert sent == ["a@example.test"]


def test_positional_keyword_and_defaulted_calls_share_one_key(ledger):
    calls = []

    @once(ledger)
    def send(to, cc=None):
        calls.append(to)
        return "ok"

    send("a@example.test")
    send(to="a@example.test")
    send("a@example.test", cc=None)
    assert calls == ["a@example.test"]


def test_different_arguments_execute_separately(ledger):
    calls = []

    @once(ledger)
    def send(to):
        calls.append(to)
        return to

    send("a@example.test")
    send("b@example.test")
    assert calls == ["a@example.test", "b@example.test"]


def test_exception_propagates_and_the_retry_reexecutes(ledger):
    attempts = []

    @once(ledger)
    def flaky(to):
        attempts.append(to)
        if len(attempts) == 1:
            raise TimeoutError("upstream died")
        return "delivered"

    with pytest.raises(TimeoutError):
        flaky("a@example.test")
    entry = ledger.entries(status="failed")[0]
    assert entry.error == "TimeoutError: upstream died"
    assert flaky("a@example.test") == "delivered"
    assert len(attempts) == 2


def test_retry_failed_false_refuses_to_rerun_non_atomic_tools(ledger):
    attempts = []

    @once(ledger, retry_failed=False)
    def wire_transfer(amount):
        attempts.append(amount)
        raise ConnectionResetError("socket closed mid-request")

    with pytest.raises(ConnectionResetError):
        wire_transfer(100)
    with pytest.raises(PreviouslyFailedError):
        wire_transfer(100)
    assert attempts == [100]  # never ran a second time


def test_explicit_key_callable_conflicts_on_payload_change(ledger):
    charges = []

    @once(ledger, tool="charge", key=lambda a: a["order_id"])
    def charge(order_id, amount):
        charges.append(amount)
        return amount

    charge("ord-1", 100)
    assert charge("ord-1", 100) == 100  # replay
    with pytest.raises(KeyConflictError):
        charge("ord-1", 999)  # same order, different amount: a bug, refused
    assert charges == [100]


def test_explicit_key_must_be_a_non_empty_string(ledger):
    @once(ledger, key=lambda a: a["order_id"])
    def charge(order_id):
        return order_id

    with pytest.raises(KeyDerivationError, match="non-empty string"):
        charge(order_id=42)  # the callable returned an int


def test_exclude_fields_ignores_per_call_noise(ledger):
    calls = []

    @once(ledger, exclude_fields=("trace_id",))
    def send(to, trace_id):
        calls.append(trace_id)
        return "ok"

    send("a@example.test", trace_id="t-1")
    send("a@example.test", trace_id="t-2")  # different trace, same effect
    assert calls == ["t-1"]


def test_ttl_reopens_the_effect_after_the_window(ledger, clock):
    calls = []

    @once(ledger, ttl=3600.0)
    def daily_report(day):
        calls.append(day)
        return len(calls)

    assert daily_report("2026-07-13") == 1
    assert daily_report("2026-07-13") == 1  # inside the window: replay
    clock.advance(3601)
    assert daily_report("2026-07-13") == 2  # window over: runs again
    assert calls == ["2026-07-13", "2026-07-13"]


def test_record_args_false_keeps_payload_off_disk(ledger):
    @once(ledger, record_args=False)
    def send(secret_body):
        return "ok"

    send("the launch codes")
    entry = ledger.entries()[0]
    assert entry.args_json is None
    assert "launch codes" not in (entry.result_json or "")


def test_unserializable_result_commits_but_replay_refuses(ledger):
    class Session:  # no JSON form
        pass

    live = Session()

    @once(ledger)
    def open_session(name):
        return live

    assert open_session("main") is live  # first call gets the real object
    with pytest.raises(ResultUnavailableError):
        open_session("main")  # replay cannot invent it, and must not re-run


def test_replay_decodes_from_json_so_tuples_become_lists(ledger):
    @once(ledger)
    def coords(city):
        return (35.6, 139.7)

    assert coords("tokyo") == (35.6, 139.7)  # live value, still a tuple
    assert coords("tokyo") == [35.6, 139.7]  # replayed from canonical JSON


def test_async_tools_get_the_same_exactly_once_contract(ledger):
    sent = []

    @once(ledger, tool="send_async")
    async def send(to):
        sent.append(to)
        if len(sent) == 1:
            raise TimeoutError("first attempt dies")
        return {"to": to}

    async def scenario():
        with pytest.raises(TimeoutError):
            await send("a@example.test")
        first = await send("a@example.test")  # retry executes
        second = await send("a@example.test")  # third call replays
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second == {"to": "a@example.test"}
    assert len(sent) == 2


def test_wrap_tool_names_the_tool_explicitly(ledger):
    calls = []
    send = wrap_tool(ledger, "email.send", lambda to: calls.append(to) or "ok")
    send("a@example.test")
    send("a@example.test")
    assert calls == ["a@example.test"]
    assert ledger.entries()[0].tool == "email.send"


def test_wrap_toolkit_wraps_public_methods_with_prefix(ledger):
    class Gateway:
        def __init__(self):
            self.sent = []
            self.region = "local"

        def send(self, to):
            self.sent.append(to)
            return "ok"

        def _internal(self):  # private: must not be wrapped
            raise AssertionError("should not be called via proxy wrapping")

    gateway = Gateway()
    kit = wrap_toolkit(ledger, gateway, prefix="email")
    kit.send("a@example.test")
    kit.send("a@example.test")
    assert gateway.sent == ["a@example.test"]
    assert ledger.entries()[0].tool == "email.send"
    assert kit.region == "local"  # non-callables fall through


def test_wrap_toolkit_include_must_name_real_methods(ledger):
    class Gateway:
        def send(self, to):
            return "ok"

    with pytest.raises(KeyDerivationError, match="does not have"):
        wrap_toolkit(ledger, Gateway(), include=("send", "refund"))
