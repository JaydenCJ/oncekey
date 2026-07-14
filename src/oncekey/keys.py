"""Idempotency key derivation: canonical JSON and SHA-256 fingerprints.

Two calls are "the same effect" when their canonical argument JSON matches.
Canonicalization is strict by design: sorted keys, no NaN/Infinity, no
non-JSON types, no non-string dict keys. A loose canonical form would let two
different calls silently collapse into one key — the exact bug this package
exists to prevent, pointed the other way.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from .errors import KeyDerivationError

__all__ = ["bind_args", "canonical_json", "derive_key", "fingerprint", "select_fields"]

_JSON_SCALARS = (str, int, float, bool, type(None))


def _normalize(value: Any, path: str) -> Any:
    """Return ``value`` reduced to plain JSON types, or raise with a path.

    Tuples become lists (JSON has no tuple), dict keys must already be
    strings, and non-finite floats are rejected because ``repr(nan)`` is not
    a stable identity.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise KeyDerivationError(
                f"non-finite float at {path}: {value!r} has no canonical JSON form"
            )
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                raise KeyDerivationError(
                    f"non-string mapping key at {path}: {key!r} "
                    f"(canonical JSON requires string keys)"
                )
            out[key] = _normalize(value[key], f"{path}.{key}")
        return out
    raise KeyDerivationError(
        f"unsupported type at {path}: {type(value).__name__} is not JSON-serializable"
    )


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to its canonical JSON form.

    Deterministic across processes and Python versions: keys sorted, compact
    separators, unicode kept as-is (no ``\\uXXXX`` escapes), NaN rejected.
    Raises :class:`KeyDerivationError` for anything without a stable form.
    """
    try:
        normalized = _normalize(value, path="$")
    except RecursionError:
        raise KeyDerivationError("circular reference: value refers to itself") from None
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(args: Any) -> str:
    """Return the SHA-256 hex digest of ``args``' canonical JSON."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


def bind_args(fn: Callable[..., Any], args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Dict[str, Any]:
    """Bind a call's arguments to parameter names, defaults applied.

    This is what makes ``send_email("a@example.test")`` and
    ``send_email(to="a@example.test")`` derive the *same* key: both normalize
    to ``{"to": "a@example.test"}`` before fingerprinting.
    """
    signature = inspect.signature(fn)
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    return dict(bound.arguments)


def select_fields(
    args: Mapping[str, Any],
    key_fields: Optional[Iterable[str]] = None,
    exclude_fields: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return the argument subset that participates in the fingerprint.

    ``key_fields`` keeps only the named arguments (each must exist);
    ``exclude_fields`` drops the named ones (useful for values that vary per
    call but do not change the effect, like a trace id). The two are mutually
    exclusive.
    """
    if key_fields is not None and exclude_fields is not None:
        raise KeyDerivationError("key_fields and exclude_fields are mutually exclusive")
    if key_fields is not None:
        selected: Dict[str, Any] = {}
        for field in key_fields:
            if field not in args:
                raise KeyDerivationError(
                    f"key_fields names {field!r}, which is not an argument "
                    f"(have: {', '.join(sorted(args)) or 'none'})"
                )
            selected[field] = args[field]
        return selected
    if exclude_fields is not None:
        dropped = set(exclude_fields)
        return {name: value for name, value in args.items() if name not in dropped}
    return dict(args)


def derive_key(
    tool: str,
    args: Mapping[str, Any],
    *,
    explicit: Optional[str] = None,
    key_fields: Optional[Iterable[str]] = None,
    exclude_fields: Optional[Iterable[str]] = None,
) -> Tuple[str, str]:
    """Return ``(key, fingerprint)`` for one logical effect.

    The fingerprint always hashes the selected argument subset. The key is
    ``"<tool>:<explicit>"`` when an explicit key is given (so the ledger can
    detect key reuse with different arguments), otherwise
    ``"<tool>:<fingerprint>"``.
    """
    selected = select_fields(args, key_fields, exclude_fields)
    digest = fingerprint(selected)
    if explicit is not None:
        if not explicit:
            raise KeyDerivationError("explicit key must be a non-empty string")
        return f"{tool}:{explicit}", digest
    return f"{tool}:{digest}", digest
