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

VERSION = "1.3.0"

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
QUEUE_DIR = os.path.join(TTS_DIR, "queue")   # per-session audio queues (stream mode)
DEBUG_FLAG = os.path.join(TTS_DIR, "DEBUG")  # touch this file to log hook activity
HOOK_LOG = os.path.join(TTS_DIR, "hook.log")
DAEMON_IDLE = 1800   # stream daemon exits after this many idle seconds
DEFAULT_VOXTRAL_MODEL = "mlx-community/Voxtral-4B-TTS-2603-mlx-4bit"
DEFAULT_VOXTRAL_PORT = 8765
SERVER_IDLE = 1800   # Voxtral server exits after this many idle seconds
VENV_PY = os.path.join(TTS_DIR, "venv", "bin", "python")  # venv with mlx-audio
SERVER_PID = os.path.join(TTS_DIR, "server.pid")
INSTALLED_SELF = os.path.join(TTS_DIR, "claude_tts.py")
HOOK_MARKER = "claude_tts.py"          # identifies our hook inside settings.json
# Events we wire. Stream mode runs a transcript-tailing daemon: UserPromptSubmit
# starts it (and hushes the prior turn), PreToolUse keeps it alive, SessionEnd
# stops it. Single mode just speaks the final answer at Stop.
STREAM_EVENTS = ["PreToolUse", "UserPromptSubmit", "SessionEnd"]
SINGLE_EVENTS = ["Stop"]
ALL_EVENTS = ["PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "SessionEnd"]
REMOTE_SPOOL_DEFAULT = "~/.claude/tts/spool.ndjson"
SPOOL_MAX_BYTES = 5 * 1024 * 1024

DEFAULT_CONFIG = {
    "mode": "local",      # "local": the hook speaks on this machine.
                          # "spool": the hook only writes the spool (remote box;
                          #          a `listen` client elsewhere does the talking).
    "engine": "auto",     # auto | say | espeak-ng | spd-say | festival | piper | voxtral
    "voice": "",          # engine-specific voice name; "" = engine default
    "rate": None,         # engine-specific speed; None = engine default
    "barge_in": True,     # a new response interrupts speech in progress
    "stream": True,       # speak each block live at tool boundaries, vs one shot
                          #   at end of turn. Block-level (no token streaming).
    "max_chars": 0,       # truncate spoken text to N chars (0 = no limit)
    "piper_model": "",    # path to a piper .onnx voice (engine=piper)
    # --- engine=voxtral: neural TTS (Mistral) via a persistent mlx-audio server ---
    "speed": 1.0,         # playback speed hint (Voxtral currently ignores it)
    "say_voice": "",      # macOS `say` voice used as fallback if the server is down
    "voxtral_model": DEFAULT_VOXTRAL_MODEL,
    "voxtral_port": DEFAULT_VOXTRAL_PORT,
    "voxtral_python": "", # python with mlx-audio; "" = the bundled venv
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
    if e == "voxtral":
        return "voxtral"
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


def _write_pid(pid, path=PID_FILE):
    try:
        _atomic_write(path, str(pid))
    except Exception:
        pass


def _kill_prev(path=PID_FILE):
    try:
        pid = int(open(path, encoding="utf-8").read().strip())
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


def speak(cfg, text, pidfile=PID_FILE, barge=None):
    """Speak `text` synchronously (blocks until done).

    `pidfile` records the player PID so it can be interrupted later. `barge`
    overrides the configured barge-in: the sequential queue passes barge=False
    so chained blocks play in full instead of cutting each other off."""
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
    if engine == "voxtral":
        _speak_voxtral(cfg, text, pidfile, barge)
        return
    if (cfg.get("barge_in", True) if barge is None else barge):
        _kill_prev(pidfile)
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
            _write_pid(p.pid, pidfile)
            p.wait()

        elif engine == "espeak-ng":
            argv = ["espeak-ng", "-v", voice or "fr"]
            if rate:
                argv += ["-s", str(rate)]
            argv += ["--stdin"]
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, **_popen_kwargs())
            _write_pid(p.pid, pidfile)
            p.communicate(text.encode("utf-8"))

        elif engine == "spd-say":
            argv = ["spd-say", "-w"]
            if voice:
                argv += ["-l", voice]
            if rate is not None and -100 <= rate <= 100:
                argv += ["-r", str(rate)]
            argv += ["--", text]
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **_popen_kwargs())
            _write_pid(p.pid, pidfile)
            p.wait()

        elif engine == "festival":
            p = subprocess.Popen(["festival", "--tts"], stdin=subprocess.PIPE,
                                 **_popen_kwargs())
            _write_pid(p.pid, pidfile)
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
            _write_pid(p1.pid, pidfile)
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
# Streaming: speak each block live, at tool boundaries                          #
# --------------------------------------------------------------------------- #
def _sid(session):
    s = "".join(c if (c.isalnum() or c in "-_") else "_" for c in (session or "default"))
    return s or "default"


