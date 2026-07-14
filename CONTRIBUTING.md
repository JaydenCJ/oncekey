# Contributing to oncekey

Thanks for your interest in contributing. Issues, discussions, and pull
requests are all welcome.

## Getting started

You need Python 3.9 or newer; the runtime has zero dependencies and the only
test dependency is pytest.

```bash
git clone https://github.com/JaydenCJ/oncekey
cd oncekey
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
bash scripts/smoke.sh
```

`scripts/smoke.sh` runs the double-send demo end-to-end, drives every CLI
subcommand against the resulting ledger, and gives a real shell command
exactly-once semantics; it must print `SMOKE OK`.

## Before you open a pull request

1. Format touched files consistently with the existing style (PEP 8, 100-column lines).
2. Keep the tree warning-free: `python -W error -m pytest` must stay green.
3. `pytest` — the full suite must pass, offline, with no new flakiness.
4. `bash scripts/smoke.sh` — must print `SMOKE OK`.
5. Add tests for behavior changes; keep logic in pure, unit-testable modules
   (`keys.py` and `ledger.py` carry the invariants — the wrapper and CLI stay thin).

## Ground rules

- **No new runtime dependencies.** The package is standard-library only; that
  is a feature, not an accident. Test-only dependencies belong in the `dev` extra.
- **No network calls, no telemetry.** The ledger is local; nothing leaves the machine.
- **Schema changes need a version bump and docs.** Anything that changes the
  meaning of an `effects` column must bump `SCHEMA_VERSION` and update
  `docs/ledger-format.md` in the same pull request.
- **Never weaken the exactly-once contract silently.** Changes to claim/commit/fail
  semantics need a test demonstrating the old failure mode is still caught.
- **Keep the three READMEs aligned.** `README.md`, `README.zh.md`, and
  `README.ja.md` are line-for-line translations; update all three together
  (English is authoritative). The quickstart is executed verbatim by
  `tests/test_readme_example.py`.
- Code comments and doc comments are written in English.

## Reporting bugs

Please include `oncekey --version`, the output of `oncekey show` for the
affected key (redact `args_json`/`result_json` if sensitive), and a minimal
repro. For suspected double executions, `oncekey export` of the relevant tool
plus the exception you observed (`LeaseLostError` etc.) is usually enough.

## Security

Please do not report security issues in public GitHub issues. Use GitHub's
private vulnerability reporting on this repository instead.
