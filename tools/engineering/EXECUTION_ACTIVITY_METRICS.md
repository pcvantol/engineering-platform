# Execution Activity Metrics

## Version 1

The Engineering Platform records three intentionally separate facts.

- **Live worktree snapshot** is a volatile observation of the current managed
  checkout: timestamp, identity, branch/HEAD where available, and uncommitted
  modified/added/deleted counts. A clean (`0 / 0 / 0`) snapshot says only that
  the worktree was clean at that moment. It never says that the run delivered
  no changes and is never a terminal historical result.
- **Cumulative activity** is persisted in the versioned, run-bound Execution
  Activity Summary. One **Codex command** is one persisted Codex CLI provider
  invocation. It is not a shell command, tool call, prompt, token count or
  GitHub request. The summary separates primary activity by lifecycle phase,
  reviewer activity by reviewer and lifecycle phase, and host validation
  commands. The overall activity total is exactly the sum of those three
  categories.
- **Terminal delivery diff** is authoritative repository evidence. It records
  the transaction baseline and terminal target SHAs, Git-proven unique added,
  modified, removed and renamed paths, total unique changed paths, provable
  phase attribution, and lifecycle PR/merge references.

GitHub changed-file counts remain evidence scoped to an individual pull
request. They are displayed separately and are never summed into the terminal
run delivery diff. The Evidence Bundle terminal changed-file count is the
canonical run-delivery count.

## Compatibility and privacy

The SQLite summary is insert-only and begins prospectively. Existing runs are
not backfilled or reinterpreted: their dashboard and prompt-history activity
field is `UNAVAILABLE` / not recorded for this historical run. Projections use
the same persisted summary for the Engineering Report, Execution Receipt
projection, dashboard and Prompt History. No raw commands, prompts, output,
tokens, secrets, private paths or file contents are retained by this metric.
