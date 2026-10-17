"""Audio manipulation integration (ffmpeg based).

Drop this file into `integrations/`, add `audio_tools` to INTEGRATIONS in .env, restart the bot.
Needs ffmpeg + ffprobe installed (on PATH, or set the path in the integration's settings).

Safety model:
  * ffmpeg is always run with an argument list (never a shell), and the model never supplies raw
    ffmpeg arguments: it picks from a fixed set of operations that are turned into a filter chain here.
  * Files are only WRITTEN inside the workspace folder. They can be READ from the workspace and from
    optional extra read-only folders. Paths are resolved (symlinks, "..") before the check.
  * Output goes to a temporary file first, so a failed or timed-out run never leaves a half-written
    file or destroys the file being overwritten.
"""
import asyncio
import json
import math
import os
import mimetypes
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from integrations import get_setting

NAME = "audio_tools"
DESCRIPTION = "Convert, edit, combine and analyse audio files with ffmpeg."

PANEL = {
    "icon": "🎚️",
    "fields": [
        {"key": "AUDIO_TOOLS_DIR", "label": "Workspace folder", "type": "text",
         "default": "audio_workspace",
         "description": "All output files are written here, and inputs are read from here. "
                        "Relative paths are relative to the bot folder."},
        {"key": "AUDIO_TOOLS_INPUT_DIRS", "label": "Extra read-only input folders", "type": "list",
         "description": "Folders the tools may read from (never write to), e.g. a music library. "
                        "Separate with commas; folder names can't contain commas."},
        {"key": "AUDIO_TOOLS_ALLOWED_USER_IDS", "label": "Only these users", "type": "list",
         "pattern": r"^\d{15,25}$",
         "description": "Discord/Stoat user IDs allowed to use the audio tools. "
                        "Leave empty to allow everyone who can talk to the bot."},
        {"key": "AUDIO_TOOLS_FFMPEG", "label": "ffmpeg command or path", "type": "text",
         "default": "ffmpeg",
         "description": "ffprobe is looked up next to it. Use a full path if ffmpeg isn't on PATH."},
        {"key": "AUDIO_TOOLS_TIMEOUT", "label": "Timeout per ffmpeg run (seconds)", "type": "number",
         "min": 10, "max": 3600, "default": 300},
        {"key": "AUDIO_TOOLS_MAX_SEND_MB", "label": "Largest file to post in chat (MB)", "type": "number",
         "min": 1, "max": 500, "default": 10,
         "description": "Discord's real limit depends on the server's boost level; Stoat's depends on the instance."},
        {"key": "AUDIO_TOOLS_STOAT_TOKEN", "label": "Stoat bot token (for posting files)", "type": "secret",
         "description": "Used only to upload files on Stoat. Leave blank to use STOAT_TOKEN or STOAT_BOT_TOKEN "
                        "from the environment."},
        {"key": "AUDIO_TOOLS_STOAT_API_URL", "label": "Stoat API URL", "type": "text",
         "default": "https://api.stoat.chat",
         "description": "Change this only if you run your own Stoat instance."},
        {"key": "AUDIO_TOOLS_CONFIRM_OVERWRITE", "label": "Ask approval before overwriting", "type": "bool",
         "default": True},
    ],
}

