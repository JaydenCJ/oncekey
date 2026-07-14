# oncekey examples

## `email_agent_demo.py` — the double-send horror story

Reproduces the classic failure: a tool call succeeds, the step after it times
out, and the agent's retry loop re-runs the whole step — sending the email
twice. Then it wraps the same tool with `once` and shows the retry replaying
the recorded result instead.

```bash
PYTHONPATH=src python3 examples/email_agent_demo.py /tmp/oncekey-demo
```

Expected output (deterministic, fully offline):

```text
sends without oncekey: 2
sends with oncekey:    1
replayed result identical: True
key reuse with new payload refused: True
ledger: /tmp/oncekey-demo/ledger.db
DEMO OK
```

Afterwards, inspect what the agent actually did:

```bash
PYTHONPATH=src python3 -m oncekey ls    /tmp/oncekey-demo/ledger.db
PYTHONPATH=src python3 -m oncekey stats /tmp/oncekey-demo/ledger.db
PYTHONPATH=src python3 -m oncekey show  /tmp/oncekey-demo/ledger.db charge_card:ord-1001
```

This demo is also the first stage of [`scripts/smoke.sh`](../scripts/smoke.sh),
so it is exercised on every verification run.
