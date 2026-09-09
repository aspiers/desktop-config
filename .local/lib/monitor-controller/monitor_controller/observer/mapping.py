"""Shared exact output-bijection search."""

MAX_MAPPING_SOLUTIONS: int = 2


class AmbiguousBijectionError(ValueError):
    """Candidate edges form more than one complete bijection."""


def unique_bijection(
    candidates: dict[str, tuple[str, ...]],
    required: frozenset[str],
) -> dict[str, str] | None:
    """Return the sole complete candidate bijection, rejecting ambiguity."""
    ordered = tuple(
        sorted(candidates, key=lambda output: (len(candidates[output]), output))
    )
    solutions: list[dict[str, str]] = []

    def search(index: int, used: frozenset[str], mapping: dict[str, str]) -> None:
        if len(solutions) >= MAX_MAPPING_SOLUTIONS:
            return
        if index == len(ordered):
            if used == required:
                solutions.append(mapping.copy())
            return
        saved_output = ordered[index]
        for live_output in candidates[saved_output]:
            if live_output in used:
                continue
            mapping[saved_output] = live_output
            search(index + 1, used | {live_output}, mapping)
            del mapping[saved_output]

    search(0, frozenset(), {})
    if len(solutions) > 1:
        raise AmbiguousBijectionError
    return solutions[0] if solutions else None
