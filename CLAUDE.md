@AGENTS.md

The instructions for this repository live in `AGENTS.md` so every coding
agent — Codex, Claude Code, Gemini, OpenCode — reads the same text. Edit
that file, not this one. Project state shared between sessions and
runtimes is in `.backbone/memory/` (git-ignored); `AGENTS.md` says how to
use it. Claude Code's private auto-memory is a cache at most: read
`.backbone/memory/HANDOFF.md` first.
