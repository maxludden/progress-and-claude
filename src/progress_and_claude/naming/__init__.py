"""Task-name refinement: a deterministic baseline plus an optional LLM pass."""

from __future__ import annotations

from progress_and_claude.naming.base import NameCache, Namer, diff_names
from progress_and_claude.naming.heuristic import HeuristicNamer, clamp_text, normalize_name
from progress_and_claude.naming.llm import LLMNamer, StoreNameCache, build_namer

__all__ = [
    "HeuristicNamer",
    "LLMNamer",
    "NameCache",
    "Namer",
    "StoreNameCache",
    "build_namer",
    "clamp_text",
    "diff_names",
    "normalize_name",
]