def _qfile(s):   return os.path.join(QUEUE_DIR, _sid(s) + ".q")
def _qlock(s):   return os.path.join(QUEUE_DIR, _sid(s) + ".lock")
def _qpid(s):    return os.path.join(QUEUE_DIR, _sid(s) + ".pid")
def _dpid(s):    return os.path.join(QUEUE_DIR, _sid(s) + ".daemon.pid")
def _dlock(s):   return os.path.join(QUEUE_DIR, _sid(s) + ".daemon.lock")


def _qdir():
    try:
        os.makedirs(QUEUE_DIR, exist_ok=True)
    except Exception:
        pass


def _dbg(msg):
    """Append a diagnostic line to hook.log, but only when ~/.claude/tts/DEBUG
    exists. Off by default - zero overhead and silent for normal use."""
    try:
        if not os.path.exists(DEBUG_FLAG):
            return
        with open(HOOK_LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _flock_ex(f, blocking=True):
    """Best-effort exclusive flock; True on success, False if held (non-blocking)
    or unsupported (e.g. no fcntl)."""
    try:
        import fcntl
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), flags)
        return True
    except Exception:
        return False


def _q_append(s, text):
    _qdir()
    try:
        with open(_qfile(s), "a", encoding="utf-8") as f:
            _flock_ex(f)
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
            f.flush()
    except Exception:
        pass


def _q_pop(s):
    """Remove and return the first queued text (FIFO), atomically."""
    try:
        f = open(_qfile(s), "r+", encoding="utf-8")
    except Exception:
        return ""
    try:
        _flock_ex(f)
        lines = f.readlines()
        if not lines:
            return ""
        first, rest = lines[0], lines[1:]
        f.seek(0)
        f.truncate()
        f.writelines(rest)
        f.flush()
    finally:
        try:
            f.close()
        except Exception:
            pass
    try:
        return json.loads(first).get("text", "")
    except Exception:
        return ""


def _q_clear(s):
    try:
        f = open(_qfile(s), "r+", encoding="utf-8")
    except Exception:
        return
    try:
        _flock_ex(f)
        f.seek(0)
        f.truncate()
    finally:
        try:
            f.close()
        except Exception:
            pass


def _drain(session):
    """Speak a session's queued blocks one after another (no overlap). Only one
    drainer runs per session: a second exits immediately and lets this one pick
    up whatever it just enqueued."""
    cfg = load_config()
    _qdir()
    try:
        lf = open(_qlock(session), "w")
    except Exception:
        lf = None
    if lf is not None and not _flock_ex(lf, blocking=False):
        return  # another drainer already owns this session
    try:
        if cfg.get("engine") == "voxtral":
            _drain_voxtral(session, cfg)
        else:
            while True:
                text = _q_pop(session)
                if not text:
                    time.sleep(0.05)        # grace for a just-appended block
                    text = _q_pop(session)
                    if not text:
                        break
                speak(cfg, text, pidfile=_qpid(session), barge=False)
    finally:
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass


def _spawn_drainer(session):
    py = sys.executable or "python3"
    selfpath = os.path.abspath(__file__)
    try:
        subprocess.Popen([py, selfpath, "drain", session],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=(os.name == "posix"))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Stream daemon: tail the transcript, speak each block the instant it lands     #
# --------------------------------------------------------------------------- #
def _proc_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _daemon_running(session):
    try:
        return _proc_alive(int(open(_dpid(session), encoding="utf-8").read().strip()))
    except Exception:
        return False


