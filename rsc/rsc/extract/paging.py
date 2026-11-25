"""Token counting and prompt paging for the extraction workflow.

Tool replies are not budgeted; this module only sizes the validation overview that
the Fix prompt carries, and the remaining rows are read through validate_entities.
"""

import tiktoken

ENCODING = tiktoken.get_encoding("cl100k_base")

# A prompt page that arrives after the overview is exhausted still answers, in this
# size, so the model always gets a continuation instead of an empty page.
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
