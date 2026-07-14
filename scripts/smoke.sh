#!/usr/bin/env bash
# Smoke test for oncekey: run the double-send demo, inspect the resulting
# ledger with every CLI subcommand, and give a real shell command
# exactly-once semantics with `oncekey run`.
# Self-contained: pure stdlib, no network, idempotent (works from a clean tree).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON="$ROOT/.venv/bin/python"
fi

# The package has zero runtime dependencies, so running from src/ needs no install.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/oncekey-smoke.XXXXXX")"
trap 'rm -rf "$WORKDIR"' EXIT
DB="$WORKDIR/ledger.db"

fail() { echo "SMOKE FAIL: $1" >&2; exit 1; }

echo "[smoke] python: $("$PYTHON" --version 2>&1)"

# 1. The demo: naive retry double-sends, the wrapped tool does not.
demo_out="$("$PYTHON" "$ROOT/examples/email_agent_demo.py" "$WORKDIR")" \
  || fail "email_agent_demo.py exited non-zero"
echo "$demo_out" | sed 's/^/[demo] /'
echo "$demo_out" | grep -q "sends without oncekey: 2" || fail "naive loop should have double-sent"
echo "$demo_out" | grep -q "sends with oncekey:    1" || fail "wrapped tool sent more than once"
echo "$demo_out" | grep -q "replayed result identical: True" || fail "replay did not match the recording"
echo "$demo_out" | grep -q "key reuse with new payload refused: True" || fail "key conflict was not refused"
echo "$demo_out" | grep -q "DEMO OK" || fail "demo did not finish"
[ -f "$DB" ] || fail "demo did not write the ledger"

# 2. ls: both tools are visible with the right statuses.
ls_out="$("$PYTHON" -m oncekey ls "$DB")"
echo "$ls_out" | sed 's/^/[ls] /'
echo "$ls_out" | grep -E 'send_email.+committed' >/dev/null || fail "ls missing committed send_email"
echo "$ls_out" | grep -E 'charge_card.+committed' >/dev/null || fail "ls missing charge_card"

# 3. stats: the ledger counts the suppressed duplicates.
stats_out="$("$PYTHON" -m oncekey stats "$DB")"
echo "$stats_out" | sed 's/^/[stats] /'
echo "$stats_out" | grep -q "duplicates suppressed: 2" || fail "stats did not count 2 suppressed duplicates"

# 4. show: a unique key prefix resolves and prints the recorded result.
show_out="$("$PYTHON" -m oncekey show "$DB" "charge_card:ord-1001")"
echo "$show_out" | grep -q "status.*committed" || fail "show missing status"
echo "$show_out" | grep -q '"order":"ord-1001"' || fail "show missing recorded result"

# 5. run: a real command executes once; the retry replays byte-identical output.
run1="$("$PYTHON" -m oncekey run "$DB" --key rel-42 -- sh -c "echo ran >> '$WORKDIR/marker'; echo deployed")" \
  || fail "first run exited non-zero"
run2="$("$PYTHON" -m oncekey run "$DB" --key rel-42 -- sh -c "echo ran >> '$WORKDIR/marker'; echo deployed" 2>"$WORKDIR/run2.err")" \
  || fail "replayed run exited non-zero"
[ "$run1" = "deployed" ] || fail "unexpected first run output: $run1"
[ "$run2" = "deployed" ] || fail "replayed output differs: $run2"
grep -q "\[oncekey\] replayed shell:rel-42" "$WORKDIR/run2.err" || fail "replay marker missing"
[ "$(wc -l < "$WORKDIR/marker")" -eq 1 ] || fail "the command's side effect ran more than once"

# 6. run: a failing command stays retryable and keeps its exit code.
set +e
"$PYTHON" -m oncekey run "$DB" --key rel-bad -- sh -c "exit 3" >/dev/null 2>&1
rc=$?
set -e
[ "$rc" -eq 3 ] || fail "failing run should exit 3, got $rc"
"$PYTHON" -m oncekey show "$DB" shell:rel-bad | grep -q "status.*failed" || fail "failed run not recorded"

# 7. verify, export, purge: the admin loop.
"$PYTHON" -m oncekey verify "$DB" | grep -q "ledger OK" || fail "verify reported issues"
exported="$("$PYTHON" -m oncekey export "$DB" | wc -l)"
[ "$exported" -eq 4 ] || fail "export should emit 4 entries, got $exported"
purge_out="$("$PYTHON" -m oncekey purge "$DB" --status failed)"
echo "$purge_out" | grep -q "purged 1 entry" || fail "purge did not remove the failed entry"

# 8. --version agrees with the package.
version_out="$("$PYTHON" -m oncekey --version)"
pkg_version="$("$PYTHON" -c 'import oncekey; print(oncekey.__version__)')"
[ "$version_out" = "oncekey $pkg_version" ] \
  || fail "--version mismatch: '$version_out' vs package '$pkg_version'"

echo "SMOKE OK"
