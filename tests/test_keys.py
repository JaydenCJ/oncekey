"""Key derivation: canonical JSON, fingerprints, argument binding.

The whole package rests on "same arguments → same key"; these tests pin the
canonical form (and, just as importantly, what gets *rejected*, since a loose
canonicalization would silently merge different effects).
"""

from __future__ import annotations

import hashlib

import pytest

from oncekey import (
    KeyDerivationError,
    bind_args,
    canonical_json,
    derive_key,
    fingerprint,
    select_fields,
)


def test_canonical_json_sorts_keys_and_uses_compact_separators():
    # Any key-order or whitespace variation would change the fingerprint.
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert canonical_json({"a": [1, 2], "b": "x"}) == '{"a":[1,2],"b":"x"}'


def test_canonical_json_tuples_equal_lists():
    # JSON has no tuple; (1, 2) and [1, 2] must be the same effect.
    assert canonical_json({"v": (1, 2)}) == canonical_json({"v": [1, 2]})


def test_canonical_json_keeps_unicode_unescaped():
    assert canonical_json({"name": "суши", "emoji": "✓"}) == '{"emoji":"✓","name":"суши"}'


def test_canonical_json_rejects_nan_and_infinity():
    # repr(nan) is not a stable identity, so non-finite floats must not key.
    with pytest.raises(KeyDerivationError, match="non-finite"):
        canonical_json({"amount": float("nan")})
    with pytest.raises(KeyDerivationError, match="non-finite"):
        canonical_json([float("inf")])


def test_canonical_json_rejects_non_string_dict_keys():
    with pytest.raises(KeyDerivationError, match="non-string mapping key"):
        canonical_json({1: "a"})


def test_canonical_json_rejects_custom_objects_with_type_name():
    class Widget:
        pass

    with pytest.raises(KeyDerivationError, match="Widget"):
        canonical_json({"w": Widget()})


def test_canonical_json_rejects_circular_references():
    loop: list = []
    loop.append(loop)
    with pytest.raises(KeyDerivationError, match="circular"):
        canonical_json(loop)


def test_canonical_json_error_reports_the_path():
    # Deep failures must say *where*, or debugging a big payload is misery.
    with pytest.raises(KeyDerivationError, match=r"\$\.outer\[1\]\.inner"):
        canonical_json({"outer": [{}, {"inner": {1, 2}}]})


def test_fingerprint_is_sha256_of_canonical_json():
    expected = hashlib.sha256(b'{"a":1}').hexdigest()
    assert fingerprint({"a": 1}) == expected


def test_fingerprint_changes_when_any_value_changes():
    assert fingerprint({"to": "a@example.test"}) != fingerprint({"to": "b@example.test"})


def test_bind_args_normalizes_positional_and_keyword_calls():
    def send(to, subject):
        pass

    assert bind_args(send, ("x", "y"), {}) == bind_args(send, (), {"subject": "y", "to": "x"})


def test_bind_args_applies_defaults():
    # A call relying on the default and a call passing it explicitly are the
    # same logical effect and must fingerprint identically.
    def send(to, cc=None):
        pass

    assert bind_args(send, ("x",), {}) == bind_args(send, ("x",), {"cc": None})


def test_select_fields_keeps_only_named_fields():
    args = {"to": "x", "subject": "y", "trace_id": "t-1"}
    assert select_fields(args, key_fields=("to", "subject")) == {"to": "x", "subject": "y"}


def test_select_fields_rejects_unknown_key_field():
    with pytest.raises(KeyDerivationError, match="not an argument"):
        select_fields({"to": "x"}, key_fields=("body",))


def test_select_fields_exclude_drops_noise_fields():
    args = {"to": "x", "trace_id": "t-1"}
    assert select_fields(args, exclude_fields=("trace_id",)) == {"to": "x"}


def test_select_fields_key_and_exclude_are_mutually_exclusive():
    with pytest.raises(KeyDerivationError, match="mutually exclusive"):
        select_fields({"a": 1}, key_fields=("a",), exclude_fields=("a",))


def test_derive_key_with_explicit_key_namespaces_by_tool():
    key, digest = derive_key("charge", {"amount": 100}, explicit="ord-1")
    assert key == "charge:ord-1"
    assert digest == fingerprint({"amount": 100})
    with pytest.raises(KeyDerivationError, match="non-empty"):
        derive_key("charge", {"amount": 100}, explicit="")


def test_derive_key_without_explicit_uses_argument_digest():
    key1, _ = derive_key("send", {"to": "a@example.test"})
    key2, _ = derive_key("send", {"to": "b@example.test"})
    assert key1.startswith("send:") and key2.startswith("send:")
    assert key1 != key2
