# ruff: noqa: EM101, EM102, TRY003
"""Shared strict JSON decoding hooks for every fail-closed codec.

Four modules decode protocol JSON with the same defensive posture — duplicate
object fields refused, non-finite numbers refused, and (for the strictest
codecs) floats refused and integers bounded. Each carried its own copies of
the hook functions, differing only in the exception raised, which is how a
hardening applied to one decoder silently missed the others (dc-9a0). As
elsewhere in this package, the caller's exception class is injected because
its callers catch that type, not a shared one.
"""

from __future__ import annotations

import json


def strict_loads(
    text: str,
    error: type[Exception],
    *,
    reject_floats: bool = False,
    max_integer: int | None = None,
) -> object:
    """Decode JSON, refusing duplicate fields and non-finite numbers.

    With *reject_floats*, any floating-point token is refused and integer
    tokens are re-parsed under *error* (bounded by *max_integer* when given).
    ``json.JSONDecodeError`` propagates so each caller can wrap it in its own
    vocabulary; *error* is raised directly for the strictness refusals.
    """

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise error(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise error(f"non-finite JSON number is forbidden: {value}")

    def reject_float(value: str) -> object:
        raise error(f"floating-point JSON number is forbidden: {value}")

    def parse_integer(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as int_error:
            raise error("JSON integer token is invalid") from int_error
        if max_integer is not None and not -max_integer <= parsed <= max_integer:
            raise error("JSON integer is outside the supported range")
        return parsed

    if reject_floats:
        return json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_int=parse_integer,
            parse_float=reject_float,
            parse_constant=reject_constant,
        )
    return json.loads(
        text,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
    )
