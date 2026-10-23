"""Deterministic name cleanup: always available, instant, and the LLM fallback."""

from __future__ import annotations

import re
from collections.abc import Sequence

from progress_and_claude.core.models import TaskSpec
from progress_and_claude.naming.base import MAX_DESCRIPTION_CHARS, MAX_NAME_CHARS

_NUMBERING = re.compile(r"^\s*(?:(?:step|task|phase)\s*\d+\s*[:.)-]\s*|\d+[.)]\s+|[-*•]\s+)", re.I)
_FILLER = re.compile(
    r"^(?:(?:now|next|then|first|finally|also|ok(?:ay)?|so|and),?\s+)*"
    r"(?:(?:i(?:'ll|\s+will|\s+am\s+going\s+to|'m\s+going\s+to)|let(?:'s|\s+us)|"
    r"we(?:'ll|\s+will|\s+need\s+to|\s+should)|(?:i\s+)?need\s+to|going\s+to|please|"
    r"try\s+to|plan\s+to|time\s+to)\s+)+",
    re.I,
)
_POLITE_TAIL = re.compile(r"[\s,]+please\W*$", re.I)
_TRAILING = " .:;,-\N{EN DASH}\N{EM DASH}"


def clamp_text(text: str, limit: int) -> str:
    """Collapse whitespace and cut text at a word boundary, adding an ellipsis.

    Args:
        text: Text to shorten.
        limit: Maximum length of the result, including the ellipsis.

    Returns:
        The cleaned text, at most ``limit`` characters.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut and not text[limit - 1].isspace():
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(_TRAILING + "(") + "…"


def normalize_name(raw: str) -> tuple[str, str]:
    """Clean a task name.

    Strips list numbering and conversational filler ("Now I'll ..."), removes
    trailing punctuation, capitalises the first letter and shortens to
    :data:`MAX_NAME_CHARS`.

    Args:
        raw: The name as written.

    Returns:
        ``(name, overflow)`` where ``overflow`` is the full cleaned text when the
        name had to be shortened (useful as a description), else ``""``.
    """
    text = " ".join(raw.split())
    text = _NUMBERING.sub("", text)
    text = _POLITE_TAIL.sub("", _FILLER.sub("", text)).strip(_TRAILING)
    if not text:
        text = " ".join(raw.split()) or "Task"
    text = text[0].upper() + text[1:]
    if len(text) <= MAX_NAME_CHARS:
        return text, ""
    return clamp_text(text, MAX_NAME_CHARS), text


class HeuristicNamer:
    """Rule-based namer: cleans phrasing, caps lengths, de-duplicates siblings."""

    async def refine(
        self,
        specs: Sequence[TaskSpec],
        *,
        plan_title: str,
        parent_name: str | None = None,
    ) -> list[TaskSpec]:
        """Return cleaned copies of ``specs`` (see :class:`~progress_and_claude.naming.base.Namer`).

        Args:
            specs: The tasks as written.
            plan_title: Unused; accepted for protocol compatibility.
            parent_name: Unused; accepted for protocol compatibility.

        Returns:
            Cleaned specs with the same structure.
        """
        return self.refine_sync(specs)

    def refine_sync(self, specs: Sequence[TaskSpec]) -> list[TaskSpec]:
        """Synchronous implementation of :meth:`refine`.

        Args:
            specs: The tasks as written.

        Returns:
            Cleaned specs with the same structure.
        """
        seen: dict[str, int] = {}
        out: list[TaskSpec] = []
        for spec in specs:
            name, overflow = normalize_name(spec.name)
            key = name.lower()
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                name = f"{name} ({seen[key]})"
            description = clamp_text(spec.description or overflow, MAX_DESCRIPTION_CHARS)
            out.append(
                spec.model_copy(
                    update={
                        "name": name,
                        "description": description,
                        "subtasks": self.refine_sync(spec.subtasks),
                    }
                )
            )
        return out
