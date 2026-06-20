# Changelog

## 1.0.0

- Initial release.
- `Stop` hook captures the final response text and cleans Markdown for speech.
- Local mode (speak on this machine) and spool mode (record for a remote listener).
- `listen` client: local tail or remote tail over SSH, with auto-reconnect and barge-in.
- TTS engines: macOS `say`; Linux `espeak-ng`, `spd-say`, `festival`, `piper`.
- Safe, idempotent, reversible installer that merges into `~/.claude/settings.json`.
- `doctor` diagnostics command.
- GitHub Actions CI smoke test.
