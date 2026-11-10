"""Versioned bank-email parsers. Each transaction stores the version that produced it."""

from khugjil.ingest.parsers import khan_v1

CURRENT = khan_v1

__all__ = ["CURRENT", "khan_v1"]
