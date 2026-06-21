# Changelog

## 1.3.0

- New `voxtral` engine: neural multilingual TTS (Mistral's Voxtral-4B-TTS) via a
  persistent local mlx-audio server, for far more natural French/English than
  the OS `say`. The server loads the model once and synthesizes over HTTP, so
  the per-block cost is just inference (no reload).
- One-command bootstrap: `setup-voxtral` (or `install --engine voxtral`) detects
  Apple Silicon, creates the bundled venv, installs `mlx-audio` +
  `mistral-common[audio]`, pre-downloads the model, wires hooks, and starts the
  server. Idempotent; falls back to `say` off Apple Silicon.
- `tts-server`: model host runs in the venv; the stdlib-only hook/daemon talk to
  it over `127.0.0.1`. Binds the port BEFORE loading (a duplicate launch fails
  fast), serves `/health` on HTTP threads, and runs ALL MLX work in one worker
  thread (MLX GPU streams are per-thread). Pre-warmed by the hooks; self-exits
  after 30 min idle; single-instance via a socket guard.
- Pipelined drain: the next block is synthesized while the current one plays.
  Blocks are split into sentences for lower latency and steadier pacing, and
  `max_tokens` is capped per chunk to kill runaway generation.
- Robust fallback: retries while the server is still loading (so the first block
  waits for the neural voice instead of dropping to `say`), and when it must use
  `say` it picks a French voice. Temp WAVs are always reclaimed; stale ones from
  a killed player are swept.
- Config: `engine=voxtral`, `voice` (e.g. `fr_female`/`fr_male`), `voxtral_model`
  (any mlx-audio model), `voxtral_port`, `voxtral_python`, `say_voice`. `doctor`
  reports the server and running daemons; `uninstall --purge` removes the venv.
- NOTE: Voxtral weights are CC-BY-NC-4.0 (non-commercial). The server is
  model-agnostic — point `voxtral_model` at a Qwen3-TTS MLX model for Apache-2.0.
  Perf: faster-than-real-time on Macs with a fan; the fanless Air throttles under
  long continuous responses (use a lighter model or `say` there).

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
