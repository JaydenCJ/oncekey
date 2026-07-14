#!/usr/bin/env python3
"""The double-send horror story, before and after oncekey.

An agent asks a tool to email a customer. The tool call succeeds, but the
step *after* it times out, so the agent's retry loop runs the whole step
again — and without protection the customer gets the email twice. This demo
reproduces that failure, then wraps the same tool with ``once`` and shows the
retry replaying the recorded result instead of re-sending.

Usage::

    python examples/email_agent_demo.py [WORKDIR]

The ledger is written to ``WORKDIR/ledger.db`` (default: a temp directory),
so you can inspect it afterwards with ``oncekey ls``/``stats``/``show``.
Fully offline and deterministic; prints ``DEMO OK`` on success.
"""

from __future__ import annotations

import os
import sys
import tempfile

from oncekey import KeyConflictError, Ledger, once


class EmailGateway:
    """A stand-in for an SMTP client: every send() is a real side effect."""

    def __init__(self) -> None:
        self.outbox = []

    def send(self, to: str, subject: str, body: str) -> dict:
        self.outbox.append((to, subject, body))
        return {"message_id": f"msg-{len(self.outbox)}", "to": to}


def flaky_agent_step(send, state, attempts_before_success: int = 1) -> dict:
    """One agent step: call the tool, then 'time out' after the side effect.

    This models the nasty real-world ordering — the send succeeded, the
    failure happened later, and the retry re-runs everything.
    """
    result = send(to="customer@example.test", subject="Your order shipped", body="Tracking: 42")
    if state["failures"] < attempts_before_success:
        state["failures"] += 1
        raise TimeoutError("model call timed out after the tool already ran")
    return result


def main() -> int:
    workdir = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="oncekey-demo-")
    os.makedirs(workdir, exist_ok=True)
    ledger_path = os.path.join(workdir, "ledger.db")

    # --- Act 1: the naive retry loop double-sends. -------------------------
    naive_gateway = EmailGateway()
    naive_state = {"failures": 0}
    for _ in range(2):  # a typical "retry once" policy
        try:
            flaky_agent_step(naive_gateway.send, naive_state)
            break
        except TimeoutError:
            continue
    print(f"sends without oncekey: {len(naive_gateway.outbox)}")

    # --- Act 2: the same loop, tool wrapped with oncekey. ------------------
    ledger = Ledger(ledger_path)
    safe_gateway = EmailGateway()
    send_email = once(ledger, tool="send_email")(safe_gateway.send)

    safe_state = {"failures": 0}
    results = []
    for _ in range(2):
        try:
            results.append(flaky_agent_step(send_email, safe_state))
            break
        except TimeoutError:
            continue
    print(f"sends with oncekey:    {len(safe_gateway.outbox)}")
    replay = send_email(to="customer@example.test", subject="Your order shipped", body="Tracking: 42")
    print(f"replayed result identical: {replay == results[-1]}")

    # --- Act 3: explicit keys catch key reuse with a different payload. ----
    charge = once(ledger, tool="charge_card", key=lambda a: a["order_id"])(
        lambda order_id, amount_cents: {"charged": amount_cents, "order": order_id}
    )
    charge(order_id="ord-1001", amount_cents=4200)
    try:
        charge(order_id="ord-1001", amount_cents=9900)  # same key, new amount: a bug
        refused = False
    except KeyConflictError:
        refused = True
    print(f"key reuse with new payload refused: {refused}")

    print(f"ledger: {ledger_path}")
    print("DEMO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
