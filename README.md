# claude-code-tts 🔊

**Listen to [Claude Code](https://www.claude.com/product/claude-code)'s replies instead of reading them — spoken live, block by block, in a natural French (or English) voice, fully on your machine.**

Pick your engine:

- **`kokoro`** ⚡ — neural, **native French** (`ff_siwis`), tiny (82M) → **faster than real-time, never thermally throttles**, **Apache-2.0 (commercial-friendly)**. The recommended default.
- **`voxtral`** — neural, native French (`fr_female` / `fr_male`), **top quality** ([Mistral's 4B](https://huggingface.co/mistralai/Voxtral-4B-TTS-2603)). CC-BY-NC (personal use). Best fidelity, heavier.
- **`say` / espeak / piper** — zero-dependency OS voices; instant, robotic, nothing to install.

Both neural voices run **100% locally** on Apple Silicon via [mlx-audio](https://github.com/Blaizzy/mlx-audio), and you flip between them with one command.

Highlights:

- ✅ **Live streaming.** A background daemon tails the conversation transcript and speaks each block the instant Claude writes it — no waiting for the turn to end.
- ✅ **Barge-in** (start typing → speech stops) and **gap-aware playback** (split into sentences, the next is synthesized while the current plays).
- ✅ **One-command voice switch** — `preset kokoro` / `preset voxtral`.
- ✅ **Safe, reversible installer.** Merges into `~/.claude/settings.json` (with a `.bak`); the hook Claude Code runs is **stdlib-only** — heavy deps live in an isolated venv.

---

## Quick start

```bash
git clone https://github.com/hasso5703/claude-code-tts.git
cd claude-code-tts

# Recommended — fast, commercial-friendly French neural voice (macOS Apple Silicon):
python3 claude_tts.py setup-kokoro

#  …or top-quality French (heavier, non-commercial):
python3 claude_tts.py setup-voxtral

#  …or no setup at all — instant robotic OS voice:
./install.sh
```

Then **restart Claude Code** (hooks load at startup) — or open `/hooks` inside it. Talk to Claude → hear the reply.

> `setup-kokoro` / `setup-voxtral` are idempotent and install **both** neural voices' deps, so you can `preset` between them anytime. See everything with `python3 claude_tts.py doctor`.

---

## The two neural voices

| | **`kokoro`** (recommended) | **`voxtral`** |
|---|---|---|
| French | native (`ff_siwis`) | native (`fr_female` / `fr_male`) |
| Quality | good | **top** |
| Speed | **RTF ~0.5× — faster than real-time, never throttles** | ~1× (throttles on a fanless Air under long output) |
| Footprint | 82M (~0.3 GB download, ~1.8 GB RAM) | 4B (~2.5 GB download, ~2.7 GB RAM) |
| License | **Apache-2.0 (commercial OK)** | CC-BY-NC-4.0 (personal only) |

Switch anytime — it reloads the model in seconds:

```bash
python3 claude_tts.py preset kokoro
python3 claude_tts.py preset voxtral
```

**Rule of thumb:** fanless Mac or a commercial product → **kokoro**; maximum fidelity on a Mac with a fan → **voxtral**.

**How it runs.** A small persistent server (`tts-server`, in the bundled venv) loads the model **once** and synthesizes on `127.0.0.1:8765`. The stdlib-only hook/daemon POST text, get a WAV back, and play it with `afplay`. The server self-exits after 30 min idle and auto-restarts on demand.

**What setup does (idempotent).** Checks **macOS + Apple Silicon + Python ≥ 3.10** (otherwise falls back to `say`); creates `~/.claude/tts/venv` and installs `mlx-audio` plus both voices' text deps (`mistral-common[audio]` for Voxtral; `misaki[en]` + `phonemizer-fork` + `espeakng-loader` for Kokoro — `espeakng-loader` bundles espeak-ng, so **no system install**); downloads the model; **auto-patches a Kokoro mlx-audio bug** (a phase-length mismatch in `istftnet` that crashed some inputs to `say`); wires the hooks; starts the server.

**Voices.** Kokoro French: `ff_siwis`. Voxtral French: `fr_female`, `fr_male`; plus generic/English `casual_*`, `neutral_*`, `cheerful_female`, and per-language `de_*`, `es_*`, `it_*`, `pt_*`, `nl_*`, `ar_male`, `hi_*`. Set `voice` in the config (Kokoro also needs `lang_code`, e.g. `f` = French). The server is model-agnostic — point `voxtral_model` at any mlx-audio TTS model and set `voice` / `lang_code` to add your own.

**Speed.** `speed` in the config (e.g. `1.1`) is applied per request — no restart needed.

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
                   (engine=say: OS voice │ engine=voxtral: local model server,
                                            running kokoro or voxtral weights)
```

- `UserPromptSubmit` starts the daemon and cuts off the previous turn (barge-in).
- `PreToolUse` keeps the daemon (and the model server) warm.
- `SessionEnd` stops the daemon.

Set `stream=false` to fall back to the classic behaviour: speak only the final answer, once, on the `Stop` hook.

---

## Configuration

`~/.claude/tts/config.json` (or pass flags at install / setup):

| Key              | Default                                   | Meaning |
|------------------|-------------------------------------------|---------|
| `mode`           | `local`                                   | `local` = speak here. `spool` = only record (for a remote listener). |
| `engine`         | `auto`                                    | `auto`, `say`, `espeak-ng`, `spd-say`, `festival`, `piper`, **`voxtral`** (the local model server — runs the kokoro/voxtral weights). |
| `voice`          | `""`                                      | Engine-specific voice. Neural: `ff_siwis` (kokoro), `fr_female` (voxtral). |
| `lang_code`      | `""`                                      | Language for engines that need it — **kokoro: `f`** = French. |
| `speed`          | `1.0`                                     | Playback speed (kokoro applies it; voxtral ignores it). |
| `stream`         | `true`                                    | Speak each block live (daemon). `false` = one shot at end of turn. |
| `barge_in`       | `true`                                    | A new turn interrupts the current speech. |
| `rate`           | `null`                                    | Speed for `say`/`espeak-ng` (wpm) / `spd-say` (−100..100). |
| `max_chars`      | `0`                                       | Truncate spoken text (0 = no limit). |
| `piper_model`    | `""`                                      | Path to a piper `.onnx` voice (engine=piper). |
| `voxtral_model`  | Voxtral 4B (Kokoro after `preset kokoro`) | Any mlx-audio TTS model id. |
| `voxtral_port`   | `8765`                                    | Local model-server port. |
| `voxtral_python` | `""`                                      | Python with mlx-audio (`""` = the bundled venv). |
| `say_voice`      | `""`                                      | `say` voice for the rare fallback if the server is down (auto-picks a French voice). |

Env overrides for one run: `CLAUDE_TTS_ENGINE`, `CLAUDE_TTS_VOICE`, `CLAUDE_TTS_RATE`, `CLAUDE_TTS_MODE`.

---

## Commands

```text
claude-tts setup-kokoro [--voice ff_siwis]     # enable Kokoro (fast/light, Apache-2.0)
claude-tts setup-voxtral [--voice fr_female]   # enable Voxtral (top quality, CC-BY-NC)
claude-tts preset kokoro|voxtral               # switch neural voice (reloads the model)
claude-tts install [--mode local|spool] [--engine E] [--voice V] [--no-stream]
claude-tts say "some text"                     # test the current engine (also reads stdin)
claude-tts doctor                              # diagnostics: engines, config, hooks, server, daemons
claude-tts uninstall [--purge]                 # remove hooks (--purge also deletes config/venv)
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

- **Nothing spoken** → restart Claude Code after install; check `doctor` (`hooks wired`, `daemon(s)`, and `server UP`).
- **Robotic voice / `say` creeps in** → the neural server wasn't reachable. Check `doctor`; `tail ~/.claude/tts/server.log`. Reset it with `pkill -f "claude_tts.py tts-server"` (auto-restarts on the next block).
- **Setup skipped / fell back to `say`** → you're not on Apple Silicon, or Python is older than 3.10 (`python3 -m venv` needs ≥ 3.10).
- **Gaps on long responses (fanless Air)** → that's Voxtral throttling; `preset kokoro` removes it.
- **Slow first model download** → set a `HF_TOKEN` (`~/.cache/huggingface/token`) for higher Hugging Face rate limits.

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

MIT — see [LICENSE](LICENSE). The tool just *calls* the models; their weights keep their own licenses: **Kokoro = Apache-2.0** (commercial OK), **Voxtral = CC-BY-NC-4.0** (Mistral AI, non-commercial).
