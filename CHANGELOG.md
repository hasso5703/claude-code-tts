# Changelog

## 1.2.0

- Stream mode is now driven by a transcript-tailing daemon instead of the
  Stop/PreToolUse hooks. The daemon speaks each assistant block the instant its
  line lands in the transcript - decoupled from tool boundaries. This removes
  the start-up delay before the first block, the one-step off-by-one, and the
  long pauses while waiting for the next tool.
- Hooks in stream mode are now lifecycle only: `UserPromptSubmit` starts the
  daemon and hushes the previous turn (instant barge-in via SIGUSR1),
  `PreToolUse` keeps it alive, `SessionEnd` stops it. No hook blocks a tool.
- Daemon is single-instance per session (flock), skips prior turns on start,
  self-exits after 30 min idle, and feeds the same sequential audio queue so
  blocks still play in full without overlapping.
- `doctor` now lists running daemons.

## 1.1.0

- Live streaming mode (default): speak each block of a response as it is
  produced - at every tool boundary (`PreToolUse`) plus the conclusion
  (`Stop`) - instead of one shot at end of turn. Granularity is block-level
  (the text between tool calls), not token-by-token; Claude Code exposes no
  per-token hook.
- Sequential per-session audio queue: chained blocks play in full, one after
  another, without cutting each other off (lock-based drainer, no daemon).
- `UserPromptSubmit` hook hushes trailing speech and resets the cursor the
  moment you send a new prompt.
- Per-session de-dup cursor (keyed by transcript uuid) so each block is spoken
  exactly once across the multiple hook events of a turn.
- `install --no-stream` keeps the classic one-shot behaviour; `--stream`
  re-enables. `doctor` now reports the stream flag and every wired event.

## 1.0.1

- Fix off-by-one: the `Stop` hook could speak the previous turn's text when the
  transcript's closing assistant message hadn't been flushed yet (common when
  the turn ended on a tool call). The hook now waits for the final assistant
  text to land before speaking.

## 1.0.0

- Initial release.
- `Stop` hook captures the final response text and cleans Markdown for speech.
- Local mode (speak on this machine) and spool mode (record for a remote listener).
- `listen` client: local tail or remote tail over SSH, with auto-reconnect and barge-in.
- TTS engines: macOS `say`; Linux `espeak-ng`, `spd-say`, `festival`, `piper`.
- Safe, idempotent, reversible installer that merges into `~/.claude/settings.json`.
- `doctor` diagnostics command.
- GitHub Actions CI smoke test.
