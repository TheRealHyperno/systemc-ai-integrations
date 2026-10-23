"""Quick notes integration: save/search short notes in a local JSON file."""

import json, os, time

NAME = "quick_notes"
DESCRIPTION = "Save and search short notes for the owner."

PANEL = {"title": "Quick Notes", "icon": "📝", "fields": []}

PROMPT = "Use quick_notes to save or recall short notes the user asks to remember."

TOOLS = [
    {
        "name": "note_add",
        "description": "Save a note.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "note_list",
        "description": "List saved notes (optionally filter by keyword).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "note_delete",
        "description": "Delete a note by id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]

_NOTES = os.path.join(os.getcwd(), ".quick_notes.json")


def _load():
    if not os.path.exists(_NOTES):
        return []
    try:
        with open(_NOTES) as f:
            return json.load(f)
    except Exception:
        return []


def _save(notes):
    with open(_NOTES, "w") as f:
        json.dump(notes, f, indent=2)


async def handle(ctx, name, args):
    if name == "note_add":
        notes = _load()
        notes.append({"id": str(len(notes) + 1), "text": str(args.get("text", "")).strip(),
                      "created_at": time.time()})
        _save(notes)
        return {"saved": True, "id": notes[-1]["id"]}
    if name == "note_list":
        q = (args.get("query") or "").lower()
        notes = _load()
        if q:
            notes = [n for n in notes if q in n["text"].lower()]
        return {"notes": notes, "count": len(notes)}
    if name == "note_delete":
        notes = _load()
        before = len(notes)
        notes = [n for n in notes if n["id"] != str(args.get("id"))]
        _save(notes)
        return {"deleted": before - len(notes)}
    return {"error": f"unknown tool: {name}"}
