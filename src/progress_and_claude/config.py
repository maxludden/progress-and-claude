"""Runtime configuration, resolved from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_state_path

APP_NAME = "progress-and-claude"
DEFAULT_NAMER_MODEL = "claude-haiku-5-5"


def state_dir() -> Path:
    """Return the per-user directory for the database and logs, creating it if needed.

    Returns:
        The state directory (``PROGRESS_AND_CLAUDE_HOME`` if set, else the platform default).
    """
    override = os.environ.get("PROGRESS_AND_CLAUDE_HOME")
    path = Path(override) if override else user_state_path(APP_NAME)
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify(text: str) -> str:
    """Convert free text into a short, stable board identifier.

    Args:
        text: Arbitrary text such as a directory name.

    Returns:
        A lowercase slug of letters, digits and hyphens (``"default"`` if nothing remains).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "default"


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable settings for one process.

    Attributes:
        db_path: SQLite database shared by the MCP server, CLI and TUI.
        default_board: Board used when a caller does not name one.
        namer: ``"auto"`` (LLM when an API key exists), ``"llm"`` or ``"heuristic"``.
        namer_model: Anthropic model used for name refinement.
        namer_timeout: Hard timeout in seconds for the naming call.
        verbose: Whether debug logging is enabled.
    """

    db_path: Path = field(default_factory=lambda: state_dir() / "progress.db")
    default_board: str = field(default_factory=lambda: slugify(Path.cwd().name))
    namer: str = "auto"
    namer_model: str = DEFAULT_NAMER_MODEL
    namer_timeout: float = 8.0
    verbose: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from ``PROGRESS_AND_CLAUDE_*`` environment variables.

        Returns:
            Settings with environment overrides applied on top of the defaults.
        """
        env = os.environ
        defaults = cls()
        return cls(
            db_path=Path(env["PROGRESS_AND_CLAUDE_DB"])
            if "PROGRESS_AND_CLAUDE_DB" in env
            else defaults.db_path,
            default_board=slugify(env.get("PROGRESS_AND_CLAUDE_BOARD", defaults.default_board)),
            namer=env.get("PROGRESS_AND_CLAUDE_NAMER", defaults.namer).lower(),
            namer_model=env.get("PROGRESS_AND_CLAUDE_NAMER_MODEL", defaults.namer_model),
            namer_timeout=float(
                env.get("PROGRESS_AND_CLAUDE_NAMER_TIMEOUT", defaults.namer_timeout)
            ),
            verbose=env.get("PROGRESS_AND_CLAUDE_VERBOSE", "").lower() in {"1", "true", "yes"},
        )
