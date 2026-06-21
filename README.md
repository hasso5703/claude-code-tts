# claude-code-tts 🔊

**Listen to [Claude Code](https://www.claude.com/product/claude-code)'s replies instead of reading them — spoken live, block by block, as Claude writes them.**

Two engines:

- **`say` / espeak / piper** — zero-dependency, instant, built into the OS.
- **`voxtral`** — a real neural multilingual voice ([Mistral's Voxtral‑4B‑TTS](https://huggingface.co/mistralai/Voxtral-4B-TTS-2603)) running **fully locally** on Apple Silicon via [mlx-audio](https://github.com/Blaizzy/mlx-audio). Natural **French** *and* English (and 7 more languages).

Highlights:

- ✅ **Live streaming.** A background daemon tails the conversation transcript and speaks each block the instant Claude writes it — no waiting for the turn to end, no lag.
- ✅ **Barge-in.** Start typing and the current speech stops immediately.
- ✅ **Sequential, gap-aware playback.** Blocks are split into sentences and the next is synthesized while the current one plays.
- ✅ **Safe, reversible installer.** Merges into `~/.claude/settings.json` (with a `.bak`); never clobbers your other hooks.
- ✅ **Core is single-file, stdlib-only.** The neural engine is an *optional* upgrade isolated in its own venv — the hook that Claude Code runs never imports anything heavy.

---

## Quick start

```bash
git clone https://github.com/hasso5703/claude-code-tts.git
cd claude-code-tts

# Basic: instant OS voice (macOS `say`, Linux espeak/piper)
./install.sh

# — or — neural French/English voice (macOS Apple Silicon):
python3 claude_tts.py setup-voxtral        # creates a venv, installs mlx-audio,
                                            # downloads the model (~2.5 GB), wires hooks
```

Then **restart Claude Code** (hooks load at startup) — or open `/hooks` inside it. Talk to Claude → hear the reply.

> `doctor` shows everything: `python3 claude_tts.py doctor`

---

## The neural voice (Voxtral)

`setup-voxtral` (or `install --engine voxtral`) does the whole bootstrap and is idempotent:

1. checks you're on **macOS + Apple Silicon** (MLX requirement; otherwise it falls back to `say`),
2. creates a venv at `~/.claude/tts/venv` and installs `mlx-audio` + `mistral-common[audio]`,
3. downloads `mlx-community/Voxtral-4B-TTS-2603-mlx-4bit` (~2.5 GB, cached by Hugging Face),
4. sets `engine=voxtral`, `voice=fr_female`, wires the hooks, and starts the model server.

**How it runs.** A small persistent HTTP server (`tts-server`, in the venv) loads the model **once** and synthesizes on demand on `127.0.0.1:8765`. The stdlib-only hook/daemon talk to it over the loopback, get a WAV back, and play it with `afplay`. The server self-exits after 30 min idle and auto-restarts on demand.

**Voices** (set `voice` in the config): French `fr_female` / `fr_male`; generic/English `casual_female`, `casual_male`, `neutral_female`, `neutral_male`, `cheerful_female`; and per-language `de_*`, `es_*`, `it_*`, `pt_*`, `nl_*`, `ar_male`, `hi_*`. Voxtral is multilingual — the voice carries the timbre/accent, the text carries the language.

**License caveat.** Voxtral weights are **CC‑BY‑NC‑4.0 (non-commercial)**. Fine for personal use; not for a commercial product. For a permissive (Apache‑2.0) alternative, point `voxtral_model` at a Qwen3‑TTS MLX model instead — the server is model-agnostic (it uses mlx-audio's generic loader).

### ⚠️ Performance note (read this)

Voxtral‑4B is a 4‑billion‑parameter model. On Macs **with a fan** (M‑series Pro/Max) it generates **faster than real-time** and streaming is seamless. On the **fanless MacBook Air**, a cold/bursty response is fine (~1× real-time), but a **long, continuous** response heats the chip and it thermally throttles (2–5× real-time) → audible gaps. If that bothers you on an Air, switch `voxtral_model` to a lighter model (e.g. a Qwen3‑TTS `0.6B`/`1.7B`) or use `engine=say`. The plumbing is identical; only the model changes.

---

## How it works

```
 you talk to Claude ─► Claude Code writes each block to the transcript (*.jsonl)
                                         │
        UserPromptSubmit hook ──► starts a background daemon for the session
                                         │
            daemon tails the transcript ─┤ each new assistant block →
                                         │   split into sentences → queue
                                         ▼
                              sequential drainer plays them in order
                       (engine=say: OS voice │ engine=voxtral: local model server)
```

- `UserPromptSubmit` starts the daemon and cuts off the previous turn (barge-in).
- `PreToolUse` keeps the daemon (and the model server) warm.
- `SessionEnd` stops the daemon.

Set `stream=false` to fall back to the classic behaviour: speak only the final answer, once, on the `Stop` hook.

---

## Configuration

`~/.claude/tts/config.json` (or pass flags at install / `setup-voxtral`):

| Key              | Default                                   | Meaning |
|------------------|-------------------------------------------|---------|
| `mode`           | `local`                                   | `local` = speak here. `spool` = only record (for a remote listener). |
| `engine`         | `auto`                                    | `auto`, `say`, `espeak-ng`, `spd-say`, `festival`, `piper`, **`voxtral`**. |
| `voice`          | `""` (`fr_female` for voxtral)            | Engine-specific voice. |
| `stream`         | `true`                                    | Speak each block live (daemon). `false` = one shot at end of turn. |
| `barge_in`       | `true`                                    | A new turn interrupts the current speech. |
| `rate`           | `null`                                    | Speed for `say`/`espeak-ng` (wpm) / `spd-say` (−100..100). |
| `max_chars`      | `0`                                       | Truncate spoken text (0 = no limit). |
| `piper_model`    | `""`                                      | Path to a piper `.onnx` voice (engine=piper). |
| `voxtral_model`  | `mlx-community/Voxtral-4B-TTS-2603-mlx-4bit` | Any mlx-audio TTS model id. |
| `voxtral_port`   | `8765`                                    | Local model-server port. |
| `voxtral_python` | `""`                                      | Python with mlx-audio (`""` = the bundled venv). |
| `say_voice`      | `""`                                      | `say` voice used if the neural server is ever unavailable (auto-picks a French voice). |

Env overrides for one run: `CLAUDE_TTS_ENGINE`, `CLAUDE_TTS_VOICE`, `CLAUDE_TTS_RATE`, `CLAUDE_TTS_MODE`.

---

## Commands

```text
claude-tts install [--mode local|spool] [--engine E] [--voice V] [--rate N] [--piper-model PATH]
claude-tts setup-voxtral [--voice fr_female]   # bootstrap + enable the neural voice
claude-tts uninstall [--purge]                 # remove hooks (--purge also deletes config/venv/cache)
claude-tts say "some text"                     # test the current engine (also reads stdin)
claude-tts doctor                              # diagnostics: engines, config, hooks, server, daemons
claude-tts listen [--ssh HOST] [--spool PATH]  # remote mode: tail+speak a server's spool
```

Run as `python3 claude_tts.py <cmd>` (or symlink `claude_tts.py` onto your PATH as `claude-tts`).
Start with **`doctor`** if anything misbehaves.

---

## Remote mode (Claude Code on a server, you listen on your laptop)

**On the server** — record only: `python3 claude_tts.py install --mode spool`, restart Claude Code.
**On your laptop** — listen: `python3 claude_tts.py listen --ssh my-server` (uses your `~/.ssh/config` host; auto-reconnects). Needs passwordless SSH.

> On macOS: `ssh-add --apple-use-keychain ~/.ssh/id_ed25519` and an `~/.ssh/config` entry with `AddKeysToAgent yes` / `UseKeychain yes`.

(The neural engine runs on the box that *speaks*; for remote audio use an OS engine on the laptop.)

---

## Troubleshooting

- **Nothing spoken** → restart Claude Code after install; check `doctor` (`hooks wired`, `daemon(s)`, and for voxtral `server UP`).
- **Robotic voice on French** → that's the `say` fallback; it means the neural server wasn't ready. Check `doctor` → `voxtral … server`; `tail ~/.claude/tts/server.log`.
- **Voxtral setup skipped** → you're not on Apple Silicon, or `python3 -m venv` failed / Python is too old (need 3.10+). It falls back to `say`.
- **Gaps on long responses (fanless Air)** → see the performance note above.
- **Reset the server** → `pkill -f "claude_tts.py tts-server"` (it auto-restarts on the next block).

---

## Privacy

Everything stays on your machine — local files, local synthesis, no network calls (beyond the one-time model download and your own SSH in remote mode).

## Uninstall

```bash
python3 claude_tts.py uninstall           # remove hooks
python3 claude_tts.py uninstall --purge   # also delete config, queue, venv, server state
```
Then restart Claude Code. (The Hugging Face model cache in `~/.cache/huggingface` is left alone.)

## Contributing

Issues and PRs welcome — keep the **hook path standard-library only** (heavy deps belong in the venv). Windows support is a good first contribution.

## License

MIT — see [LICENSE](LICENSE). (The optional Voxtral *model weights* are CC‑BY‑NC‑4.0, owned by Mistral AI; this tool just calls them.)