def _start_daemon(session, transcript):
    """Ensure a transcript-tailing speak daemon is running for this session."""
    if not transcript or not session or _daemon_running(session):
        return
    py = sys.executable or "python3"
    selfpath = os.path.abspath(__file__)
    try:
        subprocess.Popen([py, selfpath, "daemon", session, transcript],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=(os.name == "posix"))
    except Exception:
        pass


def _signal_daemon(session, sig):
    try:
        os.kill(int(open(_dpid(session), encoding="utf-8").read().strip()), sig)
    except Exception:
        pass


def _daemon_hush(session):
    """New turn / barge-in: drop pending speech and cut off the current block."""
    _q_clear(session)
    _kill_prev(_qpid(session))
    _signal_daemon(session, signal.SIGUSR1)


def _stop_daemon(session):
    _signal_daemon(session, signal.SIGTERM)


def _turn_start_lineno(transcript):
    """Raw line count up to and including the last real user prompt - where the
    current turn begins. The daemon skips this many lines on start so it never
    replays earlier turns, only speaks what comes next."""
    start = 0
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for i, ln in enumerate(f):
                s = ln.strip()
                if not s:
                    continue
                try:
                    o = json.loads(s)
                except Exception:
                    continue
                if _is_human_user(o):
                    start = i + 1
    except Exception:
        return 0
    return start


def _hard_wrap(s, max_len):
    out = []
    while len(s) > max_len:
        cut = s.rfind(", ", 0, max_len)
        if cut < max_len // 2:
            cut = s.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        out.append(s[:cut + 1].strip())
        s = s[cut + 1:].strip()
    if s:
        out.append(s)
    return out


def _split_sentences(text, max_len=220):
    """Group text into sentence-sized chunks (<= max_len chars). Synthesizing
    sentence by sentence keeps each generation short - lower latency, far less
    thermal build-up, and a runaway generation can only ruin one short chunk."""
    text = " ".join((text or "").split())
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    pieces = re.split(r"(?<=[.!?…])\s+", text)
    chunks, cur = [], ""
    for p in pieces:
        if not p:
            continue
        if len(p) > max_len:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_hard_wrap(p, max_len))
        elif cur and len(cur) + 1 + len(p) > max_len:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip() if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def _cleanup_stale_wavs(max_age=300):
    """Remove orphan spk-*.wav left behind by a killed player (e.g. a crash)."""
    try:
        now = time.time()
        for fn in os.listdir(QUEUE_DIR):
            if fn.startswith("spk-") and fn.endswith(".wav"):
                p = os.path.join(QUEUE_DIR, fn)
                try:
                    if now - os.path.getmtime(p) > max_age:
                        os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass


def _daemon_handle(session, line, spoken):
    s = line.strip()
    if not s:
        return
    try:
        o = json.loads(s)
    except Exception:
        return
    if _is_human_user(o):
        # A new turn began -> hush and forget (new blocks carry new ids anyway).
        spoken.clear()
        _q_clear(session)
        _kill_prev(_qpid(session))
        return
    if o.get("type") != "assistant":
        return
    raw = _assistant_text(o)
    if not raw.strip():
        return
    clean = clean_for_tts(raw)
    if not clean:
        return
    key = o.get("uuid") or hashlib.sha1(clean.encode("utf-8")).hexdigest()
    if key in spoken:
        return
    spoken.add(key)
    rec = {"ts": time.time(), "session": session, "cwd": "",
           "chars": len(clean), "text": clean, "raw": raw}
    write_outputs(rec, hashlib.sha1(clean.encode("utf-8")).hexdigest())
    if load_config().get("mode", "local") == "local":
        for chunk in _split_sentences(clean):
            _q_append(session, chunk)
        _spawn_drainer(session)


