#!/usr/bin/env python3
"""
claude-code-tts - Listen to Claude Code's responses instead of reading them.

A single-file, dependency-free tool (Python 3 standard library only) that hooks
Claude Code's `Stop` event, extracts the final response text, cleans the
Markdown for speech, and speaks it aloud - either locally or on a remote
machine over SSH.

TTS engines (auto-detected): macOS `say`; Linux `spd-say`, `espeak-ng`,
`festival`, or `piper`.

Subcommands:
  install        Wire the Stop hook into ~/.claude/settings.json (idempotent).
  uninstall      Remove the hook again.
  hook           Internal: invoked by Claude Code on each Stop event.
  listen         Speak responses from a spool (local file, or remote via --ssh).
  say TEXT       Speak TEXT now (test your engine).  TEXT may also come on stdin.
  speak-file P   Internal: speak the contents of file P (used by local mode).
  doctor         Print diagnostics (OS, engines, config, hook status).

See README.md for the full guide.  MIT licensed.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

VERSION = "1.0.1"

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
TTS_DIR = os.path.join(CLAUDE_DIR, "tts")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
CONFIG_PATH = os.path.join(TTS_DIR, "config.json")
SPOOL = os.path.join(TTS_DIR, "spool.ndjson")
LAST_TXT = os.path.join(TTS_DIR, "last.txt")
LAST_RAW = os.path.join(TTS_DIR, "last.raw.txt")
LAST_JSON = os.path.join(TTS_DIR, "last.json")
HASH_FILE = os.path.join(TTS_DIR, ".last.hash")
PID_FILE = os.path.join(TTS_DIR, ".speaking.pid")
INSTALLED_SELF = os.path.join(TTS_DIR, "claude_tts.py")
HOOK_MARKER = "claude_tts.py"          # identifies our hook inside settings.json
REMOTE_SPOOL_DEFAULT = "~/.claude/tts/spool.ndjson"
SPOOL_MAX_BYTES = 5 * 1024 * 1024

DEFAULT_CONFIG = {
    "mode": "local",      # "local": the hook speaks on this machine.
                          # "spool": the hook only writes the spool (remote box;
                          #          a `listen` client elsewhere does the talking).
    "engine": "auto",     # auto | say | espeak-ng | spd-say | festival | piper
    "voice": "",          # engine-specific voice name; "" = engine default
    "rate": None,         # engine-specific speed; None = engine default
    "barge_in": True,     # a new response interrupts speech in progress
    "max_chars": 0,       # truncate spoken text to N chars (0 = no limit)
    "piper_model": "",    # path to a piper .onnx voice (engine=piper)
}


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def ensure_dir():
    os.makedirs(TTS_DIR, exist_ok=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k in DEFAULT_CONFIG:
                cfg[k] = v
    except Exception:
        pass
    # environment overrides (handy for ad-hoc tweaks)
    env = os.environ
    if env.get("CLAUDE_TTS_ENGINE"):
        cfg["engine"] = env["CLAUDE_TTS_ENGINE"]
    if env.get("CLAUDE_TTS_VOICE"):
        cfg["voice"] = env["CLAUDE_TTS_VOICE"]
    if env.get("CLAUDE_TTS_RATE"):
        try:
            cfg["rate"] = int(env["CLAUDE_TTS_RATE"])
        except ValueError:
            pass
    if env.get("CLAUDE_TTS_MODE"):
        cfg["mode"] = env["CLAUDE_TTS_MODE"]
    return cfg


def save_config(cfg):
    ensure_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# Transcript extraction                                                        #
# --------------------------------------------------------------------------- #
def _is_human_user(o):
    """True for a real typed prompt (not a tool_result, not meta)."""
    if o.get("type") != "user":
        return False
    if o.get("isMeta"):
        return False
    if o.get("toolUseResult") is not None:
        return False
    m = o.get("message", {})
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, str):
        return True
    if isinstance(c, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
    return False


def _assistant_text(o):
    """Join the 'text' blocks (never 'thinking'/'tool_use') of one assistant line."""
    out = []
    for b in o.get("message", {}).get("content", []):
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text", "")
            if t.strip():
                out.append(t)
    return "\n\n".join(out)


def _load_objs(path):
    objs = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                objs.append(json.loads(ln))
            except Exception:
                pass
    return objs


def _trail_text(objs):
    """The run of assistant text at the very end (walk back, stop at first
    'user' line). Empty when the transcript currently ends on a tool call /
    tool_result - i.e. the turn's closing assistant text isn't written yet."""
    trail = []
    for o in reversed(objs):
        t = o.get("type")
        if t == "user":
            break
        if t == "assistant":
            tx = _assistant_text(o)
            if tx.strip():
                trail.insert(0, tx)
    return "\n\n".join(trail).strip()


def final_answer_text(path):
    """The text to speak: the final run of assistant text. On a real Stop the
    turn ends with text, so this is the conclusion - not the inter-tool "let me
    check..." chatter. Fallback (no closing text): the most recent assistant
    text block - bounded, never the whole turn."""
    objs = _load_objs(path)
    trail = _trail_text(objs)
    if trail:
        return trail
    for o in reversed(objs):
        if o.get("type") == "assistant":
            tx = _assistant_text(o)
            if tx.strip():
                return tx.strip()
    return ""


def final_answer_text_settled(path, timeout=3.0, interval=0.12):
    """Wait briefly for the turn's closing assistant text to land in the JSONL.

    Claude Code can fire the Stop hook a hair before the final assistant
    message is flushed to the transcript - especially when the turn ended on a
    tool call, so the last line is a tool_result ('user') line. Reading then
    would speak the PREVIOUS turn's text (off-by-one). Poll until assistant
    text appears after the last user line, or give up and best-effort."""
    deadline = time.time() + timeout
    while True:
        try:
            trail = _trail_text(_load_objs(path))
        except Exception:
            trail = ""
        if trail:
            return trail
        if time.time() >= deadline:
            return final_answer_text(path)
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# Markdown -> speech cleaning                                                   #
# --------------------------------------------------------------------------- #
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002190-\U000021FF\U00002300-\U000027BF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF]",
    flags=re.UNICODE,
)


def clean_for_tts(md):
    s = md
    s = re.sub(r"```[^\n]*\n.*?```", " . code block. ", s, flags=re.S)
    s = re.sub(r"```.*?```", " . ", s, flags=re.S)

    def _inline(m):
        c = m.group(1)
        return c if (len(c) <= 24 and "/" not in c) else " "

    s = re.sub(r"`([^`]+)`", _inline, s)
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", "link", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.M)
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.M)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*\|.*\|\s*$", "", s, flags=re.M)
    s = re.sub(r"^\s*[:\-\| ]+\s*$", "", s, flags=re.M)
    s = re.sub(r"(\*\*|\*|__|_|~~)", "", s)
    s = _EMOJI.sub("", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", ". ", s)
    s = re.sub(r"\n", ". ", s)
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    s = re.sub(r"\.{2,}", ".", s)
    s = re.sub(r"(\.\s*){2,}", ". ", s)
    return s.strip()


# --------------------------------------------------------------------------- #
# Spool / output files                                                          #
# --------------------------------------------------------------------------- #
def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def _trim_spool():
    try:
        if os.path.getsize(SPOOL) <= SPOOL_MAX_BYTES:
            return
        with open(SPOOL, encoding="utf-8") as f:
            lines = f.readlines()
        _atomic_write(SPOOL, "".join(lines[-200:]))
    except Exception:
        pass


def write_outputs(rec, h):
    ensure_dir()
    line = json.dumps(rec, ensure_ascii=False)
    try:
        import fcntl
        with open(SPOOL, "a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        with open(SPOOL, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    try:
        _atomic_write(LAST_TXT, rec["text"] + "\n")
        _atomic_write(LAST_RAW, rec["raw"] + "\n")
        _atomic_write(LAST_JSON, line + "\n")
        _atomic_write(HASH_FILE, h)
    except Exception:
        pass
    _trim_spool()


# --------------------------------------------------------------------------- #
# TTS engines                                                                   #
# --------------------------------------------------------------------------- #
def which(b):
    return shutil.which(b)


def detect_engine(cfg):
    e = cfg.get("engine", "auto")
    if e and e != "auto":
        return e if which(e.split()[0]) or e == "say" else (e if which(e) else None)
    if sys.platform == "darwin" and which("say"):
        return "say"
    if cfg.get("piper_model") and which("piper"):
        return "piper"
    for cand in ("spd-say", "espeak-ng", "festival", "say"):
        if which(cand):
            return cand
    return None


def _raw_player():
    """argv to play raw s16le 22050 mono from stdin (for piper)."""
    if which("aplay"):
        return ["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"]
    if which("paplay"):
        return ["paplay", "--raw", "--rate=22050", "--format=s16le", "--channels=1"]
    if which("ffplay"):
        return ["ffplay", "-loglevel", "quiet", "-autoexit", "-nodisp",
                "-f", "s16le", "-ar", "22050", "-i", "-"]
    return None


def _write_pid(pid):
    try:
        _atomic_write(PID_FILE, str(pid))
    except Exception:
        pass


def _kill_prev():
    try:
        pid = int(open(PID_FILE, encoding="utf-8").read().strip())
    except Exception:
        return
    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def _popen_kwargs():
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "posix":
        kw["start_new_session"] = True
    return kw


def speak(cfg, text):
    """Speak `text` synchronously (blocks until done)."""
    text = (text or "").strip()
    if not text:
        return
    if cfg.get("max_chars"):
        text = text[: int(cfg["max_chars"])]
    engine = detect_engine(cfg)
    if not engine:
        sys.stderr.write("[claude-tts] no TTS engine found "
                         "(install macOS say, or espeak-ng / spd-say on Linux)\n")
        return
    if cfg.get("barge_in", True):
        _kill_prev()
    voice = cfg.get("voice") or ""
    rate = cfg.get("rate")

    try:
        if engine == "say":
            argv = ["say"]
            if voice:
                argv += ["-v", voice]
            if rate:
                argv += ["-r", str(rate)]
            argv += ["--", text]
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **_popen_kwargs())
            _write_pid(p.pid)
            p.wait()

        elif engine == "espeak-ng":
            argv = ["espeak-ng", "-v", voice or "fr"]
            if rate:
                argv += ["-s", str(rate)]
            argv += ["--stdin"]
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, **_popen_kwargs())
            _write_pid(p.pid)
            p.communicate(text.encode("utf-8"))

        elif engine == "spd-say":
            argv = ["spd-say", "-w"]
            if voice:
                argv += ["-l", voice]
            if rate is not None and -100 <= rate <= 100:
                argv += ["-r", str(rate)]
            argv += ["--", text]
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **_popen_kwargs())
            _write_pid(p.pid)
            p.wait()

        elif engine == "festival":
            p = subprocess.Popen(["festival", "--tts"], stdin=subprocess.PIPE,
                                 **_popen_kwargs())
            _write_pid(p.pid)
            p.communicate(text.encode("utf-8"))

        elif engine == "piper":
            model = cfg.get("piper_model")
            player = _raw_player()
            if not model or not player:
                sys.stderr.write("[claude-tts] piper needs piper_model + a raw "
                                 "player (aplay/paplay/ffplay)\n")
                return
            p1 = subprocess.Popen(["piper", "-m", model, "--output-raw"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL,
                                  start_new_session=(os.name == "posix"))
            p2 = subprocess.Popen(player, stdin=p1.stdout,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            p1.stdout.close()
            _write_pid(p1.pid)
            try:
                p1.stdin.write(text.encode("utf-8"))
                p1.stdin.close()
            except Exception:
                pass
            p2.wait()

        else:
            sys.stderr.write("[claude-tts] unknown engine: %s\n" % engine)
    except FileNotFoundError:
        sys.stderr.write("[claude-tts] engine '%s' not found on PATH\n" % engine)
    except Exception as e:
        sys.stderr.write("[claude-tts] speak error: %s\n" % e)


def speak_from_line(cfg, line):
    line = line.strip()
    if not line:
        return
    try:
        text = json.loads(line).get("text", "")
    except Exception:
        text = ""
    if text:
        speak(cfg, text)


# --------------------------------------------------------------------------- #
# Tail (local spool)                                                            #
# --------------------------------------------------------------------------- #
def tail_lines(path):
    """Yield new lines appended to `path`; handles rotation/truncation."""
    while not os.path.exists(path):
        time.sleep(0.5)
    f = open(path, "r", encoding="utf-8", errors="replace")
    try:
        f.seek(0, os.SEEK_END)
        inode = os.fstat(f.fileno()).st_ino
        buf = ""
        while True:
            chunk = f.readline()
            if chunk:
                buf += chunk
                if buf.endswith("\n"):
                    yield buf
                    buf = ""
            else:
                time.sleep(0.3)
                try:
                    st = os.stat(path)
                    if st.st_ino != inode or st.st_size < f.tell():
                        f.close()
                        f = open(path, "r", encoding="utf-8", errors="replace")
                        inode = os.fstat(f.fileno()).st_ino
                        buf = ""
                except FileNotFoundError:
                    pass
    finally:
        try:
            f.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Commands                                                                      #
# --------------------------------------------------------------------------- #
def cmd_hook(_args):
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tpath = data.get("transcript_path")
    if not tpath or not os.path.exists(tpath):
        return 0
    try:
        raw = final_answer_text_settled(tpath)
    except Exception:
        return 0
    if not raw.strip():
        return 0
    clean = clean_for_tts(raw)
    if not clean:
        return 0

    h = hashlib.sha1(clean.encode("utf-8")).hexdigest()
    try:
        if os.path.exists(HASH_FILE) and open(HASH_FILE, encoding="utf-8").read().strip() == h:
            return 0
    except Exception:
        pass

    rec = {
        "ts": time.time(),
        "session": data.get("session_id"),
        "cwd": data.get("cwd"),
        "chars": len(clean),
        "text": clean,
        "raw": raw,
    }
    write_outputs(rec, h)

    cfg = load_config()
    if cfg.get("mode", "local") == "local":
        _spawn_speak_detached()
    return 0


def _spawn_speak_detached():
    py = sys.executable or "python3"
    selfpath = os.path.abspath(__file__)
    try:
        subprocess.Popen(
            [py, selfpath, "speak-file", LAST_TXT],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name == "posix"),
        )
    except Exception:
        pass


def cmd_speak_file(args):
    try:
        text = open(args.path, encoding="utf-8").read()
    except Exception:
        return 0
    speak(load_config(), text)
    return 0


def cmd_say(args):
    text = args.text
    if not text:
        text = sys.stdin.read()
    speak(load_config(), text)
    return 0


def cmd_listen(args):
    cfg = load_config()
    eng = detect_engine(cfg) or "NONE"
    if args.ssh:
        remote = args.spool or REMOTE_SPOOL_DEFAULT
        print(f"[claude-tts] listening over ssh {args.ssh}:{remote}  engine={eng}  "
              f"(Ctrl-C to quit)")
        _listen_ssh(cfg, args.ssh, remote)
    else:
        path = os.path.expanduser(args.spool) if args.spool else SPOOL
        print(f"[claude-tts] listening (local): {path}  engine={eng}  (Ctrl-C to quit)")
        try:
            for line in tail_lines(path):
                speak_from_line(cfg, line)
        except KeyboardInterrupt:
            pass
    return 0


def _listen_ssh(cfg, host, remote_spool):
    cmd = ["ssh", "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
           "-o", "ConnectTimeout=10", host,
           f"tail -n0 -F '{remote_spool}' 2>/dev/null"]
    while True:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True)
            for line in p.stdout:
                speak_from_line(cfg, line)
        except KeyboardInterrupt:
            return
        except Exception:
            pass
        print("[claude-tts] connection lost; reconnecting in 3s (Ctrl-C to quit)")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            return


def _read_settings():
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _entry_is_ours(entry):
    for h in entry.get("hooks", []):
        if HOOK_MARKER in (h.get("command", "") or ""):
            return True
    return False


def cmd_install(args):
    ensure_dir()
    selfpath = os.path.abspath(__file__)
    if os.path.abspath(INSTALLED_SELF) != selfpath:
        shutil.copyfile(selfpath, INSTALLED_SELF)
    try:
        os.chmod(INSTALLED_SELF, 0o755)
    except Exception:
        pass

    cfg = load_config()
    if args.mode:
        cfg["mode"] = args.mode
    if args.engine:
        cfg["engine"] = args.engine
    if args.voice:
        cfg["voice"] = args.voice
    if args.rate is not None:
        cfg["rate"] = args.rate
    if args.piper_model:
        cfg["piper_model"] = args.piper_model
    save_config(cfg)

    import shlex
    py = sys.executable or "python3"
    hook_cmd = f"{shlex.quote(py)} {shlex.quote(INSTALLED_SELF)} hook"

    settings = _read_settings()
    if os.path.exists(SETTINGS):
        try:
            shutil.copy2(SETTINGS, SETTINGS + ".bak")
        except Exception:
            pass
    hooks = settings.setdefault("hooks", {})
    stop = [e for e in hooks.get("Stop", []) if not _entry_is_ours(e)]
    stop.append({"hooks": [{"type": "command", "command": hook_cmd}]})
    hooks["Stop"] = stop
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print("claude-code-tts installed.")
    print("  hook    :", hook_cmd)
    print("  config  :", CONFIG_PATH)
    print("  mode    :", cfg["mode"])
    print("  engine  :", detect_engine(cfg) or "NONE - install say/espeak-ng/spd-say")
    if cfg["mode"] == "local":
        print("\nLocal mode: responses will be spoken on THIS machine.")
    else:
        print("\nSpool mode: this machine only records responses. On your audio "
              "machine run:\n  claude-tts listen --ssh <this-host>")
    print("\n>>> Restart Claude Code (or run /hooks) to activate the hook. <<<")
    return 0


def cmd_uninstall(args):
    settings = _read_settings()
    hooks = settings.get("hooks", {})
    if "Stop" in hooks:
        hooks["Stop"] = [e for e in hooks["Stop"] if not _entry_is_ours(e)]
        if not hooks["Stop"]:
            del hooks["Stop"]
    if not hooks:
        settings.pop("hooks", None)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("hook removed from", SETTINGS)
    if args.purge:
        for p in (SPOOL, LAST_TXT, LAST_RAW, LAST_JSON, HASH_FILE, PID_FILE,
                  CONFIG_PATH, INSTALLED_SELF):
            try:
                os.remove(p)
            except Exception:
                pass
        print("purged", TTS_DIR, "files")
    print("Restart Claude Code to fully deactivate.")
    return 0


def cmd_doctor(_args):
    cfg = load_config()
    print("claude-code-tts", VERSION)
    print("python      :", sys.version.split()[0], "(", sys.executable, ")")
    print("platform    :", sys.platform)
    engines = [e for e in ("say", "spd-say", "espeak-ng", "festival", "piper")
               if which(e)]
    print("engines     :", ", ".join(engines) or "NONE FOUND")
    print("config file :", CONFIG_PATH, "(exists)" if os.path.exists(CONFIG_PATH) else "(default)")
    print("  mode      :", cfg["mode"])
    print("  engine    :", cfg["engine"], "->", detect_engine(cfg) or "NONE")
    print("  voice     :", cfg["voice"] or "(default)")
    print("  rate      :", cfg["rate"])
    print("  barge_in  :", cfg["barge_in"])
    settings = _read_settings()
    stop = settings.get("hooks", {}).get("Stop", [])
    ours = [e for e in stop if _entry_is_ours(e)]
    print("hook wired  :", "YES" if ours else "NO", "(in", SETTINGS + ")")
    if ours:
        print("  command   :", ours[0]["hooks"][0]["command"])
    print("spool       :", SPOOL,
          "(%d lines)" % sum(1 for _ in open(SPOOL, encoding="utf-8"))
          if os.path.exists(SPOOL) else "(none yet)")
    if not engines:
        print("\nNo engine: macOS has `say` built in; on Linux try "
              "`sudo apt install espeak-ng` or `speech-dispatcher`.")
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                           #
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(prog="claude-tts",
                                description="Listen to Claude Code's responses.")
    p.add_argument("--version", action="version", version="claude-code-tts " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("install", help="wire the Stop hook into settings.json")
    pi.add_argument("--mode", choices=["local", "spool"])
    pi.add_argument("--engine")
    pi.add_argument("--voice")
    pi.add_argument("--rate", type=int)
    pi.add_argument("--piper-model", dest="piper_model")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="remove the hook")
    pu.add_argument("--purge", action="store_true", help="also delete config/spool")
    pu.set_defaults(func=cmd_uninstall)

    ph = sub.add_parser("hook", help="(internal) Claude Code Stop hook")
    ph.set_defaults(func=cmd_hook)

    pl = sub.add_parser("listen", help="speak responses from a spool")
    pl.add_argument("--ssh", metavar="HOST", help="tail the spool on a remote host")
    pl.add_argument("--spool", help="spool path (local) or remote path (with --ssh)")
    pl.set_defaults(func=cmd_listen)

    ps = sub.add_parser("say", help="speak TEXT now (or stdin)")
    ps.add_argument("text", nargs="?")
    ps.set_defaults(func=cmd_say)

    psf = sub.add_parser("speak-file", help="(internal) speak a file's contents")
    psf.add_argument("path")
    psf.set_defaults(func=cmd_speak_file)

    pd = sub.add_parser("doctor", help="print diagnostics")
    pd.set_defaults(func=cmd_doctor)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        # the hook must never disturb Claude Code
        if getattr(args, "cmd", None) == "hook":
            return 0
        sys.stderr.write("[claude-tts] %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
