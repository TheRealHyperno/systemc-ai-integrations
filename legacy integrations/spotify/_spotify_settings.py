"""
spotify's settings: credentials, and the one-time login.

Plain functions over .env and the token cache, with no spotipy import, so the
control panel can use them even before spotipy is installed. The file starts
with "_" so it is never loaded as an integration itself.

Keys (all read by spotify.py):
    SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REDIRECT_URI
    SPOTIFY_CACHE_PATH          optional; where the login token is kept

Credential changes take effect when the bot restarts. The login token is read
fresh on every Spotify call, so authorising or clearing it needs no restart.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from integration_settings import SettingsError, load_env, save_env

ID_KEY, SECRET_KEY, REDIRECT_KEY, CACHE_KEY = (
    "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI", "SPOTIFY_CACHE_PATH",
)
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"
DASHBOARD_URL = "https://developer.spotify.com/dashboard"
AUTH_TIMEOUT_S = 300                      # a login nobody finishes is killed after this

APP_DIR = Path(__file__).resolve().parent.parent
SPOTIFY_PY = Path(__file__).resolve().parent / "spotify.py"


# ── credentials ──────────────────────────────────────────────────────────────

def cache_path(env_path: Optional[str] = None) -> Path:
    return Path(load_env(env_path).get(CACHE_KEY) or APP_DIR / ".spotify_token")


def get_credentials(env_path: Optional[str] = None) -> dict:
    """What the panel shows. The secret itself is never returned, only a hint."""
    env = load_env(env_path)
    secret = env.get(SECRET_KEY, "")
    return {
        "client_id": env.get(ID_KEY, ""),
        "redirect_uri": env.get(REDIRECT_KEY, "") or DEFAULT_REDIRECT,
        "has_secret": bool(secret),
        "secret_hint": ("••••" + secret[-4:]) if len(secret) >= 8 else ("••••" if secret else ""),
    }


def _check_id(label: str, v: str) -> str:
    v = v.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", v):
        raise SettingsError(f"{label} should be the 32-character code from your Spotify app's settings page.")
    return v


def _check_redirect(v: str) -> str:
    v = v.strip()
    u = urlparse(v)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SettingsError("The Redirect URI must be a full URL like " + DEFAULT_REDIRECT)
    if u.hostname == "localhost":
        raise SettingsError("Spotify no longer accepts 'localhost' redirect URIs — use 127.0.0.1 "
                            "(e.g. " + DEFAULT_REDIRECT + ") and add the same URI in your Spotify app.")
    return v


def set_credentials(*, client_id: Optional[str] = None, client_secret: Optional[str] = None,
                    redirect_uri: Optional[str] = None, env_path: Optional[str] = None) -> dict:
    """Save any of the three. None or "" means 'leave as it is' (so the secret field can stay blank).
    A new Client ID invalidates the saved login, so the token cache is cleared in that case."""
    env = load_env(env_path)
    changed, token_cleared = [], False
    if client_id and client_id.strip():
        cid = _check_id("The Client ID", client_id)
        if cid != env.get(ID_KEY):
            env[ID_KEY] = cid
            changed.append("client_id")
    if client_secret and client_secret.strip():
        sec = _check_id("The Client Secret", client_secret)
        if sec != env.get(SECRET_KEY):
            env[SECRET_KEY] = sec
            changed.append("client_secret")
    if redirect_uri and redirect_uri.strip():
        uri = _check_redirect(redirect_uri)
        if uri != (env.get(REDIRECT_KEY) or DEFAULT_REDIRECT):
            env[REDIRECT_KEY] = uri
            changed.append("redirect_uri")
    if changed:
        save_env(env, env_path)
    if "client_id" in changed or "client_secret" in changed:
        token_cleared = clear_token(env_path)
    return {"changed": changed, "token_cleared": token_cleared}


# ── login state ──────────────────────────────────────────────────────────────

def spotipy_installed() -> bool:
    return importlib.util.find_spec("spotipy") is not None


def required_scopes() -> list[str]:
    """The permissions spotify.py asks for (read from the file, nothing is imported)."""
    m = re.search(r"SCOPE\s*=\s*\" \"\.join\(\[(.*?)\]\)", SPOTIFY_PY.read_text(encoding="utf-8"), re.S)
    return re.findall(r"\"([a-z\-]+)\"", m.group(1)) if m else []


def auth_status(env_path: Optional[str] = None) -> dict:
    """{state, message, ...}; state is no_credentials | no_token | needs_reauth | ok."""
    c = get_credentials(env_path)
    if not (c["client_id"] and c["has_secret"]):
        return {"state": "no_credentials",
                "message": "Add your Client ID and Client Secret on the Credentials page first."}
    path = cache_path(env_path)
    try:
        tok = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "no_token", "message": "Not authorised yet. Press Authorise to log in with Spotify."}
    except (OSError, ValueError):
        return {"state": "no_token", "message": "The saved login is unreadable. Clear it and authorise again."}
    if not tok.get("refresh_token"):
        return {"state": "needs_reauth", "message": "The saved login can't be renewed. Authorise again."}
    missing = sorted(set(required_scopes()) - set(str(tok.get("scope", "")).split()))
    if missing:
        return {"state": "needs_reauth", "missing_scopes": missing,
                "message": "The saved login is missing permissions (" + ", ".join(missing)
                           + "). Authorise again to grant them."}
    left = int(tok.get("expires_at", 0) - time.time())
    return {"state": "ok", "expires_in": left,
            "message": "Authorised. The login renews itself automatically — no restart needed."}


def clear_token(env_path: Optional[str] = None) -> bool:
    """Delete the saved login. True if there was one."""
    p = cache_path(env_path)
    if p.exists():
        p.unlink()
        return True
    return False


def start_authorization(env_path: Optional[str] = None, python: Optional[str] = None) -> subprocess.Popen:
    """Run the one-time browser login (`python integrations/spotify.py`) in the background.

    The browser opens on THIS machine. The returned process has its output merged
    into .stdout (text mode); it is killed automatically after AUTH_TIMEOUT_S so a
    login nobody finishes can't linger. SettingsError if it can't be started."""
    st = auth_status(env_path)
    if st["state"] == "no_credentials":
        raise SettingsError(st["message"])
    if not spotipy_installed():
        raise SettingsError("The 'spotipy' package isn't installed. Install it on the Dependencies tab "
                            "(or run: pip install spotipy), then try again.")
    env = {**os.environ, **load_env(env_path), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([python or sys.executable, "-u", str(SPOTIFY_PY)], cwd=str(APP_DIR), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    t = threading.Timer(AUTH_TIMEOUT_S, lambda: proc.poll() is None and proc.kill())
    t.daemon = True
    t.start()
    return proc
