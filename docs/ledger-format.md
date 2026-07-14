# The oncekey ledger format

The ledger is a single SQLite database (schema version **1**). It is safe to
open read-only with any SQLite client while agents are writing to it; all
oncekey writes happen in `BEGIN IMMEDIATE` transactions and the file uses WAL
journaling.

## Tables

### `meta`

| Column | Type | Meaning |
|---|---|---|
| `k` | TEXT (PK) | Metadata key; currently only `schema_version` |
| `v` | TEXT | Metadata value |

oncekey refuses to open a ledger whose `schema_version` is newer than the
version it understands.

### `effects` — one row per idempotency key

| Column | Type | Meaning |
|---|---|---|
| `key` | TEXT (PK) | Full idempotency key, `"<tool>:<explicit-or-digest>"` |
| `tool` | TEXT | Tool name the effect belongs to |
| `fingerprint` | TEXT | SHA-256 of the canonical JSON of the key-relevant arguments |
| `status` | TEXT | `in_flight`, `committed`, or `failed` (CHECK-constrained) |
| `attempts` | INTEGER | Execution attempts started (1 on first claim) |
| `replays` | INTEGER | Duplicate calls answered from the ledger instead of re-running |
| `args_json` | TEXT? | Canonical JSON of the key-relevant arguments (`NULL` with `record_args=False`) |
| `result_json` | TEXT? | Canonical JSON of the recorded result; `NULL` on a committed row means the result was not JSON-serializable and replays refuse |
| `error` | TEXT? | Last failure message (`"TypeName: message"`, truncated to 4000 chars) |
| `owner` | TEXT? | Lease holder while `in_flight`; `NULL` once settled |
| `created_at` | REAL | Unix seconds of the first claim |
| `updated_at` | REAL | Unix seconds of the last state change |
| `committed_at` | REAL? | Unix seconds of the commit |
| `lease_expires_at` | REAL? | When the current lease can be stolen (only while `in_flight`) |
| `expires_at` | REAL? | End of the deduplication window (`NULL` = dedup forever) |
| `duration_ms` | REAL? | Wall time of the successful execution |

Indexes exist on `tool`, `status`, and `created_at`.

## State machine

```
 (no row) --claim--> in_flight --commit--> committed --claim--> replay (replays+1)
                        |  \--fail--> failed --claim--> in_flight (attempts+1)
                        |
                        +-- lease expired --claim--> in_flight (taken over, attempts+1)
```

Invariants the code enforces (and `oncekey verify` re-checks):

- A key's `fingerprint` never changes while the row lives; a claim with a
  different fingerprint raises `KeyConflictError` instead.
- `commit`/`fail` require the caller to still own the lease; a stolen lease
  raises `LeaseLostError` rather than overwriting the new owner's truth.
- A row whose `expires_at` has lapsed is deleted lazily on the next claim
  (and eagerly by `oncekey purge --expired`), after which the effect may run
  again with a fresh `attempts = 1`.

## Canonical JSON

Fingerprints hash a strict canonical form: keys sorted, compact separators,
Unicode unescaped, `NaN`/`Infinity` rejected, tuples equal to lists, dict keys
must be strings, unknown types rejected (never coerced via `str()`, which
would silently merge different effects). The same form stores `args_json` and
`result_json`, so replayed results decode identically on every platform.
