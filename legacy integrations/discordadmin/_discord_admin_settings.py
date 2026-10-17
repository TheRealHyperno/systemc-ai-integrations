"""
discord_admin's settings: who the admins are and which actions ask for approval.

Plain functions over .env, with no discord.py import, so the control panel and
the HTTP API can use them without loading the Discord library. The file starts
with "_" so it is never loaded as an integration itself.

Keys (all read by discord_admin.py at startup):
    DISCORD_ADMIN_USER_IDS     comma-separated Discord user IDs
    DISCORD_ADMIN_CONFIRM      destructive (default) | all | none
    DISCORD_ADMIN_ALWAYS_ASK   comma-separated action names that always ask
    DISCORD_ADMIN_NEVER_ASK    comma-separated action names that never ask
"""

from __future__ import annotations

import re
from typing import Optional

from integration_settings import SettingsError, env_list, load_env, save_env

ADMIN_KEY, MODE_KEY, ASK_KEY, NEVER_KEY = (
    "DISCORD_ADMIN_USER_IDS", "DISCORD_ADMIN_CONFIRM",
    "DISCORD_ADMIN_ALWAYS_ASK", "DISCORD_ADMIN_NEVER_ASK",
)
MODES = ("destructive", "all", "none")
SETTINGS = ("default", "ask", "never")        # per-action: ask = always ask, never = never ask
BAD_ID = "That doesn't look like a Discord user ID (15-25 digits)."


class NeedsConfirmation(SettingsError):
    """'never ask' was requested for actions that are destructive by default.
    Retry with confirm_destructive=True once the user has agreed. .actions lists them."""

    def __init__(self, actions: list[str]):
        self.actions = actions
        super().__init__("These actions are destructive and normally ask first: "
                         + ", ".join(actions) + ". Repeat the request with confirm_destructive "
                         "set to true to turn their approval off.")


# ── admins ───────────────────────────────────────────────────────────────────

def parse_user_id(text) -> str:
    """A bare Discord ID or a <@mention> -> the ID. SettingsError if it isn't one."""
    m = re.search(r"\d{15,25}", str(text if text is not None else ""))
    if not m:
        raise SettingsError(BAD_ID)
    return m.group(0)


def get_admin_ids(env_path: Optional[str] = None) -> list[str]:
    return env_list(load_env(env_path), ADMIN_KEY)


def set_admin_ids(ids, env_path: Optional[str] = None) -> list[str]:
    """Replace the whole list (validated, de-duplicated, order kept)."""
    clean = list(dict.fromkeys(parse_user_id(i) for i in ids))
    env = load_env(env_path)
    env[ADMIN_KEY] = ",".join(clean)
    save_env(env, env_path)
    return clean


def add_admin(text, env_path: Optional[str] = None) -> list[str]:
    uid = parse_user_id(text)
    ids = get_admin_ids(env_path)
    return ids if uid in ids else set_admin_ids(ids + [uid], env_path)


def remove_admins(ids, env_path: Optional[str] = None) -> list[str]:
    drop = {str(i).strip() for i in ids}
    return set_admin_ids([i for i in get_admin_ids(env_path) if i not in drop], env_path)


# ── approvals ────────────────────────────────────────────────────────────────

def get_mode(env_path: Optional[str] = None) -> str:
    m = load_env(env_path).get(MODE_KEY, "destructive").strip().lower()
    return m if m in MODES else "destructive"


def set_mode(mode: str, env_path: Optional[str] = None) -> str:
    mode = str(mode).strip().lower()
    if mode not in MODES:
        raise SettingsError(f"mode must be one of: {', '.join(MODES)}")
    env = load_env(env_path)
    env[MODE_KEY] = mode
    save_env(env, env_path)
    return mode


def _names(env: dict, key: str) -> set[str]:
    return {x.lower().removeprefix("discord_") for x in env_list(env, key)}


def action_settings(env_path: Optional[str] = None) -> dict[str, str]:
    """{action: 'ask' | 'never'} for every action that has its own setting."""
    env = load_env(env_path)
    ask, never = _names(env, ASK_KEY), _names(env, NEVER_KEY)
    return {**{n: "never" for n in never}, **{n: "ask" for n in ask}}     # ask wins a tie


def default_asks(mode: str, has_confirm: bool) -> bool:
    """Would an action with no setting of its own ask under this global mode?"""
    return mode == "all" or (mode == "destructive" and has_confirm)


def set_action_settings(changes: dict, actions: list[dict], confirm_destructive: bool = False,
                        env_path: Optional[str] = None) -> dict[str, str]:
    """Apply {action: 'ask'|'never'|'default'}. `actions` is discord_admin.panel_actions().
    Setting 'never' on an action that is destructive by default raises NeedsConfirmation
    unless confirm_destructive is True. Returns action_settings() afterwards."""
    by_name = {a["name"]: a for a in actions}
    clean = {}
    for raw, setting in changes.items():
        name = str(raw).strip().lower().removeprefix("discord_")
        if name not in by_name:
            raise SettingsError(f"unknown action: {raw}")
        if setting not in SETTINGS:
            raise SettingsError(f"setting for {name} must be one of: {', '.join(SETTINGS)}")
        clean[name] = setting
    risky = sorted(n for n, s in clean.items() if s == "never" and by_name[n]["has_confirm"])
    if risky and not confirm_destructive:
        raise NeedsConfirmation(risky)
    env = load_env(env_path)
    ask, never = _names(env, ASK_KEY), _names(env, NEVER_KEY)
    for n, s in clean.items():
        ask.discard(n)
        never.discard(n)
        if s == "ask":
            ask.add(n)
        elif s == "never":
            never.add(n)
    env[ASK_KEY] = ",".join(sorted(ask))
    env[NEVER_KEY] = ",".join(sorted(never))
    save_env(env, env_path)
    return action_settings(env_path)
