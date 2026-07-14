# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-07-13

### Added

- SQLite effect ledger (`Ledger`) with a claim/commit/fail lifecycle: one row
  per idempotency key, `BEGIN IMMEDIATE` transactions for cross-process
  safety, WAL journaling, and an injectable clock for deterministic tests.
- Lease-based crash recovery: concurrent duplicates get `InFlightError` while
  a lease is live, expired leases are taken over, and a late commit after a
  takeover raises `LeaseLostError` instead of overwriting the new owner's result.
- Strict canonical-JSON key derivation: sorted keys, compact separators,
  NaN/Infinity and non-JSON types rejected with the failing path; SHA-256
  argument fingerprints; `KeyConflictError` on key reuse with a changed payload.
- `once` decorator (sync and async), `wrap_tool`, and `wrap_toolkit` with
  explicit keys (string or callable), `key_fields`/`exclude_fields` argument
  selection, TTL deduplication windows, `retry_failed=False` for non-atomic
  tools, and `record_args=False` to keep payloads off disk.
- Honest edge-case handling: failed attempts stay retryable and keep their
  attempt count; committed results that were not JSON-serializable refuse
  replay with `ResultUnavailableError` rather than inventing a value.
- `oncekey` CLI: `ls`, `show` (unique key prefixes), `stats` (with a
  duplicates-suppressed counter), `verify` (fingerprint/JSON cross-check,
  exit 1 on issues), `export` (JSON Lines), `purge` (filter required), and
  `resolve` (`--commit/--fail/--discard` human overrides).
- `oncekey run`: exactly-once execution for shell commands — replays recorded
  stdout/stderr/exit code on retries; non-zero exits stay retryable unless
  `--any-exit` is given.
- Runnable double-send demo (`examples/email_agent_demo.py`), ledger schema
  documentation (`docs/ledger-format.md`), and a trilingual README.
- 90 offline pytest tests (keys, ledger, wrapper, concurrency, CLI, README
  example) and `scripts/smoke.sh` printing `SMOKE OK`.

### Notes

- The repository ships no CI workflow; verification is local — `pip install -e '.[dev]' && pytest && bash scripts/smoke.sh`.

[0.1.0]: https://github.com/JaydenCJ/oncekey/releases/tag/v0.1.0
