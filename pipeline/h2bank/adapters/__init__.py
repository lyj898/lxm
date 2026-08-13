"""Source adapters.

Each adapter implements the same `discover() -> list[PaperLink]` interface, so
scaling from the 5-paper pilot to every school/year is a matter of raising the
cap and adding adapters - no changes to the crawl driver.
"""

from __future__ import annotations

from .base import PaperLink, SourceAdapter, get_adapter, register, registry

__all__ = ["PaperLink", "SourceAdapter", "get_adapter", "register", "registry"]

# Importing the modules populates the registry via the @register decorator.
from . import freetestpaper, sgtestpaper, testpapersfree  # noqa: E402,F401
