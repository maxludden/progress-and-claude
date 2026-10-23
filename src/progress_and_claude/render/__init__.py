"""Rendering of snapshots as plain text (for MCP results) and Rich renderables."""

from __future__ import annotations

from progress_and_claude.render.plain import render_plain
from progress_and_claude.render.rich_view import boards_table, render_snapshot

__all__ = ["boards_table", "render_plain", "render_snapshot"]
