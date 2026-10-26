# AGENTS.md

Working rules for coding agents (Codex and others) in this repository.

- `CLAUDE.md` is the full working agreement for this repository: operator-only
  actions, guarded-operation rules, tests and style. It applies to every agent,
  not only Claude. Read it before working.
- Organise files as the shared standard describes:
  https://github.com/Decksx/agent-swarm/blob/main/docs/standards/workspace-and-project-layout.md
  - Review, test and mutation copies of this repository go in your own scratch
    area, never in `C:\git`. Remove them when the PR or review ends.
  - Satellite repositories and assets for the comic suite live under
    `C:\git\ComicAutomation\_related\`, which is git-ignored.
  - Do not move `C:\git\ComicAutomation`: the "CBZ Automation" desktop shortcut
    points to it.
- New files go where the standard's repository layout says. Known root-level
  deviations (`Logs/`, `progress_tracking/`, `archive/`,
  `cbz_sqlite_schema_phase1/`, `README_SQLITE_SCHEMA_PHASE1.md`) are fixed only
  by a dedicated PR.
