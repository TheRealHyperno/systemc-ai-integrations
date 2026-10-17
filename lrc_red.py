"""lrc.red lyrics integration.

Gives the agent two tools:
  * lrc_red_search      - find tracks (title / artist / ISRC) on lrc.red
  * lrc_red_get_lyrics  - fetch the lyrics for a track (plain text or timed LRC)

Uses only the standard library (urllib), run in a worker thread so the bot's
event loop is never blocked.

lrc.red endpoints used (see https://lrc.red/docs):
  GET /match.json?title=..&artist=..&album=..&duration=..   best-match lookup
  GET /search.json?q=..                                    free-text search
  GET /s/<ISRC>.lrc | /s/<ISRC>.ttml                       lyrics files
  GET /s/<ISRC>.lyricsfile.yaml                            Lyricsfile YAML

NOTE: the exact JSON field names / query parameter names of the two .json
endpoints are collected in the constants and helpers below. If lrc.red's docs
differ from what is assumed here, adjust those and nothing else.
"""

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from integrations import get_setting

NAME = "lrc_red"
DESCRIPTION = "Fetches song lyrics (plain or time-synced LRC) from lrc.red."

PANEL = {
    "icon": "🎤",
    "fields": [
        {"key": "LRC_RED_BASE_URL", "label": "API base URL", "type": "text",
         "default": "https://lrc.red", "pattern": r"^https?://[^\s/]+(/[^\s]*)?$",
         "description": "Change only if you host or mirror lrc.red yourself."},
        {"key": "LRC_RED_TIMEOUT", "label": "Request timeout (seconds)", "type": "number",
         "min": 3, "max": 60, "default": 15},
        {"key": "LRC_RED_STRIP_TIMESTAMPS", "label": "Strip timestamps by default", "type": "bool",
         "default": True,
         "description": "Return readable plain lyrics unless the agent explicitly asks for LRC."},
    ],
}

PROMPT = (
    "Use lrc_red_get_lyrics when someone asks for a song's lyrics. Pass the title and artist "
    "(and duration if known). Only present lyrics returned by the tool; never write lyrics "
    "from memory. If the tool finds nothing, say so. Use format 'ttml' or 'lyricsfile' only when the user asks for those files."
)

TOOLS = [
    {
        "name": "lrc_red_search",
        "description": (
            "Search lrc.red for tracks matching a free-text query (title, artist, or both). "
            "Returns up to 10 candidates with their ISRC codes, which can be passed to "
            "lrc_red_get_lyrics. Use this to disambiguate when a title is ambiguous."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Search text, e.g. 'Bohemian Rhapsody Queen'."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lrc_red_get_lyrics",
        "description": (
            "Fetch lyrics for a song from lrc.red. Provide either an 'isrc' (exact) or a "
            "'title' (with 'artist' strongly recommended). Returns the matched track info "
            "and the lyrics text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Song title."},
                "artist": {"type": "string", "description": "Artist name (improves matching)."},
                "album": {"type": "string", "description": "Album name (optional)."},
                "duration_seconds": {"type": "integer",
                                     "description": "Track length in seconds (optional, improves matching)."},
                "isrc": {"type": "string",
                         "description": "12-character ISRC code, e.g. from lrc_red_search. "
                                        "If given, title/artist are ignored."},
                "format": {"type": "string", "enum": ["plain", "lrc", "ttml", "lyricsfile"],
                           "description": "'plain' = lyrics only (default). "
                                          "'lrc' = keep [mm:ss.xx] timestamps. "
                                          "'ttml' = the raw TTML file (word timing, singers, translation). "
                                          "'lyricsfile' = the raw Lyricsfile YAML (.lyricsfile.yaml)."},
            },
        },
    },
]

