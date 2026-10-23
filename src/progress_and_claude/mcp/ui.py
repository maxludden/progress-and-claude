"""The MCP Apps view: URI constant and loader for the bundled HTML."""

from __future__ import annotations

from importlib.resources import files

UI_URI = "ui://progress-and-claude/board.html"


def load_html() -> str:
    """Read the single-file HTML app shipped with the package.

    Returns:
        The HTML document served as the ``ui://`` resource.
    """
    return files("progress_and_claude.mcp").joinpath("app.html").read_text(encoding="utf-8")
