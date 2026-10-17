"""Lyricsfile -> MTTF converter integration.

Drop this file into `integrations/`, press Rescan in the control panel, tick it,
and restart the bot. Needs PyYAML (`pip install pyyaml`); without it the integration
still loads and the tool returns a clear error instead of crashing.

Converts `.lyricsfile.yaml` (version/metadata/lines/words with *_ms timings) into
Modern Timed Text Format (MTTF):

    meta(title="Demo Song", duration_ms=8000, instrumental=0)

    line("Hello world")(1000, 3500) {
      word("Hello") timems(1000, 1400)
      word("world") timems(1500, 3500)
    }

    line("No word timing")(4000, 6500)
"""

import asyncio
import math
from pathlib import Path

from integrations import get_setting

NAME = "lyricsfile_mttf"
DESCRIPTION = "Converts .lyricsfile.yaml lyric files to MTTF (Modern Timed Text Format)."

PANEL = {
    "icon": "🎤",
    "fields": [
        {"key": "LYRICSFILE_MTTF_WORKDIR", "label": "Folder for file input/output", "type": "text",
         "description": "Absolute folder the tool may read .yaml files from and write .mttf files to. "
                        "Leave empty to disable file access (the tool then only accepts pasted YAML)."},
        {"key": "LYRICSFILE_MTTF_MAX_INPUT_KB", "label": "Largest input (KB)", "type": "number",
         "min": 1, "max": 5000, "default": 512},
        {"key": "LYRICSFILE_MTTF_MAX_REPLY_CHARS", "label": "Largest MTTF returned to the model (characters)",
         "type": "number", "min": 500, "max": 50000, "default": 8000,
         "description": "Longer output is truncated in the reply (tool results stay in chat history). "
                        "Use output_path to get the complete file."},
        {"key": "LYRICSFILE_MTTF_ALLOWED_USER_IDS", "label": "Only these users", "type": "list",
         "pattern": r"^\d{15,25}$",
         "description": "Discord/Stoat user IDs. Leave empty to allow everyone."},
    ],
}

PROMPT = (
    "Use lyricsfile_mttf_convert whenever someone wants a .lyricsfile.yaml converted to MTTF. "
    "Never write or guess MTTF by hand: pass the YAML (or a file path) to the tool and relay only its "
    "result. Mention any entries in 'warnings'. If 'truncated' is true, say so and offer to write the "
    "full result to a file with output_path."
)

TOOLS = [{
    "name": "lyricsfile_mttf_convert",
    "description": (
        "Convert a .lyricsfile.yaml document to MTTF (Modern Timed Text Format). Provide exactly one "
        "input: yaml_text (the YAML itself) or path (a file inside the configured folder). Optionally "
        "give output_path to write the .mttf file instead of returning its text. Returns the MTTF, the "
        "number of lines in the source, and warnings about anything skipped."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "yaml_text": {
                "type": "string",
                "description": "Full contents of a .lyricsfile.yaml (keys: metadata, lines[].text/start_ms/"
                               "end_ms, lines[].words[].text/start_ms/end_ms). Times are milliseconds.",
            },
            "path": {
                "type": "string",
                "description": "Path to a .yaml/.yml file, relative to the configured folder (or absolute "
                               "inside it). Use instead of yaml_text.",
            },
            "output_path": {
                "type": "string",
                "description": "Optional. Where to save the result, inside the configured folder. Must end "
                               "in .mttf. When set, the MTTF text is not returned.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Allow replacing an existing output file (the requester is asked to approve). "
                               "Default false.",
            },
        },
    },
}]


# --------------------------------------------------------------------------- gating

def available(ctx) -> bool:
    allowed = get_setting("LYRICSFILE_MTTF_ALLOWED_USER_IDS")
    return not allowed or ctx.author_id in allowed


# --------------------------------------------------------------------------- conversion

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ms(v) -> int:
    """Round half up, like JavaScript's Math.round."""
    return int(math.floor(v + 0.5))