_ISRC_RE = re.compile(r"^[A-Za-z0-9]{12}$")
_LRC_TS = re.compile(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")
_LRC_WORD_TS = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")
_LRC_META = re.compile(r"^\s*\[[A-Za-z#]+:[^\]]*\]\s*$")
_AGENT_TAG = re.compile(r"\{agent:[^}]*\}")
_MAX_BYTES = 2_000_000
# lrc.red is a single-page app, so an unknown path may return its HTML shell with HTTP 200;
# responses are checked with _looks_like() before being trusted.
_LYRICSFILE_EXT = "lyricsfile.yaml"


# ───────────────────────── HTTP (blocking; always run in a thread) ─────────────────────────

def _base() -> str:
    return str(get_setting("LRC_RED_BASE_URL") or "https://lrc.red").rstrip("/")


def _http_get(url: str):
    """Return response text, or None for 404. Raises RuntimeError on other failures."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "discord-agent-lrc_red-integration/1.0",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=float(get_setting("LRC_RED_TIMEOUT"))) as resp:
            raw = resp.read(_MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"lrc.red returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"could not reach lrc.red: {e.reason}") from None
    except TimeoutError:
        raise RuntimeError("lrc.red timed out") from None
    if len(raw) > _MAX_BYTES:
        raise RuntimeError("lrc.red response was unexpectedly large")
    return raw.decode("utf-8", errors="replace")


def _get_json(path: str, params: dict):
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    text = _http_get(f"{_base()}{path}?{qs}")
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        raise RuntimeError("lrc.red returned a non-JSON response") from None


# ───────────────────────── response normalisation ─────────────────────────

def _items(data) -> list:
    """Accept a bare list, or a dict wrapping the list, or a single track dict."""
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("results", "items", "tracks", "songs", "matches", "hits", "data"):
            val = data.get(key)
            if isinstance(val, list):
                return [d for d in val if isinstance(d, dict)]
            if isinstance(val, dict):
                return _items(val)
        if any(k in data for k in ("isrc", "ISRC")):
            return [data]
    return []


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _track(d: dict) -> dict:
    artist = _first(d, "artist", "artistName", "artists")
    if isinstance(artist, list):
        artist = ", ".join(str(a.get("name", a)) if isinstance(a, dict) else str(a) for a in artist)
    out = {
        "isrc": _first(d, "isrc", "ISRC"),
        "title": _first(d, "title", "name", "trackName"),
        "artist": artist,
        "album": _first(d, "album", "albumName"),
        "duration": _first(d, "duration", "durationSeconds", "length"),
    }
    return {k: v for k, v in out.items() if v is not None}


# ───────────────────────── lyric text handling ─────────────────────────

def _strip_lrc(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if _LRC_META.match(line):
            continue
        line = _AGENT_TAG.sub("", _LRC_WORD_TS.sub("", _LRC_TS.sub("", line))).strip()
        lines.append(line)
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _ttml_text(ttml: str) -> str:
    """Plain lyrics from a TTML document (skips translation / romanisation spans)."""
    try:
        root = ET.fromstring(ttml)
    except ET.ParseError:
        return ""

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def collect(el) -> str:
        role = next((v for k, v in el.attrib.items() if local(k) == "role"), "")
        if role in ("x-translation", "x-roman", "x-bg"):
            return ""
        parts = [el.text or ""]
        for child in el:
            parts.append(collect(child))
            parts.append(child.tail or "")
        return "".join(parts)

    lines = []
    for p in root.iter():
        if local(p.tag) == "p":
            line = re.sub(r"\s+", " ", collect(p)).strip()
            if line:
                lines.append(line)
    return "\n".join(lines)


def _looks_like(kind: str, text: str) -> bool:
    head = text.lstrip()[:400].lower()
    if head.startswith(("<!doctype html", "<html")):
        return False
    if kind == "ttml":
        return "<tt" in text[:2000]
    if kind == "lyricsfile":
        return "version:" in text[:2000] or "\nlines:" in text or head.startswith("lines:")
    return True


def _fetch_lyrics(isrc: str, fmt: str):
    """Blocking. Returns (text, kind) or (None, None) if lrc.red has no such file."""
    if fmt == "lyricsfile":
        text = _http_get(f"{_base()}/s/{isrc}.{_LYRICSFILE_EXT}")
        if text and _looks_like("lyricsfile", text):
            return text.strip(), "lyricsfile"
        return None, None
    if fmt == "ttml":
        text = _http_get(f"{_base()}/s/{isrc}.ttml")
        if text and _looks_like("ttml", text):
            return text.strip(), "ttml"
        return None, None
    lrc = _http_get(f"{_base()}/s/{isrc}.lrc")
    if lrc and lrc.strip():
        return (lrc.strip(), "lrc") if fmt == "lrc" else (_strip_lrc(lrc), "plain")
    ttml = _http_get(f"{_base()}/s/{isrc}.ttml")
    if ttml and _looks_like("ttml", ttml):
        text = _ttml_text(ttml)
        if text:
            return text, "plain"
    return None, None


def _resolve(args: dict):
    """Blocking. Find the best track dict for the arguments, or None."""
    title = (args.get("title") or "").strip()
    artist = (args.get("artist") or "").strip()
    album = (args.get("album") or "").strip()
    duration = args.get("duration_seconds")
    duration = duration if isinstance(duration, int) and duration > 0 else None

    data = _get_json("/match.json", {"title": title, "artist": artist,
                                     "album": album, "duration": duration})
    for item in _items(data):
        t = _track(item)
        if t.get("isrc"):
            return t
    # match.json weighs the artist heavily; fall back to free-text search
    data = _get_json("/search.json", {"q": f"{title} {artist}".strip()})
    for item in _items(data):
        t = _track(item)
        if t.get("isrc"):
            return t
    return None


# ───────────────────────── tool handlers ─────────────────────────

async def handle(ctx, name, args):
    if name == "lrc_red_search":
        return await _search(args)
    if name == "lrc_red_get_lyrics":
        return await _get_lyrics(args)
    return {"error": f"unknown tool: {name}"}


async def _search(args):
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}
    try:
        data = await asyncio.to_thread(_get_json, "/search.json", {"q": query.strip()})
    except RuntimeError as e:
        return {"error": str(e)}
    tracks = [_track(i) for i in _items(data)]
    tracks = [t for t in tracks if t]
    if not tracks:
        return {"error": f"no tracks found on lrc.red for {query!r}"}
    return {"results": tracks[:10], "truncated": len(tracks) > 10, "total": len(tracks)}


async def _get_lyrics(args):
    fmt = args.get("format")
    if fmt not in (None, "plain", "lrc", "ttml", "lyricsfile"):
        return {"error": "format must be 'plain', 'lrc', 'ttml' or 'lyricsfile'"}
    if not fmt:
        fmt = "plain" if get_setting("LRC_RED_STRIP_TIMESTAMPS") else "lrc"

    isrc = args.get("isrc")
    track = {}
    try:
        if isrc:
            isrc = str(isrc).strip().upper().replace("-", "")
            if not _ISRC_RE.match(isrc):
                return {"error": "isrc must be 12 letters/digits (dashes allowed), e.g. GBUM71029604"}
            track = {"isrc": isrc}
        else:
            if not (args.get("title") or "").strip():
                return {"error": "provide either 'isrc' or 'title' (plus 'artist' if known)"}
            track = await asyncio.to_thread(_resolve, args)
            if not track:
                return {"error": "lrc.red has no match for that song; try lrc_red_search "
                                 "with different spelling or the original-language title"}
            isrc = str(track["isrc"]).strip().upper()
            if not _ISRC_RE.match(isrc):
                return {"error": f"lrc.red returned an unusable ISRC: {isrc!r}", "track": track}

        text, kind = await asyncio.to_thread(_fetch_lyrics, isrc, fmt)
    except RuntimeError as e:
        return {"error": str(e)}

    if not text:
        if fmt == "lyricsfile":
            return {"error": "lrc.red has no Lyricsfile for this track; "
                             "try format 'ttml' or 'lrc'", "track": track}
        if fmt == "ttml":
            return {"error": "lrc.red has no TTML file for this track; try format 'lrc'",
                    "track": track}
        return {"error": "lrc.red knows this track but has no lyrics for it "
                         "(it may be an instrumental)", "track": track}

    return {"track": track, "format": kind, "lyrics": text,
            "source": f"{_base()}/s/{isrc}"}
