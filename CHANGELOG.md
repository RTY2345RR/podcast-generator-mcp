# Changelog

## 0.1.0 (2026-11-15)
First release.
- 11 MCP tools: check setup, list voices, create show, set host, submit script, synthesize lines, stitch episode, generate episode, episode status, get script, list shows.
- Hosts cloned from a public reference clip or a saved voice; up to 6 hosts per show.
- Host-tagged text scripts (`Host: line`, `## Chapter`, `(tag)`) or JSON turn lists.
- Resumable per-line voice jobs with idempotency keys; edited lines are the only ones re-voiced.
- Episode MP3 (-16 LUFS, ID3 tags and chapters), SRT with speaker names, TXT transcript, Podcasting 2.0 chapters JSON, chapter text, RSS item and feed.xml.
- `--check` and `--print-config` for Claude Desktop, Claude Code, Cursor, Codex, VS Code and Windsurf.
