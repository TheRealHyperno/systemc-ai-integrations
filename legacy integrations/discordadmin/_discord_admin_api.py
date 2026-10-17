"""
discord_admin's HTTP routes, mounted at /api/v1/integrations/discord_admin by
integrations_api.py (via discord_admin.api_router()). Reference: API.md.

No `from __future__ import annotations` here on purpose: FastAPI reads the handlers'
annotations, and string annotations would be looked up in this module's globals.
"""

from fastapi import APIRouter, HTTPException, Request

from integration_settings import SettingsError
from integrations import _discord_admin_settings as das
from integrations_api import bad_request, changed, json_body


def build_router(panel_actions) -> APIRouter:
    r = APIRouter(tags=["discord_admin"])

    def _approvals() -> dict:
        mode, overrides = das.get_mode(), das.action_settings()
        return {
            "mode": mode,
            "modes": list(das.MODES),
            "actions": [{
                "name": a["name"], "description": a["description"],
                "default_asks": das.default_asks(mode, a["has_confirm"]),
                "setting": overrides.get(a["name"], "default"),
            } for a in panel_actions()],
        }

    def _apply(changes: dict, confirm: bool) -> dict:
        try:
            das.set_action_settings(changes, panel_actions(), confirm_destructive=confirm)
        except das.NeedsConfirmation as e:
            raise HTTPException(409, str(e))
        except SettingsError as e:
            raise bad_request(e)
        return changed(**_approvals())

    @r.get("/admins")
    async def get_admins():
        return {"admin_ids": das.get_admin_ids(), "owner_always_allowed": True}

    @r.put("/admins")
    async def replace_admins(request: Request):
        ids = (await json_body(request)).get("admin_ids")
        if not isinstance(ids, list):
            raise HTTPException(400, 'body must be {"admin_ids": ["123...", ...]}')
        try:
            return changed(admin_ids=das.set_admin_ids(ids), owner_always_allowed=True)
        except SettingsError as e:
            raise bad_request(e)

    @r.post("/admins")
    async def add_admin(request: Request):
        uid = (await json_body(request)).get("user_id")
        try:
            return changed(admin_ids=das.add_admin(uid), owner_always_allowed=True)
        except SettingsError as e:
            raise bad_request(e)

    @r.delete("/admins/{user_id}")
    async def remove_admin(user_id: str):
        if user_id not in das.get_admin_ids():
            raise HTTPException(404, f"{user_id} is not in the admin list")
        return changed(admin_ids=das.remove_admins([user_id]), owner_always_allowed=True)

    @r.get("/approvals")
    async def get_approvals():
        return _approvals()

    @r.put("/approvals/mode")
    async def put_mode(request: Request):
        try:
            das.set_mode((await json_body(request)).get("mode"))
        except SettingsError as e:
            raise bad_request(e)
        return changed(**_approvals())

    @r.put("/approvals/actions/{action}")
    async def put_action(action: str, request: Request):
        body = await json_body(request)
        name = action.lower().removeprefix("discord_")
        if name not in {a["name"] for a in panel_actions()}:
            raise HTTPException(404, f"unknown action: {action}")
        return _apply({name: body.get("setting")}, body.get("confirm_destructive") is True)

    @r.patch("/approvals/actions")
    async def patch_actions(request: Request):
        body = await json_body(request)
        changes = body.get("settings")
        if not isinstance(changes, dict) or not changes:
            raise HTTPException(400, 'body must be {"settings": {"ban_member": "ask", ...}}')
        return _apply(changes, body.get("confirm_destructive") is True)

    return r
