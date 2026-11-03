"""Package version, resolved once for CLI, manifest and batch metadata."""

from __future__ import annotations

try:
    from importlib.metadata import version as _package_version

    __version__ = _package_version("rsc")
except Exception:  # pragma: no cover - source checkout without installation
    __version__ = "0.1.0"
