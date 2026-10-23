# progress-and-claude

Accurate, hierarchical progress bars for Claude. Claude plans its work as tasks and subtasks, reports progress as it goes, and you watch the same board live in a Rich CLI, a Textual TUI, or an in-chat MCP Apps view.

One SQLite database (WAL) is shared by every front end, so what Claude reports through the MCP server is exactly what the CLI and TUI show — no daemon, no sockets.

## Install

```bash
uv sync                      # develop in this checkout
uv build                     # build sdist + wheel
```

As a Claude Code plugin (this repo is the plugin root: `.claude-plugin/plugin.json`, `.mcp.json`, `skills/`):

```bash
claude --plugin-dir /path/to/progress-and-claude
```

The bundled `.mcp.json` starts the server with `uvx --from ${CLAUDE_PLUGIN_ROOT} progress-and-claude-mcp`, and the `progress-tracking` skill teaches Claude when and how to use it.

## How progress is computed

- A leaf's progress is `completed / total`, clamped to 0..1. `done` is 100%. A step with no `total` is 0% until done.
- A parent's progress is the weighted mean of its non-skipped children. Parent status is derived, never set by hand.
- `skipped` tasks leave the maths; `failed` tasks keep their real percentage and mark ancestors *blocked*.
- Progress never goes backwards unless `reset` is passed, and a bar only shows 100% when the task is done.
- ETA is an exponential moving average of the observed rate, shown only after enough samples.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `start_plan(title, tasks, board?)` | Create the plan from a nested task tree |
| `add_tasks(tasks, parent_id?, board?)` | Add work discovered later |
| `update_tasks(updates, board?)` | Batch `completed` / `advance` / `fraction` / `status` / `note` updates |
| `show_progress(board?)` | Re-display the board |
| `list_boards()`, `clear_board(board?)` | Housekeeping |
| `get_board_state(board?)` | App-only; refreshes the in-chat view |

Every result contains a rendered text tree plus structured JSON. `start_plan`, `add_tasks`, `update_tasks` and `show_progress` also carry `_meta.ui.resourceUri` pointing at `ui://progress-and-claude/board.html`, a dependency-free MCP Apps view for hosts that render it (the Claude Code terminal shows the text tree).

### Task names

Names and descriptions are refined when tasks are created: one batched Anthropic call per `start_plan`/`add_tasks` (never per update) with an 8 s timeout. Without `ANTHROPIC_API_KEY`, on timeout, or on any error, a deterministic cleaner is used instead (strips filler like "Now I'll", numbering and trailing punctuation, caps length, de-duplicates siblings).

## CLI

```bash
progress-and-claude new "Release" "Build" ".Compile=40" ".Link" "Test=12"   # dots nest, =N sets total
progress-and-claude update 1.1 --advance 5
progress-and-claude done 1.2
progress-and-claude add -p 2 "Flaky retries"
progress-and-claude show            # one-shot Rich view (--json for data)
progress-and-claude watch           # live view
progress-and-claude tui             # Textual dashboard
progress-and-claude demo            # fake release that advances live
```

Viewers fall back to the most recently updated board when the current directory has none, so `tui` from any shell shows Claude's work.

## TUI keys

`q` quit · `r` refresh · `space`/`enter` fold · `c` fold all · `f` filter (all → active → failed) · `tab` switch pane.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROGRESS_AND_CLAUDE_DB` | platform state dir | SQLite file |
| `PROGRESS_AND_CLAUDE_HOME` | platform state dir | Database and log directory |
| `PROGRESS_AND_CLAUDE_BOARD` | slug of the working directory | Default board |
| `PROGRESS_AND_CLAUDE_NAMER` | `auto` | `auto`, `llm` or `heuristic` |
| `PROGRESS_AND_CLAUDE_NAMER_MODEL` | `claude-haiku-5-5` | Model for name refinement |
| `PROGRESS_AND_CLAUDE_NAMER_TIMEOUT` | `8` | Seconds before falling back |
| `PROGRESS_AND_CLAUDE_VERBOSE` | off | Debug logging |

The MCP server writes nothing to stdout except protocol messages; logs go to stderr and a rotating file in the state directory.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest
```
