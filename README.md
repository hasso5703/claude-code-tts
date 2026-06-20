# claude-code-tts 🔊

**Listen to [Claude Code](https://www.claude.com/product/claude-code)'s replies instead of reading them.**

A tiny, dependency-free tool that hooks Claude Code's `Stop` event, extracts the
final text of each response, cleans the Markdown, and **speaks it out loud** —
on your laptop directly, or on your laptop while Claude Code runs on a remote
server over SSH.

- ✅ **Single file**, Python 3 standard library only — nothing to `pip install`.
- ✅ **macOS & Linux.** macOS uses the built-in `say`; Linux uses `espeak-ng`,
  `spd-say`, `festival`, or `piper`.
- ✅ **Two topologies** out of the box: *local* (Claude runs where you listen) and
  *remote* (Claude runs on a server, you listen on your machine).
- ✅ **Safe installer**: merges into your `~/.claude/settings.json` without
  clobbering existing settings or hooks; fully reversible.
- ✅ **Barge-in**: a new answer interrupts the one still being read.

---

## How it works

Claude Code fires a `Stop` hook every time it finishes a response. The hook reads
the conversation transcript, pulls out the **final answer text**, strips code
blocks / links / emoji / Markdown, and writes one line to a *spool* file. From
there the text is spoken.

```
                          ┌──────────────────────────────────────────┐
   you talk to Claude ──► │ Claude Code  ──(Stop event)──►  hook      │
                          │                                  │ extract │
                          │                                  │ clean   │
                          │                                  ▼         │
                          │                       ~/.claude/tts/spool  │
                          └──────────────────────────────────┬────────┘
                                                              │
          LOCAL mode: the hook speaks here ◄──────────────────┤
                                                              │
          REMOTE mode: `claude-tts listen --ssh host` ◄───────┘  (on your laptop)
                       tails the spool over SSH and speaks locally
```

---

## Requirements

- **Python 3.8+** (already present on macOS and virtually every Linux).
- **A TTS engine**:
  - **macOS** — `say` is built in. ✅ nothing to do.
  - **Linux** — install one: `sudo apt install espeak-ng` (simplest), or
    `speech-dispatcher` (`spd-say`), or [`piper`](https://github.com/rhasspy/piper)
    for neural voices.
- For **remote mode**: passwordless SSH from your laptop to the server
  (an SSH key / agent — see [Remote setup](#scenario-b--claude-code-on-a-remote-server)).

---

## Install

```bash
git clone https://github.com/<you>/claude-code-tts.git
cd claude-code-tts
./install.sh                 # or: python3 claude_tts.py install
```

Then **restart Claude Code** (hooks are loaded at startup) — or open the `/hooks`
menu inside Claude Code to activate it.

That's it for the common case (Claude Code on your laptop). Talk to Claude → hear
the reply.

> The installer copies `claude_tts.py` to `~/.claude/tts/`, writes a config at
> `~/.claude/tts/config.json`, and adds a `Stop` hook to `~/.claude/settings.json`
> (a `.bak` backup is made first).

---

## Scenarios

### Scenario A — Claude Code on your laptop (local mode)

This is the default. Install, restart Claude Code, done. Each response is spoken
on your machine. Pick a voice if you like:

```bash
# macOS — list French voices, then set one
say -v '?' | grep fr_FR
python3 claude_tts.py install --mode local --voice Thomas --rate 210

# Linux (espeak-ng)
python3 claude_tts.py install --mode local --engine espeak-ng --voice fr
```

### Scenario B — Claude Code on a remote server (over SSH)

Claude Code runs on a server (no usable audio there); you want to hear it on your
laptop.

**On the server** — record only, don't try to play audio:
```bash
git clone https://github.com/<you>/claude-code-tts.git && cd claude-code-tts
python3 claude_tts.py install --mode spool
# restart Claude Code on the server
```

**On your laptop** — listen and speak:
```bash
git clone https://github.com/<you>/claude-code-tts.git && cd claude-code-tts
python3 claude_tts.py listen --ssh my-server      # uses your ~/.ssh/config host
```
Leave that running. Now talk to Claude on the server → your laptop reads the
replies. It auto-reconnects if the SSH link drops.

**Passwordless SSH** (so `listen` doesn't prompt). On macOS:
```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```
and in `~/.ssh/config`:
```
Host my-server
  AddKeysToAgent yes
  UseKeychain yes
  IdentityFile ~/.ssh/id_ed25519
```

> Custom remote spool path? `claude-tts listen --ssh my-server --spool '~/.claude/tts/spool.ndjson'`

---

## Configuration

`~/.claude/tts/config.json`:

| Key           | Default   | Meaning |
|---------------|-----------|---------|
| `mode`        | `local`   | `local` = the hook speaks here. `spool` = only record (remote box). |
| `engine`      | `auto`    | `auto`, `say`, `espeak-ng`, `spd-say`, `festival`, `piper`. |
| `voice`       | `""`      | Engine-specific voice (e.g. `Thomas` for `say`, `fr` for `espeak-ng`). |
| `rate`        | `null`    | Engine-specific speed (wpm for `say`/`espeak-ng`; -100..100 for `spd-say`). |
| `barge_in`    | `true`    | A new response interrupts the current one. |
| `max_chars`   | `0`       | Truncate spoken text to N characters (0 = no limit). |
| `piper_model` | `""`      | Path to a piper `.onnx` voice (when `engine=piper`). |

Set values at install time (`--mode`, `--engine`, `--voice`, `--rate`,
`--piper-model`) or edit the JSON directly. Quick env overrides for one run:
`CLAUDE_TTS_VOICE`, `CLAUDE_TTS_RATE`, `CLAUDE_TTS_ENGINE`, `CLAUDE_TTS_MODE`.

---

## Commands

```text
claude-tts install [--mode local|spool] [--engine E] [--voice V] [--rate N] [--piper-model PATH]
claude-tts uninstall [--purge]          # remove the hook (--purge also deletes config/spool)
claude-tts listen [--ssh HOST] [--spool PATH]
claude-tts say "some text"              # test your engine (also reads stdin)
claude-tts doctor                       # diagnostics: OS, engines, config, hook status
```

(Run them as `python3 claude_tts.py <cmd>`, or symlink `claude_tts.py` onto your PATH as `claude-tts`.)

Start with **`claude-tts doctor`** if anything misbehaves.

---

## Neural voices with Piper (optional, Linux/local)

For higher-quality voices than `espeak-ng`, download a [Piper](https://github.com/rhasspy/piper)
voice (`.onnx` + `.onnx.json`) and:
```bash
python3 claude_tts.py install --mode local --engine piper \
  --piper-model ~/piper/fr_FR-siwis-medium.onnx
```
Piper streams raw audio to `aplay`/`paplay`/`ffplay` (auto-detected).

---

## Troubleshooting

- **Nothing is spoken** → did you restart Claude Code after install? Check with
  `claude-tts doctor` (`hook wired: YES`). On the server, watch the spool while
  you talk: `tail -f ~/.claude/tts/spool.ndjson`.
- **No engine found** → install one (`sudo apt install espeak-ng`). `say` is
  macOS-only.
- **Remote `listen` asks for a password** → set up an SSH key / agent (see above).
- **English voice instead of French** → set `--voice` (e.g. `Thomas` on macOS,
  `fr` on espeak-ng).
- **Too verbose / reads progress chatter** → it speaks only the *final* text block
  of a turn by design; if a turn ends on a tool call it falls back to the last
  text block (never the whole turn).

---

## Uninstall

```bash
python3 claude_tts.py uninstall           # remove the hook only
python3 claude_tts.py uninstall --purge   # also delete config + spool + the copied script
```
Then restart Claude Code.

---

## Under the hood

- **Hook**: `~/.claude/settings.json` → `hooks.Stop` runs
  `python3 ~/.claude/tts/claude_tts.py hook`. The hook receives the Stop event
  JSON on stdin (`transcript_path`, `session_id`, …), never blocks Claude, and
  always exits 0.
- **Extraction**: walks the transcript (`*.jsonl`) from the end and takes the
  final run of assistant `text` blocks (skipping `thinking`/`tool_use`).
- **Spool**: `~/.claude/tts/spool.ndjson`, one JSON object per response
  (`{ts, session, cwd, chars, text, raw}`), plus `last.txt` / `last.raw.txt` /
  `last.json` for convenience. Any external script can consume these.
- **De-dupe**: identical consecutive responses are skipped (hash check).

## Privacy

Everything stays on your machines — the hook writes local files and the audio is
synthesized locally (or on the box you SSH to). No data leaves your devices, no
network calls (except your own SSH connection in remote mode).

## Contributing

Issues and pull requests welcome. Windows support (PowerShell `System.Speech`)
is a good first contribution. Keep it **standard-library only**.

## License

MIT — see [LICENSE](LICENSE).
