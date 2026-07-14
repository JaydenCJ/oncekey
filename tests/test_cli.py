"""The ``oncekey`` CLI, driven through ``main(argv)`` with captured output.

Ledgers are prepared through the library, then inspected/administered through
the CLI exactly as a user would. ``run`` invokes real subprocesses — tiny
``sh -c`` snippets, fully offline.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from oncekey import Ledger, fingerprint
from oncekey.cli import _parse_duration, main

FP = fingerprint({"to": "a@example.test"})


def _seed(db_path):
    """One committed (with a replay) and one failed entry."""
    with Ledger(db_path) as led:
        led.claim("send:k1", "send", FP, args_json='{"to":"a@example.test"}')
        led.commit("send:k1", result_json='{"id":1}', duration_ms=3.5)
        led.claim("send:k1", "send", FP)  # replay
        led.claim("charge:c1", "charge", FP)
        led.fail("charge:c1", "card declined")


def test_version_flag_prints_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "oncekey 0.1.0"


def test_ls_lists_entries_and_honors_filters(db_path, capsys):
    _seed(db_path)
    assert main(["ls", db_path]) == 0
    out = capsys.readouterr().out
    assert "send:k1" in out and "charge:c1" in out
    assert "committed" in out and "failed" in out

    assert main(["ls", db_path, "--status", "failed"]) == 0
    out = capsys.readouterr().out
    assert "charge:c1" in out and "send:k1" not in out

    assert main(["ls", db_path, "--tool", "nosuch"]) == 0
    assert "no entries" in capsys.readouterr().out


def test_show_accepts_a_unique_key_prefix(db_path, capsys):
    _seed(db_path)
    assert main(["show", db_path, "send:"]) == 0
    out = capsys.readouterr().out
    assert "send:k1" in out
    assert '{"id":1}' in out
    assert "3.5 ms" in out


def test_unknown_key_and_missing_ledger_exit_2_with_messages(db_path, tmp_path, capsys):
    _seed(db_path)
    assert main(["show", db_path, "nope:"]) == 2
    assert "no ledger entry" in capsys.readouterr().err
    assert main(["ls", str(tmp_path / "absent.db")]) == 2
    assert "no ledger at" in capsys.readouterr().err


def test_stats_reports_duplicates_suppressed(db_path, capsys):
    _seed(db_path)
    assert main(["stats", db_path]) == 0
    out = capsys.readouterr().out
    assert "duplicates suppressed: 1" in out
    assert "committed 1, failed 1" in out


def test_export_emits_one_json_object_per_entry(db_path, capsys):
    _seed(db_path)
    assert main(["export", db_path, "--status", "committed"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["key"] == "send:k1"
    assert record["replays"] == 1


def test_verify_passes_clean_and_fails_tampered(db_path, capsys):
    _seed(db_path)
    assert main(["verify", db_path]) == 0
    assert "ledger OK" in capsys.readouterr().out
    with sqlite3.connect(db_path) as raw:
        raw.execute("UPDATE effects SET result_json = '{oops' WHERE key = 'send:k1'")
    assert main(["verify", db_path]) == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_purge_requires_an_explicit_filter(db_path, capsys):
    _seed(db_path)
    assert main(["purge", db_path]) == 2
    assert "refusing to purge" in capsys.readouterr().err
    assert main(["purge", db_path, "--status", "failed"]) == 0
    assert "purged 1 entry" in capsys.readouterr().out


def test_resolve_commit_then_discard_roundtrip(db_path, capsys):
    _seed(db_path)
    assert main(["resolve", db_path, "charge:c1", "--commit", "--result", "{oops"]) == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert main(["resolve", db_path, "charge:c1", "--commit", "--result", '{"manual": true}']) == 0
    assert "as committed" in capsys.readouterr().out
    with Ledger(db_path) as led:
        assert led.get("charge:c1").result() == {"manual": True}
    assert main(["resolve", db_path, "charge:c1", "--discard"]) == 0
    with Ledger(db_path) as led:
        assert led.get("charge:c1") is None


def test_run_executes_once_then_replays_output(tmp_path, capsys):
    db = str(tmp_path / "ledger.db")
    marker = tmp_path / "marker.txt"
    cmd = f"echo ran >> {marker}; echo hello"
    assert main(["run", db, "--key", "rel-1", "--", "sh", "-c", cmd]) == 0
    first = capsys.readouterr()
    assert first.out == "hello\n"
    assert main(["run", db, "--key", "rel-1", "--", "sh", "-c", cmd]) == 0
    second = capsys.readouterr()
    assert second.out == "hello\n"  # byte-identical stdout from the ledger
    assert "[oncekey] replayed shell:rel-1" in second.err
    assert marker.read_text() == "ran\n"  # the side effect happened once


def test_run_nonzero_exit_stays_retryable(tmp_path, capsys):
    db = str(tmp_path / "ledger.db")
    assert main(["run", db, "--key", "j1", "--", "sh", "-c", "echo boom >&2; exit 3"]) == 3
    capsys.readouterr()
    # Same key, same command: the failure is retried, not replayed.
    assert main(["run", db, "--key", "j1", "--", "sh", "-c", "echo boom >&2; exit 3"]) == 3
    assert "replayed" not in capsys.readouterr().err
    with Ledger(db) as led:
        entry = led.get("shell:j1")
        assert entry.status == "failed"
        assert entry.attempts == 2


def test_run_any_exit_records_failures_as_committed(tmp_path, capsys):
    db = str(tmp_path / "ledger.db")
    assert main(["run", db, "--key", "j2", "--any-exit", "--", "sh", "-c", "exit 3"]) == 3
    capsys.readouterr()
    assert main(["run", db, "--key", "j2", "--any-exit", "--", "sh", "-c", "exit 3"]) == 3
    assert "replayed shell:j2 (recorded exit 3" in capsys.readouterr().err


def test_run_without_a_command_exits_2(tmp_path, capsys):
    assert main(["run", str(tmp_path / "ledger.db"), "--key", "k"]) == 2
    assert "no command given" in capsys.readouterr().err


def test_run_derives_key_from_command_when_omitted(tmp_path, capsys):
    db = str(tmp_path / "ledger.db")
    assert main(["run", db, "--", "echo", "hi"]) == 0
    capsys.readouterr()
    assert main(["run", db, "--", "echo", "hi"]) == 0
    assert "replayed shell:" in capsys.readouterr().err
    assert main(["run", db, "--", "echo", "other"]) == 0  # different command runs
    assert capsys.readouterr().out == "other\n"


def test_parse_duration_understands_suffixes():
    assert _parse_duration("90") == 90.0
    assert _parse_duration("90s") == 90.0
    assert _parse_duration("15m") == 900.0
    assert _parse_duration("24h") == 86400.0
    assert _parse_duration("7d") == 604800.0
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_duration("soon")
