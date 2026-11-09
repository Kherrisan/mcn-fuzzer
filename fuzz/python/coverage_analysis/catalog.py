"""Name-only protocol references.

Digest identities for steps, traces, protocols, proposals, and handlers were
removed. A handler name is its symbol, or ``relative/path::symbol`` when the
symbol collides. Ordinary file SHA-256 checks live with the manifest.
"""
