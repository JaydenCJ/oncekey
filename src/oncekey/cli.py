"""The ``oncekey`` command-line interface.

Read side: ``ls``, ``show``, ``stats``, ``verify``, ``export`` make the
ledger a first-class debugging surface ("did the agent actually send that
email, and how many retries did we swallow?"). Write side: ``purge`` and
``resolve`` are the administrative overrides, and ``run`` gives any shell
command exactly-once semantics without writing a line of Python.

Exit codes: 0 success; 1 for ``verify`` with issues; ``run`` propagates the
recorded command's exit code; 2 for usage and ledger errors.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, models
from .errors import OnceKeyError
from .keys import canonical_json, fingerprint
from .ledger import Ledger
from .models import Entry

__all__ = ["main"]

_KEY_WIDTH = 46
_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


# ------------------------------------------------------------------ helpers


def _parse_duration(text: str) -> float:
    """Parse ``"90"``/``"90s"``/``"15m"``/``"24h"``/``"7d"`` into seconds."""
    raw = text.strip().lower()
    unit = 1.0
    if raw and raw[-1] in _DURATION_UNITS:
        unit = _DURATION_UNITS[raw[-1]]
        raw = raw[:-1]
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid duration {text!r}; use a number with an optional s/m/h/d suffix"
        ) from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"duration must be >= 0, got {text!r}")
    return value * unit


def _format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _format_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _open(path: str) -> Ledger:
    if path != ":memory:" and not os.path.exists(path):
        raise OnceKeyError(f"no ledger at {path!r} (a ledger is created by the first wrapped call)")
    return Ledger(path)


def _print_table(rows: List[List[str]], *, right_align: Sequence[int] = ()) -> None:
    if not rows:
        return
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cells.append(cell.rjust(widths[i]) if i in right_align else cell.ljust(widths[i]))
        print("  ".join(cells).rstrip())


# --------------------------------------------------------------- subcommands


def _cmd_ls(args: argparse.Namespace) -> int:
    with _open(args.ledger) as ledger:
        entries = ledger.entries(tool=args.tool, status=args.status, limit=args.limit)
        now = ledger._clock()  # the ledger's clock keeps ages consistent in tests
    if not entries:
        print("no entries")
        return 0
    rows = [["KEY", "TOOL", "STATUS", "ATTEMPTS", "REPLAYS", "AGE"]]
    for entry in entries:
        rows.append(
            [
                _truncate(entry.key, _KEY_WIDTH),
                entry.tool,
                entry.status,
                str(entry.attempts),
                str(entry.replays),
                _format_age(now - entry.created_at),
            ]
        )
    _print_table(rows, right_align=(3, 4))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    with _open(args.ledger) as ledger:
        entry = ledger.require(args.key)
        now = ledger._clock()
    fields = [
        ("key", entry.key),
        ("tool", entry.tool),
        ("status", entry.status),
        ("attempts", str(entry.attempts)),
        ("replays", str(entry.replays)),
        ("fingerprint", entry.fingerprint),
        ("created", f"{_format_ts(entry.created_at)} ({_format_age(now - entry.created_at)} ago)"),
        ("committed", _format_ts(entry.committed_at)),
        ("expires", _format_ts(entry.expires_at)),
        ("duration", f"{entry.duration_ms:.1f} ms" if entry.duration_ms is not None else "-"),
        ("owner", entry.owner or "-"),
        ("error", entry.error or "-"),
        ("args", entry.args_json if entry.args_json is not None else "(not recorded)"),
        (
            "result",
            entry.result_json
            if entry.result_json is not None
            else ("(unavailable)" if entry.status == models.COMMITTED else "-"),
        ),
    ]
    width = max(len(name) for name, _ in fields)
    for name, value in fields:
        print(f"{name.ljust(width)}  {value}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    with _open(args.ledger) as ledger:
        stats = ledger.stats()
    print(f"oncekey stats — {args.ledger}")
    print(
        f"entries:               {stats['entries']}   "
        f"(committed {stats['committed']}, failed {stats['failed']}, "
        f"in flight {stats['in_flight']})"
    )
    print(f"attempts started:      {stats['attempts']}")
    print(f"duplicates suppressed: {stats['replays']}")
    if stats["tools"]:
        print()
        rows = [["TOOL", "ENTRIES", "COMMITTED", "FAILED", "IN-FLIGHT", "REPLAYS"]]
        for tool in stats["tools"]:
            rows.append(
                [
                    tool["tool"],
                    str(tool["entries"]),
                    str(tool["committed"]),
                    str(tool["failed"]),
                    str(tool["in_flight"]),
                    str(tool["replays"]),
                ]
            )
        _print_table(rows, right_align=(1, 2, 3, 4, 5))
    return 0


def _cmd_purge(args: argparse.Namespace) -> int:
    if args.older_than is None and args.status is None and not args.expired:
        raise OnceKeyError(
            "refusing to purge without a filter; pass --older-than, --status, and/or --expired "
            "(use --older-than 0 to wipe everything)"
        )
    with _open(args.ledger) as ledger:
        count = ledger.purge(older_than=args.older_than, status=args.status, expired=args.expired)
    print(f"purged {count} entr{'y' if count == 1 else 'ies'}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    with _open(args.ledger) as ledger:
        issues = ledger.verify()
        total = ledger.stats()["entries"]
    if issues:
        for issue in issues:
            print(issue)
        print(f"{len(issues)} issue{'' if len(issues) == 1 else 's'} found")
        return 1
    print(f"ledger OK ({total} entr{'y' if total == 1 else 'ies'})")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    with _open(args.ledger) as ledger:
        entries = ledger.entries(tool=args.tool, status=args.status)
    for entry in entries:
        print(json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=False))
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    if args.result is not None and not args.commit:
        raise OnceKeyError("--result only applies with --commit")
    if args.error is not None and not args.fail:
        raise OnceKeyError("--error only applies with --fail")
    with _open(args.ledger) as ledger:
        entry = ledger.require(args.key)
        if args.discard:
            ledger.resolve(entry.key, action="discard")
            print(f"discarded {entry.key} (the effect may run again)")
            return 0
        if args.fail:
            ledger.resolve(entry.key, action="fail", error=args.error)
            print(f"resolved {entry.key} as failed")
            return 0
        result_json: Optional[str] = None
        if args.result is not None:
            try:
                result_json = canonical_json(json.loads(args.result))
            except ValueError as exc:
                raise OnceKeyError(f"--result is not valid JSON: {exc}") from exc
        ledger.resolve(entry.key, action="commit", result_json=result_json)
        print(f"resolved {entry.key} as committed")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if not command:
        raise OnceKeyError("no command given; usage: oncekey run LEDGER --key KEY -- cmd args...")
    payload: Dict[str, Any] = {"argv": command}
    digest = fingerprint(payload)
    key = f"{args.tool}:{args.key}" if args.key else f"{args.tool}:{digest}"

    with Ledger(args.ledger) as ledger:
        claim = ledger.claim(
            key,
            args.tool,
            digest,
            args_json=canonical_json(payload),
            ttl=args.ttl,
        )
        if claim.replayed:
            recorded = claim.entry.result()
            sys.stdout.write(recorded["stdout"])
            sys.stderr.write(recorded["stderr"])
            print(
                f"[oncekey] replayed {key} (recorded exit {recorded['exit_code']}, "
                f"replay #{claim.entry.replays})",
                file=sys.stderr,
            )
            return int(recorded["exit_code"])

        started = time.perf_counter()
        try:
            proc = subprocess.run(command, capture_output=True, text=True)
        except OSError as exc:
            ledger.fail(key, f"{type(exc).__name__}: {exc}")
            raise OnceKeyError(f"cannot run {command[0]!r}: {exc}") from exc
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if proc.returncode == 0 or args.any_exit:
            result = {
                "argv": command,
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            ledger.commit(
                key,
                result_json=canonical_json(result),
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )
        else:
            # A non-zero exit is a failed attempt: record it and stay
            # retryable, exactly like a wrapped tool that raised.
            ledger.fail(key, f"exit code {proc.returncode}")
        return proc.returncode


# -------------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oncekey",
        description="Inspect and administer an oncekey effect ledger; run shell commands exactly once.",
    )
    parser.add_argument("--version", action="version", version=f"oncekey {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="command")

    p = sub.add_parser("ls", help="list ledger entries, newest first")
    p.add_argument("ledger", help="path to the ledger database")
    p.add_argument("--tool", help="only entries for this tool")
    p.add_argument("--status", choices=models.STATUSES, help="only entries with this status")
    p.add_argument("--limit", type=int, help="show at most N entries")
    p.set_defaults(fn=_cmd_ls)

    p = sub.add_parser("show", help="show one entry in full (key may be a unique prefix)")
    p.add_argument("ledger")
    p.add_argument("key")
    p.set_defaults(fn=_cmd_show)

    p = sub.add_parser("stats", help="totals and a per-tool breakdown")
    p.add_argument("ledger")
    p.set_defaults(fn=_cmd_stats)

    p = sub.add_parser("purge", help="delete entries matching the given filters")
    p.add_argument("ledger")
    p.add_argument("--older-than", type=_parse_duration, metavar="DUR", help="e.g. 30m, 24h, 7d")
    p.add_argument("--status", choices=models.STATUSES)
    p.add_argument("--expired", action="store_true", help="entries whose TTL has lapsed")
    p.set_defaults(fn=_cmd_purge)

    p = sub.add_parser("verify", help="cross-check the ledger; exit 1 on issues")
    p.add_argument("ledger")
    p.set_defaults(fn=_cmd_verify)

    p = sub.add_parser("export", help="dump entries as JSON Lines on stdout")
    p.add_argument("ledger")
    p.add_argument("--tool")
    p.add_argument("--status", choices=models.STATUSES)
    p.set_defaults(fn=_cmd_export)

    p = sub.add_parser("resolve", help="manually settle an entry (human override)")
    p.add_argument("ledger")
    p.add_argument("key", help="entry key (or a unique prefix)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--commit", action="store_true", help="mark the effect as done")
    group.add_argument("--fail", action="store_true", help="mark the effect as failed")
    group.add_argument("--discard", action="store_true", help="delete the entry so it may run again")
    p.add_argument("--result", metavar="JSON", help="with --commit: record this JSON result")
    p.add_argument("--error", metavar="MSG", help="with --fail: the error to record")
    p.set_defaults(fn=_cmd_resolve)

    p = sub.add_parser(
        "run",
        help="run a shell command exactly once, replaying its output on retries",
        usage="oncekey run [-h] [--key KEY] [--tool TOOL] [--ttl DUR] [--any-exit] ledger -- command...",
    )
    p.add_argument("ledger", help="ledger path (created if missing)")
    p.add_argument("--key", help="idempotency key; derived from the command line if omitted")
    p.add_argument("--tool", default="shell", help="tool name in the ledger (default: shell)")
    p.add_argument("--ttl", type=_parse_duration, metavar="DUR", help="dedup window, e.g. 24h")
    p.add_argument(
        "--any-exit",
        action="store_true",
        help="record non-zero exits as committed too (default: they stay retryable)",
    )
    p.set_defaults(fn=_cmd_run)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    # Everything after a literal "--" is the command for `oncekey run`; split
    # it off before argparse sees it (argparse.REMAINDER is too greedy).
    raw = list(sys.argv[1:] if argv is None else argv)
    tail: List[str] = []
    if "--" in raw:
        cut = raw.index("--")
        tail = raw[cut + 1 :]
        raw = raw[:cut]
    args = parser.parse_args(raw)
    if tail and args.cmd != "run":
        parser.error(f"unrecognized arguments after --: {' '.join(tail)}")
    args.command = tail
    try:
        return int(args.fn(args))
    except OnceKeyError as exc:
        print(f"oncekey: {exc}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
