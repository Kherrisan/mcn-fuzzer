"""Token-budgeted paging and per-turn reply budgets for the extraction tools."""

import threading
from math import ceil

import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")

INDEX_PAGE_TOKENS = 2000
DETAIL_PAGE_TOKENS = 8000
# A read that arrives after the turn budget is spent still answers, in this size,
# so the model always gets a continuation offset instead of an error.
MINIMUM_PAGE_TOKENS = 256


def count_tokens(text):
    return len(ENCODING.encode(text, disallowed_special=()))


def bounded_page(rows, serialize, budget, offset=0, limit=None):
    """Return one page that fits roughly in ``budget`` tokens.

    ``serialize`` returns the JSON text of one row. A single row larger than the
    budget is still returned so a caller never stalls on one oversized item.
    """
    budget = max(MINIMUM_PAGE_TOKENS, int(budget))
    offset = max(0, offset)
    room = len(rows) if limit is None else max(0, int(limit))
    selected = []
    used = 0
    index = offset
    while index < len(rows):
        text = serialize(rows[index])
        cost = count_tokens(text) + 4
        if selected and used + cost > budget:
            break
        selected.append(rows[index])
        used += cost
        index += 1
        if used >= budget or len(selected) >= room:
            break
    return {
        "rows": selected,
        "offset": offset,
        "count": len(rows),
        "next_offset": index if index < len(rows) else None,
        "tokens": used,
    }


def continuation(page):
    """Small footer that tells the model where to continue reading."""
    if page.get("next_offset") is None:
        return {}
    return {"next_offset": page["next_offset"], "count": page["count"]}


class TurnBudget:
    """Tokens all tool replies of one model turn may spend together.

    Read tools share what is left, in call order, so a parallel turn cannot return
    several full pages at once. Mutations and their receipts are not paged: they are
    small, and losing one would hide a write the host already applied.
    """

    def __init__(self, limit):
        self.limit = max(1, int(limit))
        self.used = 0
        self.calls = 0
        # Tool calls of one parallel turn run in worker threads, so the reservation is
        # the synchronization point: two reads can never both claim the same tokens.
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self.used = 0
            self.calls = 0

    @property
    def remaining(self):
        return max(0, self.limit - self.used)

    def reserve(self, requested, *, floor=MINIMUM_PAGE_TOKENS):
        """Claim up to ``requested`` tokens, never below the continuation floor.

        The whole allowance is reserved up front so a parallel turn cannot overdraw the
        budget; ``settle`` returns what the page did not use.
        """
        with self._lock:
            allowed = max(int(floor), min(int(requested), self.remaining or int(floor)))
            self.used += allowed
            self.calls += 1
            return allowed

    def settle(self, allowed, spent):
        """Return the part of a reservation the page did not spend."""
        with self._lock:
            self.used = max(0, self.used - max(0, int(allowed) - max(0, int(spent))))

    def deferred(self):
        """True when this call had to wait for a later turn to read its rows."""
        return self.used >= self.limit


def page_limit(config_context_tokens):
    """Share of the context window one turn's tool replies may use."""
    return max(INDEX_PAGE_TOKENS, ceil(config_context_tokens / 16))