PROMPT = (
    "Audio tools work on files in the audio workspace (plus any read-only input folders). "
    "Call audio_tools_list to see what is there. Use audio_tools_info or audio_tools_analyze for facts "
    "like duration, loudness or silence; never guess them. To process a chat attachment or a download, "
    "first save it into the workspace with your other tools. Put all the edits for one output file into a "
    "single audio_tools_edit call (operations run in the order given). After a successful edit you can "
    "post the result with audio_tools_send. Trust only the tool results: say a file was created only "
    "when the result says ok is true."
)

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".aiff", ".aif"}
MEDIA_EXTS = AUDIO_EXTS | {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4b", ".3gp"}
OUT_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac"}
ROOT = Path(__file__).resolve().parent.parent
MAX_REVERSE_SECONDS = 900          # reverse / fade-out fallback buffer the whole clip in memory
OP_NAMES = ["trim", "volume", "normalize", "fade_in", "fade_out", "speed", "tempo", "pitch", "reverse",
            "lowpass", "highpass", "bass", "treble", "echo", "compress", "denoise", "remove_silence"]

_SEM = None


class AudioError(Exception):
    """A problem worth telling the user about (bad argument, missing file, ffmpeg failure)."""


# --------------------------------------------------------------------------- tool schemas

_OP_SCHEMA = {
    "type": "object",
    "required": ["op"],
    "properties": {
        "op": {"type": "string", "enum": OP_NAMES},
        "start": {"type": "number", "description": "trim: start time in seconds (default 0)"},
        "end": {"type": "number", "description": "trim: end time in seconds (use end OR duration)"},
        "duration": {"type": "number", "description": "trim: length to keep in seconds"},
        "db": {"type": "number", "description": "volume: gain in dB (-60..30); bass/treble: gain in dB (-20..20)"},
        "target_lufs": {"type": "number", "description": "normalize: loudness target (default -16, range -30..-5)"},
        "seconds": {"type": "number", "description": "fade_in / fade_out: fade length in seconds"},
        "factor": {"type": "number", "description": "speed: 0.25..4, changes pitch too (like a record). "
                                                    "tempo: 0.25..4, keeps pitch."},
        "semitones": {"type": "number", "description": "pitch: -12..12, keeps the speed"},
        "hz": {"type": "number", "description": "lowpass / highpass: cutoff frequency in Hz (20..20000)"},
        "delay_ms": {"type": "number", "description": "echo: delay in ms (1..2000, default 300)"},
        "decay": {"type": "number", "description": "echo: strength of the repeats (0..0.95, default 0.4)"},
        "threshold_db": {"type": "number", "description": "compress: level above which compression starts "
                                                          "(-60..0, default -18). remove_silence: what counts "
                                                          "as silence (-80..-10, default -50)"},
        "ratio": {"type": "number", "description": "compress: ratio (1..20, default 4)"},
        "min_silence_seconds": {"type": "number", "description": "remove_silence: only cut silences at least "
                                                                 "this long (0.1..30, default 0.5)"},
        "strength_db": {"type": "number", "description": "denoise: noise reduction in dB (1..40, default 12)"},
    },
}

TOOLS = [
    {
        "name": "audio_tools_list",
        "description": "List the audio/video files available to the audio tools: the workspace and any "
                       "read-only input folders, newest first (at most 60 per folder).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "audio_tools_info",
        "description": "Read a file's audio details: duration, format, codec, sample rate, channels, "
                       "bitrate, size and title/artist/album tags.",
        "input_schema": {
            "type": "object",
            "required": ["file"],
            "properties": {"file": {"type": "string", "description": "File name or path (workspace, or an "
                                                                     "allowed input folder)."}},
        },
    },
    {
        "name": "audio_tools_analyze",
        "description": "Measure a file: mean/max volume (dB), integrated loudness (LUFS), loudness range, "
                       "true peak, and where the silences are. Takes a while on long files.",
        "input_schema": {
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string"},
                "silence_threshold_db": {"type": "number", "description": "Below this is silence "
                                                                          "(-80..-10, default -50)"},
                "min_silence_seconds": {"type": "number", "description": "Shortest silence to report "
                                                                         "(0.1..30, default 0.5)"},
            },
        },
    },
    {
        "name": "audio_tools_edit",
        "description": (
            "Create a new audio file from an input by applying operations in order, and/or converting "
            "format. Video inputs have their audio extracted. With no operations it is a plain "
            "conversion. Operations: trim, volume, normalize, fade_in, fade_out, speed, tempo, pitch, "
            "reverse, lowpass, highpass, bass, treble, echo, compress, denoise, remove_silence. "
            "The input file is never modified. Example operations: "
            '[{"op":"trim","start":10,"end":40},{"op":"fade_out","seconds":3},{"op":"volume","db":-3}]'
        ),
        "input_schema": {
            "type": "object",
            "required": ["input"],
            "properties": {
                "input": {"type": "string", "description": "File name or path (workspace, or an allowed "
                                                           "input folder)."},
                "output": {"type": "string", "description": "Output name inside the workspace. The "
                                                            "extension picks the format: mp3, wav, flac, "
                                                            "ogg, opus, m4a or aac. Default: "
                                                            "<input>_edit.<same extension>."},
                "operations": {"type": "array", "items": _OP_SCHEMA,
                               "description": "Up to 20 operations, applied in order."},
                "sample_rate": {"type": "integer", "description": "Output sample rate in Hz "
                                                                  "(8000..192000). Default: unchanged."},
                "channels": {"type": "integer", "enum": [1, 2], "description": "1 = mono, 2 = stereo. "
                                                                              "Default: unchanged."},
                "bitrate": {"type": "string", "description": "Lossy formats only, e.g. '192k'. "
                                                             "Default: a good quality preset."},
                "overwrite": {"type": "boolean", "description": "Allow replacing an existing output "
                                                                "(may ask for approval)."},
            },
        },
    },
    {
        "name": "audio_tools_combine",
        "description": "Join several files one after another (mode 'concat') or layer them on top of each "
                       "other (mode 'mix'; each can be delayed and have its own volume). Output is "
                       "44.1 kHz stereo unless sample_rate is given.",
        "input_schema": {
            "type": "object",
            "required": ["mode", "inputs", "output"],
            "properties": {
                "mode": {"type": "string", "enum": ["concat", "mix"]},
                "inputs": {
                    "type": "array",
                    "description": "2 to 20 files, in order.",
                    "items": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string"},
                            "volume_db": {"type": "number", "description": "Gain for this input (-60..30)."},
                            "delay_seconds": {"type": "number", "description": "mix only: start this "
                                                                               "input this many seconds "
                                                                               "late (0..3600)."},
                        },
                    },
                },
                "output": {"type": "string", "description": "Output name inside the workspace "
                                                            "(mp3, wav, flac, ogg, opus, m4a, aac)."},
                "sample_rate": {"type": "integer"},
                "bitrate": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
        },
    },
    {
        "name": "audio_tools_split",
        "description": "Cut a file into equal-length parts (the last may be shorter), saved as "
                       "<prefix>_001.<ext>, <prefix>_002.<ext>, ... in the workspace. At most 60 parts.",
        "input_schema": {
            "type": "object",
            "required": ["input", "segment_seconds"],
            "properties": {
                "input": {"type": "string"},
                "segment_seconds": {"type": "number", "description": "Length of each part (1..3600)."},
                "prefix": {"type": "string", "description": "Output name prefix (default: the input's name "
                                                            "+ '_part')."},
                "format": {"type": "string", "enum": ["mp3", "wav", "flac", "ogg", "opus", "m4a", "aac"],
                           "description": "Default: same as the input if possible, else mp3."},
                "overwrite": {"type": "boolean"},
            },
        },
    },
    {
        "name": "audio_tools_send",
        "description": "Post a workspace file in the current chat as an attachment (Discord and Stoat). "
                       "Files over the configured size limit are refused.",
        "input_schema": {
            "type": "object",
            "required": ["file"],
            "properties": {"file": {"type": "string", "description": "A file in the workspace."},
                           "message": {"type": "string", "description": "Optional text to post with it."}},
        },
    },
]


