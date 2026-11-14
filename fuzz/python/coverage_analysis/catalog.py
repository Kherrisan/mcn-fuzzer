"""Name-only protocol references.

Digest identities for steps, traces, protocols, proposals, and handlers were
removed. A handler name is its symbol, or ``relative/path::symbol`` when the
symbol collides. Each function keeps a stable symbol id.
"""