def _daemon(session, transcript, from_end=False):
    """Tail the session transcript and speak each assistant text block the moment
    its line lands - decoupled from tool boundaries, so no off-by-one and no wait
    for the next tool. Speaking itself runs in the sequential drainer; this
    process only watches and enqueues. `from_end` starts at EOF (for a manual
    mid-turn start), otherwise it begins at the current turn's first line."""
    _qdir()
    try:
        lf = open(_dlock(session), "w")
        import fcntl
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        return  # another daemon already owns this session (or no fcntl)
    try:
        _atomic_write(_dpid(session), str(os.getpid()))
    except Exception:
        pass

    def _on_hush(*_a):
        _q_clear(session)
        _kill_prev(_qpid(session))
    try:
        signal.signal(signal.SIGUSR1, _on_hush)
    except Exception:
        pass

    spoken = set()
    while not os.path.exists(transcript):
        time.sleep(0.3)
    try:
        f = open(transcript, "r", encoding="utf-8", errors="replace")
    except Exception:
        return
    if from_end:
        try:
            f.seek(0, os.SEEK_END)
        except Exception:
            pass
    else:
        for _ in range(_turn_start_lineno(transcript)):
            if not f.readline():
                break
    try:
        inode = os.fstat(f.fileno()).st_ino
    except Exception:
        inode = None
    last = time.time()
    buf = ""
    try:
        while True:
            line = f.readline()
            if line:
                last = time.time()
                buf += line
                if buf.endswith("\n"):
                    _daemon_handle(session, buf, spoken)
                    buf = ""
                continue
            if time.time() - last > DAEMON_IDLE:
                break
            time.sleep(0.15)
            try:
                st = os.stat(transcript)
                if inode is not None and (st.st_ino != inode or st.st_size < f.tell()):
                    f.close()
                    f = open(transcript, "r", encoding="utf-8", errors="replace")
                    inode = os.fstat(f.fileno()).st_ino
                    buf = ""
            except FileNotFoundError:
                break
    finally:
        try:
            f.close()
        except Exception:
            pass
        try:
            os.remove(_dpid(session))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Voxtral neural TTS (Mistral) via a persistent local mlx-audio server          #
# --------------------------------------------------------------------------- #
def _voxtral_py(cfg):
    return os.path.expanduser(cfg.get("voxtral_python") or VENV_PY)


def _server_port(cfg):
    try:
        return int(cfg.get("voxtral_port") or DEFAULT_VOXTRAL_PORT)
    except Exception:
        return DEFAULT_VOXTRAL_PORT


