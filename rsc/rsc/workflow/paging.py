"""Bounded, complete records from an assignment's immutable input files."""

from .sources import token_count

DEFAULT_BUDGET = 24000
HARD_BUDGET = 30000


class PageError(ValueError):
    pass


def resolve_budget(requested):
    if requested is None:
        return DEFAULT_BUDGET
    if type(requested) is not int or not 1 <= requested <= HARD_BUDGET:
        raise PageError(f"max_tokens must be a positive integer at most {HARD_BUDGET}")
    return requested


def seal_response(payload):
    sealed = {**payload, "tokens": 0}
    for _ in range(12):
        counted = token_count(sealed)
        if sealed["tokens"] == counted:
            return sealed
        sealed["tokens"] = counted
    return sealed


def records_page(rows, cursor=0, max_tokens=None, *, metadata=None):
    budget = resolve_budget(max_tokens)
    if type(cursor) is not int or not 0 <= cursor <= len(rows):
        raise PageError("invalid cursor")

    def render(count):
        end = cursor + count
        return seal_response(
            {
                "items": rows[cursor:end],
                "next_cursor": end if end < len(rows) else None,
                "max_tokens": budget,
                **(metadata or {}),
            }
        )

    chosen = 0
    if render(0)["tokens"] > budget:
        raise PageError(f"page metadata requires {render(0)['tokens']} tokens")
    while cursor + chosen < len(rows) and render(chosen + 1)["tokens"] <= budget:
        chosen += 1
    if not chosen and cursor < len(rows):
        raise PageError(f"single record requires {render(1)['tokens']} tokens; budget is {budget}")
    return render(chosen)
