"""
Spotify integration for SystemC.
Search, playlists and playback control for ONE Spotify account (yours).

Enable it in the control panel's Integrations tab (or INTEGRATIONS=spotify in .env), then restart.

One-time setup:
  1. https://developer.spotify.com/dashboard -> create an app. Add this Redirect URI:
         http://127.0.0.1:8888/callback          (Spotify rejects "localhost": use 127.0.0.1)
  2. Control panel -> Integrations -> Spotify -> Settings -> Credentials: paste the Client ID and
     Client Secret (or put SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env yourself).
  3. pip install spotipy            (spotipy 2.26 or newer)
  4. Authorise once, from the project folder:   python integrations/spotify.py
     (opens a browser; the token is cached and refreshed automatically afterwards)

All settings are declared in PANEL below, so the control panel and the HTTP API build, validate and
save them for you:
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI   the credentials
  SPOTIFY_ALLOWED_USER_IDS    only these chat users may use the tools. If empty, EVERYONE who can
                              talk to the bot can control your Spotify.
  SPOTIFY_CACHE_PATH          where the login token is stored (default: <project>/.spotify_token)

Spotify's rules for new apps (Development Mode, since Feb 2026): the app owner needs Spotify Premium,
one Client ID per developer, and at most five authorised users. Playback controls also need Premium
and an active device. Never put the client secret in this file: it lives in .env only.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from pathlib import Path

try:
    from integrations import get_setting          # when the bot loads this file
except ImportError:                               # when run as a script: python integrations/spotify.py
    def get_setting(key, default=None):
        return os.environ.get(key) or default

NAME = "spotify"
DESCRIPTION = "Control Spotify: search, create playlists, add tracks, and manage playback."

# Everything here must be a plain literal: the control panel reads it without importing this file.
# Setting keys must start with SPOTIFY_ (the file name).
PANEL = {
    "title": "Spotify",
    "icon": "🎧",
    "submenu": [
        {"id": "credentials", "title": "Credentials",
         "description": "From your app at developer.spotify.com/dashboard. Saved in .env; restart the "
                        "bot to apply. After changing them, authorise again (see Authentication).",
         "fields": [
             {"key": "SPOTIFY_CLIENT_ID", "label": "Client ID", "type": "text", "required": True,
              "pattern": r"^[A-Za-z0-9]{16,64}$",
              "description": "The 32-character code on your Spotify app's settings page."},
             {"key": "SPOTIFY_CLIENT_SECRET", "label": "Client Secret", "type": "secret", "required": True,
              "pattern": r"^[A-Za-z0-9]{16,64}$",
              "description": "Also on the app's settings page. Stored in .env only."},
             {"key": "SPOTIFY_REDIRECT_URI", "label": "Redirect URI", "type": "text",
              "default": "http://127.0.0.1:8888/callback", "pattern": r"^https?://(?!localhost\b)\S+$",
              "description": "Must match a Redirect URI in your Spotify app exactly. Use 127.0.0.1, "
                             "not localhost: Spotify no longer accepts localhost."},
         ]},
        {"id": "access", "title": "Access",
         "description": "Who may use the Spotify tools, and where the login is kept.",
         "fields": [
             {"key": "SPOTIFY_ALLOWED_USER_IDS", "label": "Allowed users", "type": "list",
              "pattern": r"^[A-Za-z0-9_-]{3,40}$",
              "description": "Chat user IDs that may control Spotify. Leave EMPTY and everyone who can "
                             "talk to the bot can control your account."},
             {"key": "SPOTIFY_CACHE_PATH", "label": "Login token file", "type": "text",
              "description": "Optional. Defaults to .spotify_token in the project folder."},
         ]},
        {"id": "auth", "title": "Authentication",
         "description": "One-time login: run  python integrations/spotify.py  on the machine running the "
                        "bot (it opens a browser). The login then renews itself, no restart needed."},
    ],
}

PROMPT = """You can control the owner's Spotify account. Rules:
- Use spotify_search to find tracks/artists/albums, and spotify_list_playlists to find playlist IDs.
- To play a song, use spotify_play_track with its URI, or with a search query if you only have a name.
- To add tracks to a playlist you need the playlist ID and track URIs (spotify:track:...).
- Playback controls (play, pause, next, previous) need Spotify Premium and an active device (Spotify \
open and playing somewhere). If one fails, say that rather than retrying.
- If a search returns several plausible matches, pick the most relevant, or ask which one if it \
really is ambiguous.
- Never claim something worked unless the tool result says ok."""

# Keep this exact shape: the control panel reads the list below to check a saved login's permissions.
SCOPE = " ".join([
    "user-library-read", "user-library-modify",
    "playlist-read-private", "playlist-modify-public", "playlist-modify-private",
    "user-modify-playback-state", "user-read-playback-state", "user-read-currently-playing",
])

DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
_PROJECT = Path(__file__).resolve().parent.parent


class _Err(Exception):
    """A user-facing problem; becomes {"error": ...} for the model."""


# ── Config / client ──────────────────────────────────────────────────────────

def _cache_path() -> str:
    return str(get_setting("SPOTIFY_CACHE_PATH") or _PROJECT / ".spotify_token")


def _spotipy():
    try:
        import spotipy
    except ImportError:
        raise _Err("the 'spotipy' package isn't installed. Run: pip install spotipy")
    return spotipy


def _auth_manager(open_browser: bool = False):
    _spotipy()
    from spotipy.oauth2 import SpotifyOAuth
    cid, secret = get_setting("SPOTIFY_CLIENT_ID", ""), get_setting("SPOTIFY_CLIENT_SECRET", "")
    if not cid or not secret:
        raise _Err("the Spotify Client ID / Client Secret aren't set (control panel > Integrations > "
                   "Spotify > Credentials)")
    return SpotifyOAuth(
        client_id=cid, client_secret=secret,
        redirect_uri=get_setting("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT),
        scope=SCOPE, cache_path=_cache_path(), open_browser=open_browser,
    )


_NOT_AUTHORISED = ("Spotify isn't authorised yet (or its permissions changed). On the machine running "
                   "the bot, run: python integrations/spotify.py   (one-time browser login)")


def get_sp():
    """Spotify client from the cached token. Never starts an interactive login (that would hang the
    bot waiting on stdin): authorise once with `python integrations/spotify.py`."""
    spotipy = _spotipy()
    auth = _auth_manager(open_browser=False)
    try:
        token = auth.get_cached_token()              # also refreshes an expired token
    except Exception:                                # refresh refused: new Client ID/secret, revoked, ...
        raise _Err(_NOT_AUTHORISED)
    if not token:
        raise _Err(_NOT_AUTHORISED)
    return spotipy.Spotify(auth_manager=auth, requests_timeout=15, retries=2)


# ── Who may use it ───────────────────────────────────────────────────────────

def _allowed_ids() -> set[str]:
    raw = get_setting("SPOTIFY_ALLOWED_USER_IDS", [])
    if isinstance(raw, str):
        raw = raw.replace(";", ",").split(",")
    return {str(x).strip().upper() for x in raw if str(x).strip()}


def available(ctx) -> bool:
    allowed = _allowed_ids()
    if not allowed:
        return True
    return bool(ctx.author_id) and str(ctx.author_id).upper() in allowed


# ── Errors ───────────────────────────────────────────────────────────────────

def _explain(e: Exception) -> str:
    """A short, accurate message for the model (and the owner) from a spotipy / network error."""
    status = getattr(e, "http_status", None)
    reason = str(getattr(e, "reason", "") or "").upper()
    msg = str(getattr(e, "msg", "") or e).strip()
    if type(e).__name__ == "SpotifyOauthError":
        return _NOT_AUTHORISED
    if reason == "NO_ACTIVE_DEVICE" or (status == 404 and "device" in msg.lower()):
        return ("No active Spotify device. Open Spotify on a phone, computer or speaker (play something, "
                "then pause it) so it shows up, and try again.")
    if reason == "PREMIUM_REQUIRED":
        return "Playback controls need Spotify Premium."
    if status == 401:
        return _NOT_AUTHORISED
    if status == 403:
        return (f"Spotify refused this (403): {msg}. If this is a new app in Development Mode, the owner's "
                "account needs Premium and every user must be added under User Management in the dashboard.")
    if status == 404:
        return f"Spotify couldn't find that (404): {msg}"
    if status == 429:
        return "Spotify is rate-limiting this app. Wait a moment and try again."
    if status:
        return f"Spotify error {status}: {msg}"
    return f"{type(e).__name__}: {msg}"


# ── Tool registry ────────────────────────────────────────────────────────────

TOOLS: list[dict] = []
_ACTIONS: dict = {}


def _tool(name, description, props=None, required=()):
    def deco(fn):
        full = f"spotify_{name}"
        TOOLS.append({
            "name": full, "description": description,
            "input_schema": {"type": "object", "properties": props or {}, "required": list(required)},
        })
        _ACTIONS[full] = fn
        return fn
    return deco


def _s(desc): return {"type": "string", "description": desc}
def _b(desc): return {"type": "boolean", "description": desc}


_TRACK_RE = re.compile(r"^(?:spotify:track:|https?://open\.spotify\.com/(?:intl-[a-z]+/)?track/)"
                       r"([A-Za-z0-9]{22})(?:[?/#].*)?$")
_ADDABLE_RE = re.compile(r"^spotify:(?:track|episode):[A-Za-z0-9]{22}$")


def _track_uri(value) -> str | None:
    """spotify:track:ID, an open.spotify.com track link, or a bare 22-character ID -> a track URI."""
    v = str(value or "").strip()
    m = _TRACK_RE.match(v)
    if m:
        return "spotify:track:" + m.group(1)
    return "spotify:track:" + v if re.fullmatch(r"[A-Za-z0-9]{22}", v) else None


def _start(sp, **kw):
    """start_playback, falling back to an available device when none is active right now."""
    device_id = kw.pop("device_id", None)
    try:
        return sp.start_playback(device_id=device_id, **kw)
    except Exception as e:
        if device_id or str(getattr(e, "reason", "")).upper() != "NO_ACTIVE_DEVICE":
            raise
        devices = (sp.devices() or {}).get("devices", [])
        usable = [d for d in devices if d.get("id") and not d.get("is_restricted")]
        if not usable:
            raise
        return sp.start_playback(device_id=usable[0]["id"], **kw)


# ── Tools (plain functions; handle() runs them in a worker thread because spotipy blocks) ──

@_tool("search", "Search Spotify for tracks, artists, albums or playlists. Returns names with URIs.",
       {"q": _s("Search text, e.g. 'Daft Punk Around the World'"),
        "type": _s("track | artist | album | playlist (default track)")}, ["q"])
def _search(q, type="track"):
    kind = str(type or "track").lower()
    if kind not in ("track", "artist", "album", "playlist"):
        raise _Err("type must be track, artist, album or playlist")
    res = get_sp().search(q=q, limit=5, type=kind)           # Spotify caps search at 10 results
    items = [i for i in (res.get(kind + "s", {}) or {}).get("items", []) if i]   # response keys are plural
    out = []
    for it in items:
        row = {"name": it.get("name"), "uri": it.get("uri")}
        if it.get("artists"):
            row["artists"] = [a.get("name") for a in it["artists"]]
        if kind == "track":
            row["album"] = (it.get("album") or {}).get("name")
        if kind == "playlist":
            row["owner"] = (it.get("owner") or {}).get("display_name")
        out.append(row)
    return {"results": out} if out else {"results": [], "note": "No results found."}


@_tool("list_playlists", "List the user's playlists (name, ID, track count) so you can add to an existing one.")
def _list_playlists():
    page = get_sp().current_user_playlists(limit=50)         # 50 is the API maximum
    rows = []
    for p in page.get("items", []):
        if not p:
            continue
        counts = p.get("items") or p.get("tracks") or {}      # Spotify renamed tracks -> items (Feb 2026)
        rows.append({"name": p.get("name"), "id": p.get("id"), "tracks": counts.get("total"),
                     "public": p.get("public")})
    out = {"playlists": rows, "count": len(rows)}
    if page.get("next"):
        out["note"] = "Only the first 50 playlists are listed."
    return out


@_tool("create_playlist", "Create a new Spotify playlist.",
       {"name": _s("Playlist name"), "public": _b("Public playlist? (default true)"),
        "description": _s("Optional playlist description")}, ["name"])
def _create_playlist(name, public=True, description=""):
    pl = get_sp().current_user_playlist_create(name=name, public=bool(public),
                                               description=str(description or ""))   # POST /me/playlists
    return {"ok": True, "name": name, "id": pl["id"], "uri": pl.get("uri"),
            "url": (pl.get("external_urls") or {}).get("spotify")}


@_tool("add_to_playlist", "Add one or more tracks to a playlist.",
       {"playlist_id": _s("Playlist ID"),
        "track_uris": {"type": "array", "items": {"type": "string"}, "description": "Track URIs (spotify:track:...)"}},
       ["playlist_id", "track_uris"])
def _add_to_playlist(playlist_id, track_uris):
    uris = [str(u).strip() for u in (track_uris or []) if u]
    if not uris:
        raise _Err("track_uris is empty")
    bad = [u for u in uris if not _ADDABLE_RE.match(u)]
    if bad:
        raise _Err(f"these aren't valid track URIs (spotify:track:...): {bad[:3]}")
    sp = get_sp()
    for i in range(0, len(uris), 100):               # API limit: 100 per request
        sp.playlist_add_items(playlist_id=playlist_id, items=uris[i:i + 100])
    return {"ok": True, "added": len(uris), "playlist_id": playlist_id}


@_tool("play_track",
       "Start playing a specific track. Give its URI (best), or a search query if you only know the "
       "name. Needs Premium and an active device.",
       {"uri": _s("Spotify track URI (spotify:track:...) or open.spotify.com track link"),
        "query": _s("Song to search for and play when you don't have a URI, e.g. 'Daft Punk Around the World'"),
        "device_id": _s("Optional device ID; by default the active device (or the first available one)")})
def _play_track(uri=None, query=None, device_id=None):
    sp = get_sp()
    target, info = None, {}
    if uri:
        target = _track_uri(uri)
        if not target:
            raise _Err("Invalid track URI. It must look like spotify:track:... (or be an open.spotify.com track link)")
    elif query:
        hits = [t for t in ((sp.search(q=str(query), limit=5, type="track").get("tracks") or {}).get("items") or []) if t]
        if not hits:
            raise _Err(f"No track found for {query!r}")
        top = hits[0]
        target = top["uri"]
        info = {"name": top.get("name"), "artists": [a.get("name") for a in top.get("artists", [])]}
    else:
        raise _Err("give either a track uri or a search query")
    _start(sp, uris=[target], device_id=device_id or None)
    return {"ok": True, "uri": target, **info}


@_tool("control_playback", "Control playback: play, pause, next or previous. Needs Premium and an active device.",
       {"action": _s("play | pause | next | previous")}, ["action"])
def _control_playback(action):
    sp = get_sp()
    action = str(action).lower()
    if action == "play":
        _start(sp)
    elif action == "pause":
        sp.pause_playback()
    elif action == "next":
        sp.next_track()
    elif action == "previous":
        sp.previous_track()
    else:
        raise _Err("action must be play, pause, next or previous")
    return {"ok": True, "action": action}


@_tool("play_playlist", "Start playing a specific playlist or album using its URI.",
       {"uri": _s("Spotify URI (spotify:playlist:... or spotify:album:...)")}, ["uri"])
def _play_playlist(uri):
    if not re.match(r"^spotify:(?:playlist|album):[A-Za-z0-9]{22}$", str(uri or "")):
        raise _Err("uri must look like spotify:playlist:... or spotify:album:...")
    _start(get_sp(), context_uri=uri)
    return {"ok": True, "uri": uri}


@_tool("current_track", "Get the track that's playing right now.")
def _current_track():
    track = get_sp().current_playback()
    if not track or not track.get("item"):
        return {"note": "Nothing is currently playing."}
    item = track["item"]
    return {
        "name": item.get("name"),
        "artist": [a.get("name") for a in item.get("artists", [])] or [(item.get("show") or {}).get("name")],
        "album": (item.get("album") or {}).get("name"),
        "uri": item.get("uri"),
        "is_playing": bool(track.get("is_playing")),
    }


@_tool("reset_auth", "Delete the cached Spotify login so the next authorisation starts fresh.")
def _reset_auth():
    path = _cache_path()
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True, "note": f"Token cache cleared at {path}. Re-authorise by running: python integrations/spotify.py"}


async def handle(ctx, name, args):
    fn = _ACTIONS.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    args = dict(args or {})
    try:
        inspect.signature(fn).bind(**args)           # reject unexpected/missing arguments clearly
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    try:
        return await asyncio.to_thread(fn, **args)   # spotipy blocks, so keep it off the event loop
    except _Err as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": _explain(e)}


# ── One-time login: python integrations/spotify.py ───────────────────────────

def _load_dotenv():
    """Fill os.environ from <project>/.env (the bot does this itself; a bare script does not)."""
    p = _PROJECT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    _load_dotenv()
    try:
        auth = _auth_manager(open_browser=True)
        print("Authenticating... Follow the browser prompts (the redirect page may say it can't connect: "
              "that's fine, just let it finish).")
        auth.get_access_token(as_dict=False)
        print(f"Authentication successful! Token cached at {_cache_path()}")
    except _Err as e:
        print(f"Authentication failed: {e}")
    except Exception as e:
        print(f"Authentication failed: {_explain(e)}")