def _fmt_num(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _quote(s) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _valid_key(k) -> bool:
    return (isinstance(k, str) and k != ""
            and (k[0].isalpha() and k[0].isascii() or k[0] == "_")
            and all(c.isascii() and (c.isalnum() or c == "_") for c in k))


def convert(yaml_text: str):
    """Return (mttf_text, warnings, source_line_count). Raises ValueError on bad input."""
    try:
        import yaml
    except ImportError:
        raise ValueError("PyYAML is not installed; install it on the Dependencies tab (pip install pyyaml)")
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}")
    if not isinstance(data, dict):
        raise ValueError("file is empty or not a YAML mapping")

    out, warn = [], []

    # metadata
    raw_meta = data.get("metadata") or {}
    if not isinstance(raw_meta, dict):
        warn.append("metadata is not a mapping; ignored")
        raw_meta = {}
    meta = []
    for k, v in raw_meta.items():
        if not _valid_key(k):
            warn.append(f'skipped metadata key "{k}"')
            continue
        if isinstance(v, str):
            meta.append(f"{k}={_quote(v)}")
        elif isinstance(v, bool):
            meta.append(f"{k}={1 if v else 0}")
        elif _is_num(v):
            meta.append(f"{k}={_fmt_num(v)}")
        elif v is not None:
            warn.append(f'skipped non-scalar metadata "{k}"')
    if meta:
        out += [f"meta({', '.join(meta)})", ""]

    # lines
    lines = data.get("lines") if isinstance(data.get("lines"), list) else []
    if not lines and not raw_meta.get("instrumental"):
        warn.append("no lines found")

    skipped = untimed = 0
    for l in lines:
        if not isinstance(l, dict):
            skipped += 1
            continue
        raw_words = l.get("words") if isinstance(l.get("words"), list) else []
        words = []
        for w in raw_words:
            if (isinstance(w, dict) and isinstance(w.get("text"), str)
                    and w["text"].strip() != "" and _is_num(w.get("start_ms"))):
                words.append({
                    "raw": w["text"],
                    "t": w["text"].strip(),
                    "s": w["start_ms"],
                    "e": w["end_ms"] if _is_num(w.get("end_ms")) else None,
                })

        if isinstance(l.get("text"), str):
            text = l["text"].strip()
        else:
            text = "".join(w["raw"] for w in words).strip()
        start = l["start_ms"] if _is_num(l.get("start_ms")) else (words[0]["s"] if words else None)
        last = words[-1]["e"] if words else None
        end = l["end_ms"] if _is_num(l.get("end_ms")) else last

        if not text or start is None:
            skipped += 1
            continue

        rng = f"({_ms(start)}" + (f", {_ms(end)}" if end is not None else "") + ")"
        if not words:
            out += [f"line({_quote(text)}){rng}", ""]
            untimed += 1
            continue
        out.append(f"line({_quote(text)}){rng} {{")
        for w in words:
            t = f"timems({_ms(w['s'])}" + (f", {_ms(w['e'])}" if w["e"] is not None else "") + ")"
            out.append(f"  word({_quote(w['t'])}) {t}")
        out += ["}", ""]

    if skipped:
        warn.append(f"{skipped} line(s) skipped (no text or timing)")
    if untimed:
        warn.append(f"{untimed} line(s) had no word timing, written as a single line")
    return "\n".join(out).rstrip() + "\n", warn, len(lines)


# --------------------------------------------------------------------------- file helpers

def _workdir():
    raw = (get_setting("LYRICSFILE_MTTF_WORKDIR") or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _inside(base: Path, p: str) -> Path:
    cand = Path(p).expanduser()
    if not cand.is_absolute():
        cand = base / cand
    cand = cand.resolve()
    if cand != base and base not in cand.parents:
        raise ValueError("path is outside the configured folder")
    return cand


def _read_text(path: Path, max_bytes: int) -> str:
    if not path.is_file():
        raise ValueError(f"file not found: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"file is larger than the {max_bytes // 1024} KB limit")
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("file is not valid UTF-8 text")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- tool handler

async def handle(ctx, name, args):
    if name != "lyricsfile_mttf_convert":
        return {"error": f"unknown tool: {name}"}
    # re-check the gate here too (defence in depth)
    if not available(ctx):
        return {"error": "you are not allowed to use this tool"}
    if not isinstance(args, dict):
        args = {}

    yaml_text, path_arg = args.get("yaml_text"), args.get("path")
    out_arg = args.get("output_path")
    if bool(yaml_text) == bool(path_arg):
        return {"error": "provide exactly one of yaml_text or path"}
    if yaml_text is not None and not isinstance(yaml_text, str):
        return {"error": "yaml_text must be a string"}
    if out_arg is not None and not isinstance(out_arg, str):
        return {"error": "output_path must be a string"}

    max_bytes = int(get_setting("LYRICSFILE_MTTF_MAX_INPUT_KB")) * 1024
    base = _workdir()
    needs_files = bool(path_arg or out_arg)
    if needs_files and base is None:
        return {"error": "file access is disabled: no folder is configured for this integration. "
                         "Pass the YAML as yaml_text instead."}

    try:
        if path_arg:
            if not isinstance(path_arg, str):
                return {"error": "path must be a string"}
            src = _inside(base, path_arg)
            yaml_text = await asyncio.to_thread(_read_text, src, max_bytes)
        elif len(yaml_text.encode("utf-8")) > max_bytes:
            return {"error": f"yaml_text is larger than the {max_bytes // 1024} KB limit"}

        mttf, warnings, n_lines = await asyncio.to_thread(convert, yaml_text)
    except ValueError as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": f"could not read the file: {e.strerror or e}"}

    result = {"lines": n_lines, "warnings": warnings, "chars": len(mttf)}

    if out_arg:
        try:
            dest = _inside(base, out_arg)
        except ValueError as e:
            return {"error": str(e)}
        if dest.suffix.lower() != ".mttf":
            return {"error": "output_path must end in .mttf"}
        if dest.exists():
            if not args.get("overwrite"):
                return {"error": f"{dest.name} already exists; set overwrite=true to replace it",
                        "performed": False}
            if not await ctx.confirm(f"Overwrite {dest.name} with converted MTTF?\n"
                                     f"Requested by {ctx.author_name}"):
                return {"error": "approval denied or timed out; file NOT written", "performed": False}
        try:
            await asyncio.to_thread(_write_text, dest, mttf)
        except OSError as e:
            return {"error": f"could not write the file: {e.strerror or e}", "performed": False}
        result.update(written=str(dest.relative_to(base)), performed=True)
        return result

    limit = int(get_setting("LYRICSFILE_MTTF_MAX_REPLY_CHARS"))
    if len(mttf) > limit:
        result.update(mttf=mttf[:limit], truncated=True,
                      note="output truncated; call again with output_path to save the complete file")
    else:
        result["mttf"] = mttf
    return result