def _server_running(cfg):
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % _server_port(cfg), timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _port_open(port):
    """True if something is already listening on the port (server loading OR
    ready). Used to avoid spawning a duplicate while the model is loading and
    /health still returns 503."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _start_server(cfg):
    """Ensure the persistent Voxtral server (venv python + mlx-audio) is up. The
    server loads the model once and synthesizes on demand."""
    if _port_open(_server_port(cfg)):
        return  # already bound (loading or ready) - don't spawn a duplicate
    py = _voxtral_py(cfg)
    if not os.path.exists(py):
        return
    try:
        subprocess.Popen([py, os.path.abspath(__file__), "tts-server",
                          "--port", str(_server_port(cfg))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=(os.name == "posix"))
    except Exception:
        pass


def _stop_server():
    try:
        os.kill(int(open(SERVER_PID, encoding="utf-8").read().strip()),
                signal.SIGTERM)
    except Exception:
        pass


def _voxtral_available():
    """Voxtral runs on MLX, which is macOS + Apple Silicon only."""
    import platform
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _venv_has_deps(py):
    try:
        return subprocess.run([py, "-c", "import mlx_audio, mistral_common"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:
        return False


def _bootstrap_voxtral():
    """Create the bundled venv and install mlx-audio + mistral-common[audio] so
    a fresh clone works end to end. Returns (ok, message). Idempotent."""
    if not _voxtral_available():
        return False, "Voxtral needs macOS on Apple Silicon (MLX)."
    venv_dir = os.path.join(TTS_DIR, "venv")
    py = VENV_PY
    if not os.path.exists(py):
        print("  creating venv at", venv_dir, "...")
        ensure_dir()
        r = subprocess.run([sys.executable, "-m", "venv", venv_dir])
        if r.returncode != 0 or not os.path.exists(py):
            return False, "could not create venv (need `python3 -m venv`)"
    if not _venv_has_deps(py):
        print("  installing mlx-audio + mistral-common[audio] (a few minutes)...")
        subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
        r = subprocess.run([py, "-m", "pip", "install", "-q",
                            "mlx-audio", "mistral-common[audio]"])
        if r.returncode != 0 or not _venv_has_deps(py):
            return False, "pip install of mlx-audio failed"
    return True, "ok"


def _model_cached(model):
    cache = os.path.expanduser("~/.cache/huggingface/hub/models--"
                               + model.replace("/", "--"))
    return os.path.isdir(cache) and any(
        f.endswith(".safetensors")
        for _r, _d, fs in os.walk(cache) for f in fs)


def _download_voxtral_model(cfg):
    model = cfg.get("voxtral_model") or DEFAULT_VOXTRAL_MODEL
    if _model_cached(model):
        return True
    py = _voxtral_py(cfg)
    if not os.path.exists(py):
        return False
    print("  downloading model (~2.5 GB, one-time):", model)
    r = subprocess.run(
        [py, "-c", "import sys; from huggingface_hub import snapshot_download; "
         "snapshot_download(sys.argv[1])", model])
    return r.returncode == 0 and _model_cached(model)


def _voxtral_synth(cfg, text):
    """POST text to the local Voxtral server; write the returned WAV to a temp
    file and return its path. While the server is still loading the model it
    answers 503 - we retry for a bit so the FIRST block waits for Voxtral rather
    than dropping to `say`. Returns None only on real failure."""
    import urllib.request
    import urllib.error
    if not os.path.exists(_voxtral_py(cfg)):
        return None   # no venv -> the server can never come up; don't stall
    url = "http://127.0.0.1:%d/speak" % _server_port(cfg)
    body = json.dumps({"text": text, "voice": cfg.get("voice") or "fr_female",
                       "speed": cfg.get("speed") or 1.0}).encode("utf-8")
    deadline = time.time() + 25.0    # model-load window
    wav = None
    while True:
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as r:
                wav = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 503 and time.time() < deadline:
                time.sleep(0.5)        # still loading the model
                continue
            return None
        except Exception:
            if time.time() < deadline:
                time.sleep(0.5)        # not bound yet
                continue
            return None
    if not wav:
        return None
    try:
        _qdir()
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".wav", dir=QUEUE_DIR, prefix="spk-")
        with os.fdopen(fd, "wb") as f:
            f.write(wav)
        return path
    except Exception:
        return None


def _wav_player(path):
    if which("afplay"):
        return ["afplay", path]
    if which("paplay"):
        return ["paplay", path]
    if which("aplay"):
        return ["aplay", "-q", path]
    if which("ffplay"):
        return ["ffplay", "-loglevel", "quiet", "-autoexit", "-nodisp", path]
    return None


def _play_wav_async(path, pidfile):
    argv = _wav_player(path)
    if not argv:
        return None
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=(os.name == "posix"))
        _write_pid(p.pid, pidfile)
        return p
    except Exception:
        return None


_FR_SAY_VOICE = None


def _french_say_voice():
    """A French macOS `say` voice if one is installed, so the rare fallback at
    least speaks French. Cached per process."""
    global _FR_SAY_VOICE
    if _FR_SAY_VOICE is not None:
        return _FR_SAY_VOICE
    _FR_SAY_VOICE = ""
    if which("say"):
        try:
            out = subprocess.run(["say", "-v", "?"], capture_output=True,
                                 text=True, timeout=5).stdout
            names = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
            for pref in ("Thomas", "Audrey", "Aurelie", "Amelie", "Jacques"):
                if pref in names:
                    _FR_SAY_VOICE = pref
                    break
            else:
                for ln in out.splitlines():
                    if "fr_FR" in ln or "fr_CA" in ln:
                        _FR_SAY_VOICE = ln.split()[0]
                        break
        except Exception:
            pass
    return _FR_SAY_VOICE


def _say_fallback(cfg, text, pidfile):
    """Speak via the OS `say` engine when the Voxtral server is unavailable -
    using a French voice if available (the default `say` voice mangles French)."""
    fb = dict(cfg)
    fb["engine"] = "say" if which("say") else "auto"
    fb["voice"] = cfg.get("say_voice") or _french_say_voice()
    fb["rate"] = None
    speak(fb, text, pidfile=pidfile, barge=False)


def _speak_voxtral(cfg, text, pidfile, barge):
    _start_server(cfg)
    wav = _voxtral_synth(cfg, text)
    if not wav:
        _say_fallback(cfg, text, pidfile)
        return
    if (cfg.get("barge_in", True) if barge is None else barge):
        _kill_prev(pidfile)
    p = _play_wav_async(wav, pidfile)
    if p is not None:
        p.wait()
    try:
        os.remove(wav)
    except Exception:
        pass


def _drain_voxtral(session, cfg):
    """Pipelined drain: synthesize the NEXT block while the current one plays, so
    there is no synthesis gap between blocks (only the first block waits)."""
    _start_server(cfg)
    _cleanup_stale_wavs()
    pending = None   # (text, wav_or_None) already synthesized, ready to play
    while True:
        if pending is None:
            text = _q_pop(session)
            if not text:
                time.sleep(0.05)
                text = _q_pop(session)
                if not text:
                    break
            pending = (text, _voxtral_synth(cfg, text))
        text, wav = pending
        pending = None
        player = _play_wav_async(wav, _qpid(session)) if wav else None
        if wav is None:
            _say_fallback(cfg, text, _qpid(session))   # server not ready yet
        # synthesize the next block while the current one plays
        nxt = _q_pop(session)
        if nxt:
            pending = (nxt, _voxtral_synth(cfg, nxt))
        if player is not None:
            player.wait()
        if wav:
            try:
                os.remove(wav)   # always reclaim, even if the player failed
            except Exception:
                pass


def _tts_server(port):
    """Persistent TTS server. Bind the port FIRST (a duplicate launch then fails
    fast, before wasting a 15s / 2.7GB model load). /health is served on HTTP
    threads (503 while loading, 200 once ready). ALL MLX work runs in ONE worker
    thread that owns the model - MLX GPU streams are per-thread, so loading and
    generate() must share a thread. Run under the venv python (lazy imports)."""
    import io
    import wave
    import threading
    import numpy as np
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mlx_audio.tts.utils import load_model

    import queue as _queue
    cfg = load_config()
    state = {"last": time.time(), "ready": False}
    jobs = _queue.Queue()

    def _to_wav(audio, sr):
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes(pcm)
        return buf.getvalue()

    def _worker():
        # Load and run the model in THIS thread only: MLX GPU streams are
        # per-thread, so generate() must run where the model was created.
        model = load_model(cfg.get("voxtral_model") or DEFAULT_VOXTRAL_MODEL)
        sr0 = int(getattr(model, "sample_rate", 24000) or 24000)
        state["ready"] = True
        while True:
            text, voice, out = jobs.get()
            try:
                chunks, sr = [], sr0
                mt = int(min(900, max(200, len(text) * 4)))
                for res in model.generate(text=text, voice=voice, max_tokens=mt):
                    a = np.asarray(res.audio, dtype=np.float32).reshape(-1)
                    if a.size:
                        chunks.append(a)
                    sr = int(getattr(res, "sample_rate", sr) or sr)
                audio = (np.concatenate(chunks) if chunks
                         else np.zeros(0, dtype=np.float32))
                out["wav"] = _to_wav(audio, sr)
            except Exception as e:
                out["err"] = str(e)
            finally:
                out["event"].set()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/health"):
                self.send_response(200 if state["ready"] else 503)
                self.end_headers()
                self.wfile.write(b"ok" if state["ready"] else b"loading")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            state["last"] = time.time()
            if not self.path.startswith("/speak"):
                self.send_response(404)
                self.end_headers()
                return
            if not state["ready"]:
                self.send_response(503)
                self.end_headers()
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                req = {}
            text = (req.get("text") or "").strip()
            voice = req.get("voice") or cfg.get("voice") or "fr_female"
            out = {"event": threading.Event()}
            jobs.put((text, voice, out))
            if not out["event"].wait(timeout=120) or "wav" not in out:
                sys.stderr.write("[claude-tts] synth error: %s\n"
                                 % out.get("err", "timeout"))
                self.send_response(500)
                self.end_headers()
                return
            wav = out["wav"]
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self.wfile.write(wav)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    except OSError as e:
        sys.stderr.write("[claude-tts] server bind failed: %s\n" % e)
        return  # someone already owns the port; exit before loading the model
    srv.daemon_threads = True
    try:
        _atomic_write(SERVER_PID, str(os.getpid()))
    except Exception:
        pass

    # Serve immediately (health = 503 while loading) and run all MLX work in the
    # single worker thread (it loads the model and flips ready=True when done).
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=_worker, daemon=True).start()

    # Main thread = idle watchdog; exit (and free the model) after long silence.
    try:
        while time.time() - state["last"] <= SERVER_IDLE:
            time.sleep(30)
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            os.remove(SERVER_PID)
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
    cfg = load_config()
    event = data.get("hook_event_name") or "Stop"
    session = data.get("session_id")
    tpath = data.get("transcript_path")
    _dbg("%.2f %-16s sess=%s" % (time.time(), event, _sid(session)[:8]))

    # Streaming OFF -> original behaviour: one shot, final answer only, at Stop.
    if not cfg.get("stream", True):
        return _hook_single_shot(data, cfg, tpath) if event == "Stop" else 0

    # Streaming ON -> a background daemon tails the transcript and speaks each
    # block the instant it lands. The hooks only manage that daemon's lifecycle.
    if event == "SessionEnd":
        _stop_daemon(session)
        return 0
    if event == "UserPromptSubmit":
        _daemon_hush(session)            # cut the previous turn's speech at once
        _start_daemon(session, tpath)    # ensure the daemon is up for this turn
    elif event == "PreToolUse":
        _start_daemon(session, tpath)    # cheap keep-alive: restart if it died
    if cfg.get("engine") == "voxtral":
        _start_server(cfg)               # pre-warm so blocks never wait cold
    return 0


def _hook_single_shot(data, cfg, tpath):
    """Original behaviour: speak only the final answer, once, at end of turn."""
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
    rec = {"ts": time.time(), "session": data.get("session_id"),
           "cwd": data.get("cwd"), "chars": len(clean), "text": clean, "raw": raw}
    write_outputs(rec, h)
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


def cmd_drain(args):
    _drain(args.session)
    return 0


def cmd_daemon(args):
    _daemon(args.session, args.transcript, getattr(args, "from_end", False))
    return 0


def cmd_tts_server(args):
    _tts_server(args.port)
    return 0


def cmd_setup_voxtral(args):
    """Friendly alias: enable Voxtral end to end (venv + deps + model + hooks)."""
    for k, v in (("mode", None), ("rate", None), ("piper_model", None),
                 ("stream", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    if not hasattr(args, "voice"):
        args.voice = None
    args.engine = "voxtral"
    return cmd_install(args)


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
    if getattr(args, "stream", None) is not None:
        cfg["stream"] = args.stream
    if cfg.get("engine") == "voxtral":
        ok, msg = _bootstrap_voxtral()   # create venv + install mlx-audio if needed
        if not ok:
            print("  voxtral unavailable:", msg, "- falling back to `say`.")
            cfg["engine"] = "auto"
        elif not cfg.get("voice"):
            cfg["voice"] = "fr_female"
    save_config(cfg)
    if cfg.get("engine") == "voxtral":
        _download_voxtral_model(cfg)     # pre-cache the weights (idempotent)
        _start_server(cfg)               # warm the model now

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
    events = STREAM_EVENTS if cfg.get("stream", True) else SINGLE_EVENTS
    for ev in ALL_EVENTS:
        kept = [e for e in hooks.get(ev, []) if not _entry_is_ours(e)]
        if ev in events:
            entry = {"hooks": [{"type": "command", "command": hook_cmd}]}
            if ev in ("PreToolUse", "PostToolUse"):
                entry["matcher"] = "*"
            kept.append(entry)
        if kept:
            hooks[ev] = kept
        elif ev in hooks:
            del hooks[ev]
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print("claude-code-tts installed.")
    print("  hook    :", hook_cmd)
    print("  config  :", CONFIG_PATH)
    print("  mode    :", cfg["mode"])
    print("  stream  :", cfg.get("stream", True),
          "(speak each block live)" if cfg.get("stream", True) else "(one shot at end)")
    print("  events  :", ", ".join(events))
    print("  engine  :", detect_engine(cfg) or "NONE - install say/espeak-ng/spd-say")
    if cfg.get("engine") == "voxtral":
        print("  voxtral :", cfg.get("voxtral_model"),
              "(server " + ("up" if _server_running(cfg) else "starting…") + ")")
    if cfg["mode"] == "local":
        print("\nLocal mode: responses will be spoken on THIS machine.")
    else:
        print("\nSpool mode: this machine only records responses. On your audio "
              "machine run:\n  claude-tts listen --ssh <this-host>")
    print("\n>>> Restart Claude Code (or run /hooks) to activate the hooks. <<<")
    return 0


def cmd_uninstall(args):
    settings = _read_settings()
    hooks = settings.get("hooks", {})
    for ev in ALL_EVENTS:
        if ev in hooks:
            hooks[ev] = [e for e in hooks[ev] if not _entry_is_ours(e)]
            if not hooks[ev]:
                del hooks[ev]
    if not hooks:
        settings.pop("hooks", None)
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("hook removed from", SETTINGS)
    if args.purge:
        _stop_server()
        for p in (SPOOL, LAST_TXT, LAST_RAW, LAST_JSON, HASH_FILE, PID_FILE,
                  CONFIG_PATH, INSTALLED_SELF, SERVER_PID,
                  os.path.join(TTS_DIR, "server.log")):
            try:
                os.remove(p)
            except Exception:
                pass
        for d in (QUEUE_DIR, os.path.join(TTS_DIR, "venv")):
            try:
                shutil.rmtree(d)
            except Exception:
                pass
        print("purged", TTS_DIR, "files (config, queue, venv, server state)")
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
    print("  stream    :", cfg.get("stream", True))
    if cfg.get("engine") == "voxtral":
        print("  voxtral   :", cfg.get("voxtral_model"),
              "(server " + ("UP" if _server_running(cfg) else "down") + ")")
    settings = _read_settings()
    allhooks = settings.get("hooks", {})
    wired = [ev for ev in ALL_EVENTS
             if any(_entry_is_ours(e) for e in allhooks.get(ev, []))]
    print("hooks wired :", ", ".join(wired) or "NONE", "(in", SETTINGS + ")")
    cmd_seen = ""
    for ev in wired:
        for e in allhooks.get(ev, []):
            if _entry_is_ours(e):
                cmd_seen = e["hooks"][0]["command"]
                break
        if cmd_seen:
            break
    if cmd_seen:
        print("  command   :", cmd_seen)
    daemons = []
    try:
        for fn in sorted(os.listdir(QUEUE_DIR)):
            if fn.endswith(".daemon.pid"):
                try:
                    if _proc_alive(int(open(os.path.join(QUEUE_DIR, fn),
                                            encoding="utf-8").read().strip())):
                        daemons.append(fn.split(".")[0][:8])
                except Exception:
                    pass
    except Exception:
        pass
    print("daemon(s)   :", ", ".join(daemons) or "none running")
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

    pi = sub.add_parser("install", help="wire the TTS hooks into settings.json")
    pi.add_argument("--mode", choices=["local", "spool"])
    pi.add_argument("--engine")
    pi.add_argument("--voice")
    pi.add_argument("--rate", type=int)
    pi.add_argument("--piper-model", dest="piper_model")
    pi.add_argument("--stream", dest="stream", action="store_true", default=None,
                    help="speak each block live at tool boundaries (default)")
    pi.add_argument("--no-stream", dest="stream", action="store_false",
                    help="speak only the final answer, once at end of turn")
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="remove the hook")
    pu.add_argument("--purge", action="store_true", help="also delete config/spool")
    pu.set_defaults(func=cmd_uninstall)

    ph = sub.add_parser("hook", help="(internal) Claude Code hook (all events)")
    ph.set_defaults(func=cmd_hook)

    pdr = sub.add_parser("drain", help="(internal) speak a session's queued blocks")
    pdr.add_argument("session")
    pdr.set_defaults(func=cmd_drain)

    pdm = sub.add_parser("daemon", help="(internal) tail a transcript and speak live")
    pdm.add_argument("session")
    pdm.add_argument("transcript")
    pdm.add_argument("--from-end", dest="from_end", action="store_true",
                     help="start at end of transcript (skip the current backlog)")
    pdm.set_defaults(func=cmd_daemon)

    psv = sub.add_parser("tts-server",
                         help="(internal) persistent Voxtral TTS server")
    psv.add_argument("--port", type=int, default=DEFAULT_VOXTRAL_PORT)
    psv.set_defaults(func=cmd_tts_server)

    pset = sub.add_parser("setup-voxtral",
                          help="enable Voxtral neural TTS (venv + model + hooks)")
    pset.add_argument("--voice", default=None)
    pset.set_defaults(func=cmd_setup_voxtral)

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
        # the hook (and its detached drainer) must never disturb Claude Code
        if getattr(args, "cmd", None) in ("hook", "drain", "daemon"):
            return 0
        sys.stderr.write("[claude-tts] %s\n" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
