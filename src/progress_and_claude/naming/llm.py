"""LLM-backed namer with a hard timeout and a deterministic fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from loguru import logger

from progress_and_claude.config import Settings
from progress_and_claude.core.models import TaskSpec
from progress_and_claude.core.store import Store
from progress_and_claude.naming.base import (
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    NameCache,
    Namer,
)
from progress_and_claude.naming.heuristic import HeuristicNamer, clamp_text

_TOOL_NAME = "submit_names"
_SYSTEM = f"""You polish task names for a progress tracker that shows progress bars.
For every item return a concise imperative `name` (at most {MAX_NAME_CHARS} characters, no
trailing period, no numbering) and a one-sentence `description` (at most
{MAX_DESCRIPTION_CHARS} characters) saying what the task covers.
Rules: keep the original meaning and never invent work; subtask names must not repeat the
words of their parent; keep names distinct among siblings; if a description is already good,
keep it. Return every index exactly once via the {_TOOL_NAME} tool."""

_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": "Return the polished name and description for every task index.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["index", "name", "description"],
                },
            }
        },
        "required": ["items"],
    },
}


class StoreNameCache:
    """:class:`NameCache` backed by the shared SQLite database."""

    def __init__(self, store: Store) -> None:
        """Create a cache.

        Args:
            store: The shared store.
        """
        self._store = store

    def get(self, key: str) -> str | None:
        """Return the cached value for ``key`` or ``None``."""
        with self._store.read() as tx:
            return tx.cache_get(key)

    def put(self, key: str, value: str) -> None:
        """Store ``value`` under ``key``."""
        with self._store.transaction() as tx:
            tx.cache_put(key, value, time.time())


def _flatten(
    specs: Sequence[TaskSpec], parent: int | None = None, out: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Flatten a spec tree to indexed dicts in depth-first order."""
    out = [] if out is None else out
    for spec in specs:
        index = len(out)
        out.append(
            {"index": index, "parent": parent, "name": spec.name, "description": spec.description}
        )
        _flatten(spec.subtasks, index, out)
    return out


def _apply(
    specs: Sequence[TaskSpec], results: dict[int, tuple[str, str]], counter: list[int]
) -> list[TaskSpec]:
    """Rebuild a spec tree, replacing names/descriptions by depth-first index."""
    out: list[TaskSpec] = []
    for spec in specs:
        index = counter[0]
        counter[0] += 1
        name, description = results.get(index, (spec.name, spec.description))
        out.append(
            spec.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "subtasks": _apply(spec.subtasks, results, counter),
                }
            )
        )
    return out


class LLMNamer:
    """Refines names with one batched Anthropic call per plan, falling back on failure."""

    def __init__(
        self,
        *,
        model: str,
        timeout: float,
        cache: NameCache | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Create the namer.

        Args:
            model: Anthropic model id.
            timeout: Hard timeout in seconds for the whole call.
            cache: Optional result cache keyed by the request content.
            client_factory: Builds an ``AsyncAnthropic``-like client (injectable for tests).
        """
        self._model = model
        self._timeout = timeout
        self._cache = cache
        self._client_factory = client_factory
        self._client: Any = None
        self._fallback = HeuristicNamer()

    async def refine(
        self,
        specs: Sequence[TaskSpec],
        *,
        plan_title: str,
        parent_name: str | None = None,
    ) -> list[TaskSpec]:
        """Refine names via the LLM; return the heuristic result if anything goes wrong.

        Args:
            specs: The tasks as written.
            plan_title: Title of the plan, given to the model as context.
            parent_name: Parent task name when adding subtasks.

        Returns:
            Refined specs with the same structure.
        """
        baseline = self._fallback.refine_sync(specs)
        if self._client_factory is None and not os.environ.get("ANTHROPIC_API_KEY"):
            logger.debug("No ANTHROPIC_API_KEY; using heuristic names")
            return baseline
        flat = _flatten(baseline)
        payload = {"plan": plan_title, "parent_task": parent_name, "items": flat}
        key = hashlib.sha256(
            json.dumps([self._model, payload], sort_keys=True).encode()
        ).hexdigest()
        try:
            cached = self._cache.get(key) if self._cache else None
            items = json.loads(cached) if cached else await self._ask(payload)
            if not cached and self._cache:
                self._cache.put(key, json.dumps(items))
            results = self._parse(items, len(flat))
        except Exception as exc:
            logger.warning(
                "LLM naming failed ({}: {}); using heuristic names", type(exc).__name__, exc
            )
            return baseline
        return _apply(baseline, results, [0])

    async def _ask(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Call the model and return its ``items`` list."""
        if self._client is None:
            self._client = (
                self._client_factory() if self._client_factory else self._default_client()
            )
        response = await asyncio.wait_for(
            self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=_SYSTEM,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": json.dumps(payload)}],
            ),
            timeout=self._timeout,
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                items = block.input.get("items")
                if isinstance(items, list):
                    return items
        raise ValueError("model returned no tool_use block with items")

    def _default_client(self) -> Any:
        """Create the real Anthropic client (imported lazily to keep startup fast)."""
        from anthropic import AsyncAnthropic

        return AsyncAnthropic(timeout=self._timeout, max_retries=0)

    @staticmethod
    def _parse(items: list[dict[str, Any]], count: int) -> dict[int, tuple[str, str]]:
        """Validate model output into ``{index: (name, description)}``, dropping bad entries."""
        results: dict[int, tuple[str, str]] = {}
        for item in items:
            index, name, desc = item.get("index"), item.get("name"), item.get("description", "")
            if not isinstance(index, int) or not 0 <= index < count:
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            results[index] = (
                clamp_text(name.strip(" ."), MAX_NAME_CHARS),
                clamp_text(desc if isinstance(desc, str) else "", MAX_DESCRIPTION_CHARS),
            )
        return results


def build_namer(settings: Settings, store: Store | None = None) -> Namer:
    """Choose a namer from settings.

    Args:
        settings: Process settings (``namer`` is ``auto``, ``llm`` or ``heuristic``).
        store: When given, LLM results are cached in it.

    Returns:
        An :class:`LLMNamer` when enabled (``auto`` requires ``ANTHROPIC_API_KEY``),
        otherwise a :class:`HeuristicNamer`.
    """
    use_llm = settings.namer == "llm" or (
        settings.namer == "auto" and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )
    if not use_llm:
        return HeuristicNamer()
    return LLMNamer(
        model=settings.namer_model,
        timeout=settings.namer_timeout,
        cache=StoreNameCache(store) if store else None,
    )
