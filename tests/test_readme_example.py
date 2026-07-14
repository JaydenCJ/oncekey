"""The README quickstart, executed verbatim — keep code and docs in sync.

If this test breaks, either the library changed behavior or the README is
lying; both need fixing in the same commit.
"""

from __future__ import annotations

from oncekey import Ledger, once


def test_readme_quickstart_behaves_as_documented(tmp_path):
    ledger = Ledger(str(tmp_path / "effects.db"))
    sent = []

    @once(ledger, tool="send_email")
    def send_email(to: str, subject: str) -> dict:
        sent.append(to)  # imagine the SMTP call here
        return {"message_id": f"msg-{len(sent)}", "to": to}

    first = send_email("ops@example.test", "deploy finished")
    second = send_email("ops@example.test", "deploy finished")  # agent retried

    assert first == {"message_id": "msg-1", "to": "ops@example.test"}
    assert second == first  # replayed, not re-sent
    assert len(sent) == 1  # the email went out exactly once

    stats = ledger.stats()
    assert stats["entries"] == 1
    assert stats["replays"] == 1  # the "duplicates suppressed" the CLI shows
