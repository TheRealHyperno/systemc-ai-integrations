"""Weather integration for SystemC."""

NAME = "weather"
DESCRIPTION = "Look up current weather for a city (uses wttr.in)."

PANEL = {
    "title": "Weather",
    "icon": "🌤️",
    "fields": [
        {"key": "WEATHER_UNITS", "label": "Units", "type": "choice",
         "choices": ["metric", "imperial"], "default": "metric",
         "description": "metric = Celsius, imperial = Fahrenheit."},
    ],
}

PROMPT = "Use weather_lookup when the user asks about the weather or temperature for a place."

TOOLS = [
    {
        "name": "weather_lookup",
        "description": "Get the current weather summary for a city.",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
]


def available(ctx):
    return True


async def handle(ctx, name, args):
    if name != "weather_lookup":
        return {"error": f"unknown tool: {name}"}
    city = str(args.get("city", "")).strip()
    if not city:
        return {"error": "city is required"}
    import urllib.request
    import urllib.parse
    try:
        from integrations import get_setting as _gs
        units_pref = _gs("WEATHER_UNITS", "metric")
    except Exception:
        units_pref = "metric"
    units = "m" if units_pref == "metric" else "u"
    url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&{units}"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        return {"error": str(e)}
    return {"weather": text, "city": city}
