"""loguru configuration shared by every entry point."""

from __future__ import annotations

import sys

from loguru import logger

from progress_and_claude.config import state_dir

_FORMAT = "{time:HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} - {message}"


def configure_logging(*, verbose: bool = False, stderr: bool = True, quiet: bool = False) -> None:
    """Replace loguru's default sinks with a stderr sink and a rotating file sink.

    stdout is never used: the MCP stdio transport owns it, and the TUI owns the
    terminal, so TUI callers pass ``stderr=False`` to log to the file only.

    Args:
        verbose: Emit DEBUG records instead of INFO.
        stderr: Also log to stderr.
        quiet: Only show warnings and errors on stderr (ignored when ``verbose``).
    """
    logger.remove()
    level = "DEBUG" if verbose else "WARNING" if quiet else "INFO"
    if stderr:
        logger.add(sys.stderr, level=level, format=_FORMAT)
    logger.add(
        state_dir() / "progress-and-claude.log",
        level="DEBUG",
        format=_FORMAT,
        rotation="1 MB",
        retention=3,
        enqueue=True,
    )