# --------------------------------------------------------------------------- paths and settings

def _allowed(ctx) -> bool:
    ids = get_setting("AUDIO_TOOLS_ALLOWED_USER_IDS") or []
    return not ids or str(getattr(ctx, "author_id", None)) in {str(i) for i in ids}


def available(ctx) -> bool:
    return _allowed(ctx)


def _workspace() -> Path:
    p = Path(str(get_setting("AUDIO_TOOLS_DIR") or "audio_workspace")).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def _input_dirs() -> list:
    out = []
    for d in get_setting("AUDIO_TOOLS_INPUT_DIRS") or []:
        p = Path(str(d)).expanduser()
        out.append((p if p.is_absolute() else ROOT / p).resolve())
    return out


def _inside(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _display(p: Path) -> str:
    try:
        return p.relative_to(_workspace()).as_posix()
    except ValueError:
        return str(p)


def _resolve_input(name) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise AudioError("a file name is required")
    raw = name.strip().strip('"')
    p = Path(raw).expanduser()
    roots = [_workspace()] + _input_dirs()
    for cand in ([p] if p.is_absolute() else [r / p for r in roots]):
        try:
            rc = cand.resolve()
        except OSError:
            continue
        if rc.is_file() and any(_inside(rc, r) for r in roots):
            return rc
    raise AudioError(f"file not found in the audio workspace or the allowed input folders: {raw}")


def _resolve_output(name) -> Path:
    if not isinstance(name, str) or not name.strip():
        raise AudioError("an output file name is required")
    ws = _workspace()
    p = Path(name.strip().strip('"')).expanduser()
    rc = (p if p.is_absolute() else ws / p).resolve()
    if rc == ws or not _inside(rc, ws):
        raise AudioError("output must be inside the audio workspace folder")
    if rc.suffix.lower() not in OUT_EXTS:
        raise AudioError("output name must end in one of: " + ", ".join(sorted(OUT_EXTS)))
    return rc


def _default_output(inp: Path, tag: str) -> Path:
    ext = inp.suffix.lower() if inp.suffix.lower() in OUT_EXTS else ".mp3"
    return _workspace() / f"{inp.stem}_{tag}{ext}"


async def _check_overwrite(ctx, out: Path, overwrite) -> None:
    if not out.exists():
        return
    if overwrite is not True:
        raise AudioError(f"{_display(out)} already exists; pick another output name or pass overwrite=true")
    if get_setting("AUDIO_TOOLS_CONFIRM_OVERWRITE"):
        if not await ctx.confirm(f"Overwrite {_display(out)}?\nRequested by {ctx.author_name}"):
            raise AudioError("overwrite not approved; nothing was changed")


# --------------------------------------------------------------------------- running ffmpeg

def _bin(tool: str) -> str:
    exe = str(get_setting("AUDIO_TOOLS_FFMPEG") or "ffmpeg").strip()
    if tool == "ffmpeg":
        return exe
    p = Path(exe)
    if p.parent != Path("."):
        return str(p.with_name(p.name.replace("ffmpeg", "ffprobe")))
    return "ffprobe"


def _tail(text: str, n: int = 500) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


def _run(cmd: list, timeout: float):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise AudioError("ffmpeg/ffprobe was not found. Install ffmpeg and put it on PATH, or set its "
                         "path in this integration's settings.") from None
    except subprocess.TimeoutExpired:
        raise AudioError(f"ffmpeg ran longer than {timeout:g}s and was stopped") from None


async def _exec(cmd: list):
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(2)          # at most two ffmpeg processes at once
    timeout = float(get_setting("AUDIO_TOOLS_TIMEOUT") or 300)
    async with _SEM:
        return await asyncio.to_thread(_run, cmd, timeout)


def _head() -> list:
    return [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]


async def _finish(cmd_head: list, out: Path) -> None:
    """Run ffmpeg into a temp file next to `out`, then swap it into place."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.stem}.partial{out.suffix}")
    try:
        r = await _exec(cmd_head + [str(tmp)])
        if r.returncode != 0 or not tmp.exists():
            raise AudioError("ffmpeg failed: " + (_tail(r.stderr) or f"exit code {r.returncode}"))
        os.replace(tmp, out)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _codec_args(ext: str, bitrate) -> list:
    if bitrate is not None:
        if not isinstance(bitrate, str) or not re.fullmatch(r"\d{2,3}k", bitrate.strip().lower()):
            raise AudioError("bitrate must look like '192k'")
        return [] if ext in (".wav", ".flac") else ["-b:a", bitrate.strip().lower()]
    return {".mp3": ["-q:a", "2"], ".ogg": ["-q:a", "5"], ".m4a": ["-b:a", "192k"],
            ".aac": ["-b:a", "192k"], ".opus": ["-b:a", "128k"]}.get(ext, [])


def _flt(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


async def _probe(path: Path) -> dict:
    r = await _exec([_bin("ffprobe"), "-v", "error", "-print_format", "json",
                     "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise AudioError("could not read that file as audio/video: " + _tail(r.stderr))
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        raise AudioError("ffprobe returned unreadable output") from None
    streams = data.get("streams") or []
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if a is None:
        raise AudioError("the file has no audio stream")
    fmt = data.get("format") or {}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    dur = _flt(fmt.get("duration")) or _flt(a.get("duration"))
    br = _flt(a.get("bit_rate")) or _flt(fmt.get("bit_rate"))
    sr = int(_flt(a.get("sample_rate")) or 44100)
    size = _flt(fmt.get("size"))
    return {
        "duration_seconds": round(dur, 2) if dur is not None else None,
        "container": fmt.get("format_name"),
        "codec": a.get("codec_name"),
        "sample_rate": sr,
        "channels": a.get("channels"),
        "bitrate_kbps": round(br / 1000) if br else None,
        "size_mb": round(size / 1048576, 2) if size else None,
        "has_video": any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                         for s in streams),
        "tags": {k: str(tags[k])[:100] for k in ("title", "artist", "album") if k in tags},
    }


# --------------------------------------------------------------------------- operations -> filter chain

def _num(op: dict, key: str, lo: float, hi: float, default=None):
    v = op.get(key, default)
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            pass
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise AudioError(f"'{key}' must be a number")
    if not lo <= v <= hi:
        raise AudioError(f"'{key}' must be between {lo:g} and {hi:g}")
    return float(v)


def _need(op: dict, key: str, lo: float, hi: float, default=None) -> float:
    v = _num(op, key, lo, hi, default)
    if v is None:
        raise AudioError(f"'{key}' is required")
    return v


def _atempo(f: float) -> list:
    parts = []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.6f}")
    return parts


def _apply(kind: str, op: dict, f: list, sr: int, dur, orig):
    """Append filters for one operation; return the (estimated) duration afterwards, or None if unknown."""
    if kind == "trim":
        start = _num(op, "start", 0, 86400, 0)
        end = _num(op, "end", 0, 86400)
        length = _num(op, "duration", 0.01, 86400)
        if end is not None and length is not None:
            raise AudioError("give 'end' or 'duration', not both")
        if length is not None:
            end = start + length
        if end is not None and end <= start:
            raise AudioError("'end' must be after 'start'")
        if dur is not None and start >= dur:
            raise AudioError(f"'start' is past the end of the audio ({dur:.2f}s long at this point)")
        f.append("atrim=start=%.3f" % start + (":end=%.3f" % end if end is not None else ""))
        f.append("asetpts=PTS-STARTPTS")
        if dur is not None:
            dur = (min(end, dur) if end is not None else dur) - start
    elif kind == "volume":
        f.append("volume=%.2fdB" % _need(op, "db", -60, 30))
    elif kind == "normalize":
        t = _need(op, "target_lufs", -30, -5, -16)
        f.append(f"loudnorm=I={t:g}:TP=-1.5:LRA=11")
        f.append(f"aresample={sr}")                 # loudnorm leaves the stream at 192 kHz
    elif kind == "fade_in":
        f.append("afade=t=in:st=0:d=%.3f" % _need(op, "seconds", 0.01, 600))
    elif kind == "fade_out":
        s = _need(op, "seconds", 0.01, 600)
        if dur is not None:
            f.append("afade=t=out:st=%.3f:d=%.3f" % (max(0.0, dur - s), s))
        else:
            if (orig or 0) > MAX_REVERSE_SECONDS:
                raise AudioError("fade_out after remove_silence on a long file isn't supported in one call; "
                                 "run remove_silence first, then fade_out on the result")
            f += ["areverse", "afade=t=in:st=0:d=%.3f" % s, "areverse"]
    elif kind == "speed":
        x = _need(op, "factor", 0.25, 4)
        f += [f"asetrate={int(sr * x)}", f"aresample={sr}"]
        dur = dur / x if dur is not None else None
    elif kind == "tempo":
        x = _need(op, "factor", 0.25, 4)
        f += _atempo(x)
        dur = dur / x if dur is not None else None
    elif kind == "pitch":
        r = 2 ** (_need(op, "semitones", -12, 12) / 12)
        f += [f"asetrate={int(sr * r)}", f"aresample={sr}"] + _atempo(1 / r)
    elif kind == "reverse":
        if (dur if dur is not None else (orig or 0)) > MAX_REVERSE_SECONDS:
            raise AudioError(f"reverse is limited to {MAX_REVERSE_SECONDS // 60} minutes of audio "
                             "(trim first)")
        f.append("areverse")
    elif kind in ("lowpass", "highpass"):
        f.append(f"{kind}=f=%g" % _need(op, "hz", 20, 20000))
    elif kind in ("bass", "treble"):
        f.append(f"{kind}=g=%.1f" % _need(op, "db", -20, 20))
    elif kind == "echo":
        d = _need(op, "delay_ms", 1, 2000, 300)
        k = _need(op, "decay", 0, 0.95, 0.4)
        f.append("aecho=0.8:0.88:%d:%.2f" % (round(d), k))
    elif kind == "compress":
        th = _need(op, "threshold_db", -60, 0, -18)
        ratio = _need(op, "ratio", 1, 20, 4)
        f.append("acompressor=threshold=%.6f:ratio=%g:attack=20:release=250" % (10 ** (th / 20), ratio))
    elif kind == "denoise":
        f.append("afftdn=nr=%g" % _need(op, "strength_db", 1, 40, 12))
    elif kind == "remove_silence":
        th = _need(op, "threshold_db", -80, -10, -50)
        d = _need(op, "min_silence_seconds", 0.1, 30, 0.5)
        f.append("silenceremove=start_periods=1:start_duration=0:start_threshold=%gdB:"
                 "stop_periods=-1:stop_duration=%g:stop_threshold=%gdB" % (th, d, th))
        dur = None
    else:
        raise AudioError("unknown operation; use one of: " + ", ".join(OP_NAMES))
    return dur


def _build_filters(ops, sr: int, duration):
    if ops is None:
        ops = []
    if not isinstance(ops, list):
        raise AudioError("'operations' must be a list")
    if len(ops) > 20:
        raise AudioError("at most 20 operations per call")
    f, dur = [], duration
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise AudioError(f"operation {i} must be an object with an 'op' name")
        kind = op["op"].strip().lower()
        try:
            dur = _apply(kind, op, f, sr, dur, duration)
        except AudioError as e:
            raise AudioError(f"operation {i} ({kind}): {e}") from None
    return f, dur


def _int_arg(args: dict, key: str, lo: int, hi: int):
    v = args.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise AudioError(f"'{key}' must be a whole number")
    try:
        n = int(float(v))
    except ValueError:
        raise AudioError(f"'{key}' must be a whole number") from None
    if not lo <= n <= hi:
        raise AudioError(f"'{key}' must be between {lo} and {hi}")
    return n


# --------------------------------------------------------------------------- tool handlers

def _scan(root: Path, limit: int = 60):
    found, seen, base = [], 0, len(root.parts)
    for dp, dns, fns in os.walk(root):
        if len(Path(dp).parts) - base >= 3:
            dns[:] = []
        for fn in fns:
            seen += 1
            p = Path(dp) / fn
            if p.suffix.lower() in MEDIA_EXTS:
                try:
                    st = p.stat()
                except OSError:
                    continue
                found.append((st.st_mtime, p, st.st_size))
        if seen > 5000:
            break
    found.sort(key=lambda t: t[0], reverse=True)
    return found[:limit], len(found)


async def _list(ctx, args):
    ws = _workspace()
    ws.mkdir(parents=True, exist_ok=True)
    out = {"workspace": str(ws)}
    folders = [("workspace_files", ws)] + [(f"input_folder:{d}", d) for d in _input_dirs()]
    for label, root in folders:
        if not root.is_dir():
            out[label] = {"error": "folder does not exist"}
            continue
        items, total = await asyncio.to_thread(_scan, root)
        entry = {"files": [{"file": _display(p) if root == ws else str(p), "size_mb": round(sz / 1048576, 2)}
                           for _, p, sz in items]}
        if total > len(items):
            entry.update(truncated=True, total=total)
        out[label] = entry
    return out


async def _info(ctx, args):
    p = _resolve_input(args.get("file"))
    return {"file": _display(p), **(await _probe(p))}


async def _analyze(ctx, args):
    p = _resolve_input(args.get("file"))
    th = _need(args, "silence_threshold_db", -80, -10, -50)
    ms = _need(args, "min_silence_seconds", 0.1, 30, 0.5)
    info = await _probe(p)
    flt = f"volumedetect,silencedetect=noise={th:g}dB:d={ms:g},ebur128=peak=true:framelog=quiet"
    r = await _exec([_bin("ffmpeg"), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
                     "-i", str(p), "-vn", "-map", "0:a:0", "-af", flt, "-f", "null", "-"])
    if r.returncode != 0:
        raise AudioError("ffmpeg failed: " + _tail(r.stderr))
    err = r.stderr

    def last(pattern):
        m = re.findall(pattern, err)
        return float(m[-1]) if m else None

    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", err)]
    ends = [(float(e), float(d)) for e, d in re.findall(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)", err)]
    silences = [{"start": round(max(0.0, e - d), 2), "end": round(e, 2)} for e, d in ends]
    if len(starts) > len(ends) and info["duration_seconds"] is not None:
        silences.append({"start": round(starts[-1], 2), "end": info["duration_seconds"]})
    out = {
        "file": _display(p),
        "duration_seconds": info["duration_seconds"],
        "mean_volume_db": last(r"mean_volume:\s*(-?[\d.]+) dB"),
        "max_volume_db": last(r"max_volume:\s*(-?[\d.]+) dB"),
        "integrated_lufs": last(r"I:\s+(-?[\d.]+) LUFS"),
        "loudness_range_lu": last(r"LRA:\s+([\d.]+) LU"),
        "true_peak_dbfs": last(r"Peak:\s+(-?[\d.]+) dBFS"),
        "silence_count": len(silences),
        "silence_total_seconds": round(sum(s["end"] - s["start"] for s in silences), 2),
        "silences": silences[:20],
    }
    if len(silences) > 20:
        out["silences_truncated"] = True
    return out


async def _edit(ctx, args):
    inp = _resolve_input(args.get("input"))
    info = await _probe(inp)
    out = _resolve_output(args["output"]) if args.get("output") else _default_output(inp, "edit")
    if out == inp:
        raise AudioError("output must be a different file from the input")
    sr_out = _int_arg(args, "sample_rate", 8000, 192000)
    channels = _int_arg(args, "channels", 1, 2)
    filters, dur = _build_filters(args.get("operations"), info["sample_rate"], info["duration_seconds"])
    codec = _codec_args(out.suffix.lower(), args.get("bitrate"))
    await _check_overwrite(ctx, out, args.get("overwrite"))

    cmd = _head() + ["-i", str(inp), "-vn", "-map", "0:a:0"]
    if filters:
        cmd += ["-af", ",".join(filters)]
    if sr_out:
        cmd += ["-ar", str(sr_out)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd += codec
    await _finish(cmd, out)
    return {"ok": True, "output": _display(out), **(await _probe(out))}


async def _combine(ctx, args):
    mode = args.get("mode")
    if mode not in ("concat", "mix"):
        raise AudioError("mode must be 'concat' or 'mix'")
    items = args.get("inputs")
    if not isinstance(items, list) or not 2 <= len(items) <= 20:
        raise AudioError("'inputs' must be a list of 2 to 20 files")
    out = _resolve_output(args.get("output"))
    sr = _int_arg(args, "sample_rate", 8000, 192000) or 44100
    codec = _codec_args(out.suffix.lower(), args.get("bitrate"))

    files, chains = [], []
    for i, item in enumerate(items):
        if isinstance(item, str):
            item = {"file": item}
        if not isinstance(item, dict):
            raise AudioError(f"input {i + 1} must be an object with a 'file'")
        path = _resolve_input(item.get("file"))
        if path == out:
            raise AudioError("output must be different from every input")
        await _probe(path)                                   # fail early with a clear message
        files.append(path)
        parts = [f"aresample={sr}", "aformat=sample_fmts=fltp:channel_layouts=stereo"]
        vol = _num(item, "volume_db", -60, 30)
        if vol:
            parts.append("volume=%.2fdB" % vol)
        delay = _num(item, "delay_seconds", 0, 3600)
        if delay:
            if mode != "mix":
                raise AudioError("delay_seconds only applies to mode 'mix'")
            ms = round(delay * 1000)
            parts.append(f"adelay={ms}|{ms}")
        chains.append(f"[{i}:a:0]" + ",".join(parts) + f"[a{i}]")

    labels = "".join(f"[a{i}]" for i in range(len(files)))
    if mode == "concat":
        tail = f"{labels}concat=n={len(files)}:v=0:a=1[out]"
    else:
        tail = (f"{labels}amix=inputs={len(files)}:duration=longest:dropout_transition=0,"
                f"volume={len(files)},alimiter=limit=0.97[out]")
    await _check_overwrite(ctx, out, args.get("overwrite"))

    cmd = _head()
    for p in files:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", ";".join(chains + [tail]), "-map", "[out]", "-ar", str(sr)] + codec
    await _finish(cmd, out)
    return {"ok": True, "output": _display(out), "mode": mode, **(await _probe(out))}


async def _split(ctx, args):
    inp = _resolve_input(args.get("input"))
    seg = _need(args, "segment_seconds", 1, 3600)
    info = await _probe(inp)
    if info["duration_seconds"] and math.ceil(info["duration_seconds"] / seg) > 60:
        raise AudioError("that would make more than 60 parts; use a longer segment_seconds")
    fmt = args.get("format")
    ext = f".{fmt}" if fmt else (inp.suffix.lower() if inp.suffix.lower() in OUT_EXTS else ".mp3")
    base = _resolve_output(f"{args.get('prefix') or inp.stem + '_part'}{ext}")
    stem = re.compile(re.escape(base.stem) + r"_\d{3}" + re.escape(base.suffix) + "$")
    if base.parent.is_dir() and any(stem.fullmatch(x.name) for x in base.parent.iterdir()):
        if args.get("overwrite") is not True:
            raise AudioError("parts with that prefix already exist; choose another prefix or pass overwrite=true")
        if get_setting("AUDIO_TOOLS_CONFIRM_OVERWRITE") and not await ctx.confirm(
                f"Overwrite existing parts named {_display(base.parent / base.stem)}_NNN{base.suffix}?\n"
                f"Requested by {ctx.author_name}"):
            raise AudioError("overwrite not approved; nothing was changed")
    base.parent.mkdir(parents=True, exist_ok=True)
    pattern = base.with_name(f"{base.stem}_%03d{base.suffix}")
    cmd = _head() + ["-i", str(inp), "-vn", "-map", "0:a:0"] + _codec_args(base.suffix, None) + [
        "-f", "segment", "-segment_start_number", "1", "-segment_time", f"{seg:g}", "-reset_timestamps", "1", str(pattern)]
    r = await _exec(cmd)
    if r.returncode != 0:
        raise AudioError("ffmpeg failed: " + _tail(r.stderr))
    parts = sorted(x for x in base.parent.iterdir() if stem.fullmatch(x.name))
    if not parts:
        raise AudioError("ffmpeg produced no files")
    return {"ok": True, "parts": [_display(x) for x in parts], "count": len(parts)}


_UA = "SystemC-audio-tools/1.0"


def _http(method: str, url: str, token, *, json_body=None, data=None, content_type=None, timeout=60):
    """One blocking JSON request (run it in a thread). Stdlib only, so there's nothing extra to install."""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    if token:
        headers["X-Bot-Token"] = token
    body = data
    if json_body is not None:
        body, content_type = json.dumps(json_body).encode("utf-8"), "application/json"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace")
        raise AudioError(f"Stoat answered HTTP {e.code} to {method} {url}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise AudioError(f"could not reach Stoat at {url}: {e}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise AudioError(f"Stoat sent a non-JSON answer to {method} {url}") from None


def _stoat_token() -> str:
    token = get_setting("AUDIO_TOOLS_STOAT_TOKEN") or ""
    for key in ("STOAT_TOKEN", "STOAT_BOT_TOKEN"):
        token = token or os.environ.get(key, "")
    if not token:
        raise AudioError("no Stoat bot token available: fill in 'Stoat bot token' in the audio tools settings "
                         "(or set STOAT_TOKEN in the environment) and restart the bot")
    return str(token)


def _stoat_channel_id(ctx) -> str:
    for obj in (ctx.raw_channel, ctx.channel):
        for attr in ("id", "channel_id"):
            cid = getattr(obj, attr, None)
            if isinstance(cid, str) and re.fullmatch(r"[0-9A-Za-z]{26}", cid):
                return cid
    raise AudioError("couldn't work out this chat's Stoat channel ID")


async def _send_stoat(ctx, path: Path, message) -> None:
    """Upload to Stoat's file server (Autumn), then post a message that references the uploaded file."""
    token = _stoat_token()
    channel_id = _stoat_channel_id(ctx)
    api = str(get_setting("AUDIO_TOOLS_STOAT_API_URL") or "https://api.stoat.chat").rstrip("/")

    def work():
        info = _http("GET", api + "/", None)                       # server config lists the file server URL
        autumn = (((info.get("features") or {}).get("autumn") or {}).get("url")
                  or "https://autumn.stoat.chat").rstrip("/")
        boundary = uuid.uuid4().hex
        fname = path.name.replace('"', "_")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        up = _http("POST", f"{autumn}/attachments", token, data=body,
                   content_type=f"multipart/form-data; boundary={boundary}", timeout=300)
        file_id = up.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise AudioError("Stoat's file server didn't return a file id")
        payload = {"attachments": [file_id]}
        if isinstance(message, str) and message.strip():
            payload["content"] = message.strip()[:2000]
        _http("POST", f"{api}/channels/{channel_id}/messages", token, json_body=payload)

    await asyncio.to_thread(work)


async def _send(ctx, args):
    p = _resolve_input(args.get("file"))
    if not _inside(p, _workspace()):
        raise AudioError("only files in the audio workspace can be posted; copy it there first")
    limit = float(get_setting("AUDIO_TOOLS_MAX_SEND_MB") or 10)
    size = p.stat().st_size / 1048576
    if size > limit:
        raise AudioError(f"{_display(p)} is {size:.1f} MB; the limit for posting is {limit:g} MB "
                         "(try a lower bitrate, mono, or a shorter clip)")
    msg = args.get("message")
    if ctx.platform == "discord":
        import discord
        try:
            await ctx.raw_channel.send(content=msg if isinstance(msg, str) and msg.strip() else None,
                                       file=discord.File(str(p)))
        except Exception as e:                                 # permissions, size limit, closed DM...
            raise AudioError(f"Discord refused the upload: {e}") from None
    elif ctx.platform == "stoat":
        await _send_stoat(ctx, p, msg)
    else:
        raise AudioError(f"posting files isn't supported on {ctx.platform}; the file is at {p}")
    return {"ok": True, "sent": _display(p), "size_mb": round(size, 2)}


_HANDLERS = {
    "audio_tools_list": _list,
    "audio_tools_info": _info,
    "audio_tools_analyze": _analyze,
    "audio_tools_edit": _edit,
    "audio_tools_combine": _combine,
    "audio_tools_split": _split,
    "audio_tools_send": _send,
}


async def handle(ctx, name, args):
    if not _allowed(ctx):                                      # defence in depth; available() hides the tools
        return {"error": "you are not allowed to use the audio tools"}
    fn = _HANDLERS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return await fn(ctx, args if isinstance(args, dict) else {})
    except AudioError as e:
        return {"error": str(e), "performed": False}
