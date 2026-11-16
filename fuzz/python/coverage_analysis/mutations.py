"""Field mutations over an ordered proposal trace."""

from __future__ import annotations

import base64
import binascii

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
FIELD_KINDS = ("string", "bytes", "integer", "boolean", "list", "map")
BYTES_ENCODINGS = ("hex", "base64")


class MutationError(ValueError):
    pass


def _representable(value, path):
    """Reject values that cannot round-trip through the producer's ParamValue."""
    if isinstance(value, bool) or isinstance(value, str):
        return
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise MutationError(f"{path}: integer is outside the signed 64-bit range")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _representable(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for name, item in value.items():
            if not isinstance(name, str) or not name:
                raise MutationError(f"{path}: object member names must be nonempty strings")
            _representable(item, f"{path}.{name}")
        return
    raise MutationError(
        f"{path}: only strings, integers, booleans, lists, and objects are representable"
    )


def _encoded_bytes(value, encoding, path):
    if not isinstance(value, str):
        raise MutationError(f"{path}: bytes values must be encoded strings")
    if not value:
        return
    if encoding == "hex":
        if len(value) % 2:
            raise MutationError(f"{path}: hex bytes need an even number of digits")
        try:
            bytes.fromhex(value)
        except ValueError as error:
            raise MutationError(f"{path}: invalid hex bytes: {error}") from error
        return
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise MutationError(f"{path}: invalid base64 bytes: {error}") from error


def validate_default(item, kind, encoding, path="default"):
    """Check that a declared default matches its kind and is representable."""
    if "default" not in item:
        return
    value = item["default"]
    expected = {
        "string": (str, "a string"),
        "integer": (int, "an integer"),
        "boolean": (bool, "a boolean"),
        "list": (list, "an array"),
        "map": (dict, "an object"),
    }
    if kind == "bytes":
        _encoded_bytes(value, encoding or "hex", path)
        return
    type_, description = expected[kind]
    if type_ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise MutationError(f"{path} must be {description} for kind {kind}")
    elif not isinstance(value, type_):
        raise MutationError(f"{path} must be {description} for kind {kind}")
    _representable(value, path)


def field_type(item):
    """Validate and return (kind, encoding) for one mutable field."""
    kind = str(item.get("kind") or "string").strip()
    if kind not in FIELD_KINDS:
        raise MutationError(
            f"unknown field kind {kind!r}; expected one of {', '.join(FIELD_KINDS)}"
        )
    encoding = item.get("encoding")
    if kind == "bytes":
        encoding = str(encoding or "hex").strip()
        if encoding not in BYTES_ENCODINGS:
            raise MutationError(
                f"unknown bytes encoding {encoding!r}; expected hex or base64"
            )
    else:
        if encoding not in (None, ""):
            raise MutationError("encoding is only valid for bytes fields")
        encoding = None
    validate_default(item, kind, encoding)
    return kind, encoding
