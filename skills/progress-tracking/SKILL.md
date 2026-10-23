---
name: progress-tracking
description: Show accurate task and subtask progress bars while working. Use when starting any multi-step job (roughly three or more distinct steps, long-running work, batch operations over many files/tests/rows) or when the user asks for progress, status, or a plan to be tracked.
---

# Progress tracking

Use the `progress-and-claude` MCP tools to keep a live, accurate progress board while you work. The user can watch the same board in a terminal (`progress-and-claude tui` / `watch`) or in an in-chat view.

## Workflow

1. **Plan first.** Before starting, call `start_plan` once with a short `title` and the full task tree.
   - Names are short, imperative, and distinct: "Migrate user table", not "Now I will go ahead and migrate the user table".
   - Add a one-sentence `description` only when the name alone is ambiguous.
   - Put countable work in `total` (files, tests, rows, pages). Leave `total` out for a plain step that is simply done or not done.
   - A task has **either** `total` **or** `subtasks`, never both. Parent progress is derived from its subtasks.
   - Use `weight` only when subtasks differ a lot in effort (default 1).
2. **Report at milestones, not every action.** Call `update_tasks` when a step starts, finishes, or a countable batch advances meaningfully. Batch several tasks in one call.
   - Prefer `completed` (absolute) or `advance` (increment). Use `fraction` only when there is no natural unit.
   - Set `status: "running"` when you begin a task, `"done"` when it truly finished.
3. **Be honest.**
   - Mark a task `failed` (with a short `note`) when it fails; do not mark it done to keep the bar moving.
   - Use `skipped` for work you decided not to do; it leaves the percentage maths instead of faking progress.
   - Progress never moves backwards unless you pass `reset: true`.
4. **Adjust the plan.** If you discover more work, call `add_tasks` (optionally with `parent_id`). The percentage may drop; that is correct.
5. **Finish cleanly.** When everything is done the board reads 100%. Use `show_progress` to re-display it on request; `clear_board` only if the user asks.

## Notes

- Task ids are dotted (`2`, `2.1`) and returned in every result. Use them in `update_tasks`.
- Names may be refined by the server; check the "Renamed" line in the result and use the ids, not names.
- Tool results already include a rendered progress tree. Show it to the user at milestones; do not re-draw bars yourself.
