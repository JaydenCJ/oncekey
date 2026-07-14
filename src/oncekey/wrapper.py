"""Exactly-once wrappers for agent tools.

The wrapper is the piece agent code actually touches: decorate a tool with
:func:`once` (or wrap it with :func:`wrap_tool`), and a retried call with the
same logical arguments replays the recorded result instead of re-running the
side effect.

The contract, precisely:

* **Same key, same arguments, committed** → the recorded result is returned
  (decoded from canonical JSON, so tuples come back as lists).
* **Same key, different arguments** → :class:`KeyConflictError`.
* **Same key, currently executing elsewhere** → :class:`InFlightError`.
* **Previous attempt raised** → the tool runs again by default
  (``retry_failed=True``); pass ``retry_failed=False`` for non-atomic tools
  where "it raised" does not prove "it did not happen".
* **The tool raises** → the exception propagates unchanged; the ledger
  records the failure first.
"""

from __future__ import annotations

import functools
import inspect
import time
from typing import Any, Callable, Iterable, Mapping, Optional, TypeVar, Union

from .errors import KeyDerivationError
from .keys import bind_args, canonical_json, fingerprint, select_fields
from .ledger import Ledger

__all__ = ["once", "wrap_tool", "wrap_toolkit"]

F = TypeVar("F", bound=Callable[..., Any])
KeySpec = Union[str, Callable[[Mapping[str, Any]], str], None]


def once(
    ledger: Ledger,
    *,
    tool: Optional[str] = None,
    key: KeySpec = None,
    key_fields: Optional[Iterable[str]] = None,
    exclude_fields: Optional[Iterable[str]] = None,
    ttl: Optional[float] = None,
    retry_failed: bool = True,
    record_args: bool = True,
) -> Callable[[F], F]:
    """Decorate a tool so each logical effect executes at most once.

    Parameters
    ----------
    ledger:
        The :class:`Ledger` that records effects.
    tool:
        Ledger name for the tool; defaults to the function's ``__name__``.
    key:
        Explicit idempotency key: a string, or a callable receiving the bound
        arguments dict and returning a string (e.g. ``lambda a: a["order_id"]``).
        Without it, the key is derived from the arguments themselves.
    key_fields / exclude_fields:
        Restrict which arguments define "the same call" (mutually exclusive).
        Use ``exclude_fields`` for per-call noise like trace ids.
    ttl:
        Deduplication window in seconds; after it lapses the effect may run
        again. ``None`` (default) deduplicates forever.
    retry_failed:
        When ``False``, a previously failed effect raises
        :class:`PreviouslyFailedError` instead of re-executing.
    record_args:
        Store the key-relevant arguments in the ledger (readable via
        ``oncekey show``). Turn off for payloads you do not want on disk.
    """
    key_fields = tuple(key_fields) if key_fields is not None else None
    exclude_fields = tuple(exclude_fields) if exclude_fields is not None else None

    def decorate(fn: F) -> F:
        tool_name = tool or getattr(fn, "__name__", None) or "tool"

        def prepare(args: tuple, kwargs: Mapping[str, Any]):
            bound = bind_args(fn, args, dict(kwargs))
            selected = select_fields(bound, key_fields, exclude_fields)
            digest = fingerprint(selected)
            if key is None:
                explicit = None
            elif callable(key):
                explicit = key(bound)
            else:
                explicit = key
            if explicit is not None and (not isinstance(explicit, str) or not explicit):
                raise KeyDerivationError(
                    f"explicit key for tool {tool_name!r} must be a non-empty string, "
                    f"got {explicit!r}"
                )
            full_key = f"{tool_name}:{explicit}" if explicit is not None else f"{tool_name}:{digest}"
            args_json = canonical_json(selected) if record_args else None
            return full_key, digest, args_json

        def claim(full_key: str, digest: str, args_json: Optional[str]):
            return ledger.claim(
                full_key,
                tool_name,
                digest,
                args_json=args_json,
                ttl=ttl,
                retry_failed=retry_failed,
            )

        def record_success(full_key: str, result: Any, started: float) -> None:
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                result_json: Optional[str] = canonical_json(result)
            except KeyDerivationError:
                # The effect happened; commit it so it never re-runs, but
                # leave the result unrecorded so replays refuse honestly.
                result_json = None
            ledger.commit(full_key, result_json=result_json, duration_ms=duration_ms)

        def record_failure(full_key: str, exc: BaseException) -> None:
            ledger.fail(full_key, f"{type(exc).__name__}: {exc}")

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                full_key, digest, args_json = prepare(args, kwargs)
                outcome = claim(full_key, digest, args_json)
                if outcome.replayed:
                    return outcome.entry.result()
                started = time.perf_counter()
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as exc:
                    record_failure(full_key, exc)
                    raise
                record_success(full_key, result, started)
                return result

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            full_key, digest, args_json = prepare(args, kwargs)
            outcome = claim(full_key, digest, args_json)
            if outcome.replayed:
                return outcome.entry.result()
            started = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                record_failure(full_key, exc)
                raise
            record_success(full_key, result, started)
            return result

        return wrapper  # type: ignore[return-value]

    return decorate


def wrap_tool(ledger: Ledger, name: str, fn: Callable[..., Any], **options: Any) -> Callable[..., Any]:
    """Wrap a single callable under an explicit tool name.

    Equivalent to ``once(ledger, tool=name, **options)(fn)`` — the imperative
    spelling for tools you receive from a framework rather than define.
    """
    return once(ledger, tool=name, **options)(fn)


class _WrappedToolkit:
    """A thin proxy exposing a toolkit's methods, each wrapped exactly-once.

    Non-callable attributes and anything excluded fall through to the
    original object unchanged.
    """

    def __init__(self, wrapped: Mapping[str, Callable[..., Any]], original: Any) -> None:
        self._wrapped = dict(wrapped)
        self._original = original

    def __getattr__(self, name: str) -> Any:
        wrapped = self.__dict__.get("_wrapped", {})
        if name in wrapped:
            return wrapped[name]
        return getattr(self.__dict__["_original"], name)

    def __dir__(self) -> Iterable[str]:
        return sorted(set(self._wrapped) | set(dir(self._original)))


def wrap_toolkit(
    ledger: Ledger,
    toolkit: Any,
    *,
    include: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
    prefix: Optional[str] = None,
    **options: Any,
) -> Any:
    """Wrap every public method of ``toolkit`` with exactly-once semantics.

    Returns a proxy object; the original toolkit is not mutated. ``include``
    limits wrapping to the named methods, ``exclude`` skips some, and
    ``prefix`` namespaces the ledger tool names (``"email.send"`` style).
    """
    include_set = set(include) if include is not None else None
    exclude_set = set(exclude) if exclude is not None else set()
    wrapped: dict = {}
    for name in dir(toolkit):
        if name.startswith("_") or name in exclude_set:
            continue
        if include_set is not None and name not in include_set:
            continue
        member = getattr(toolkit, name)
        if not callable(member):
            continue
        tool_name = f"{prefix}.{name}" if prefix else name
        wrapped[name] = once(ledger, tool=tool_name, **options)(member)
    if include_set is not None:
        missing = include_set - set(wrapped)
        if missing:
            raise KeyDerivationError(
                f"include names methods the toolkit does not have: {', '.join(sorted(missing))}"
            )
    return _WrappedToolkit(wrapped, toolkit)
