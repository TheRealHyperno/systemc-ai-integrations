"""Discord voice integration: join a voice channel and speak text back.

Note: this version is bot -> voice (text-to-speech) only. Receiving/speech-to-text
requires additional voice receive libraries not bundled here, so commands are still
issued via normal text messages."""

import os
import tempfile
import asyncio

NAME = "discord_voice"
DESCRIPTION = "Join Discord voice channels and speak text back (TTS)."

PANEL = {
    "title": "Discord Voice",
    "icon": "🔊",
    "fields": [
        {"key": "VOICE_TTS_ENGINE", "label": "TTS engine", "type": "choice",
         "choices": ["gtts", "pyttsx3"], "default": "gtts"},
        {"key": "VOICE_TTS_LANG", "label": "Language", "type": "text", "default": "en"},
    ],
}

PROMPT = ("Use discord_voice tools to join/leave voice channels and speak text aloud "
          "when the user asks you to talk in voice chat.")

TOOLS = [
    {"name": "voice_join", "description": "Join a Discord voice channel by ID.",
     "input_schema": {"type": "object", "properties": {"channel_id": {"type": "string"}}, "required": ["channel_id"]}},
    {"name": "voice_leave", "description": "Leave the current voice channel in a guild.",
     "input_schema": {"type": "object", "properties": {"guild_id": {"type": "string"}}, "required": ["guild_id"]}},
    {"name": "voice_say", "description": "Speak text in a joined voice channel.",
     "input_schema": {"type": "object", "properties": {"guild_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["guild_id", "text"]}},
    {"name": "voice_status", "description": "Show current voice connections.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _raw(ctx):
    return getattr(ctx, "raw_channel", None) or getattr(ctx, "channel", None)


def _client(ctx):
    ch = _raw(ctx)
    cli = getattr(ch, "_client", None)
    if cli is None and hasattr(ch, "_state"):
        cli = getattr(ch._state, "_get_client", lambda: None)()
    if cli is None:
        guild = getattr(ch, "guild", None)
        if guild is not None:
            cli = getattr(guild, "_state", None)
            cli = getattr(cli, "_get_client", lambda: None)() if cli else None
    return cli


def _guild(ctx, guild_id):
    cli = _client(ctx)
    if cli is None:
        return None
    try:
        return cli.get_guild(int(guild_id))
    except Exception:
        return None


def _tts(text, engine, lang):
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    os.remove(path)
    if engine == "pyttsx3":
        import pyttsx3
        out = path.replace(".mp3", ".wav")
        e = pyttsx3.init()
        e.save_to_file(text, out)
        e.runAndWait()
        return out
    from gtts import gTTS
    gTTS(text=text, lang=lang).save(path)
    return path


async def handle(ctx, name, args):
    if name == "voice_status":
        cli = _client(ctx)
        vcs = list(getattr(cli, "voice_clients", []) or []) if cli else []
        return {"connections": [{"guild_id": str(v.guild.id), "channel": str(v.channel.id)} for v in vcs]}
    if name == "voice_join":
        gid = None
        ch = _raw(ctx)
        guild = getattr(ch, "guild", None)
        channel_id = str(args.get("channel_id", "")).strip()
        if guild is None:
            # try all guilds via client
            cli = _client(ctx)
            for g in getattr(cli, "guilds", []) or []:
                c = g.get_channel(int(channel_id)) if channel_id.isdigit() else None
                if c is not None:
                    guild = g
                    break
        if guild is None:
            return {"error": "could not resolve guild"}
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if channel is None:
            return {"error": "voice channel not found"}
        vc = guild.voice_client
        if vc is not None and vc.is_connected():
            await vc.move_to(channel)
        else:
            vc = await channel.connect()
        return {"joined": str(channel.id), "guild_id": str(guild.id)}
    if name in ("voice_leave", "voice_say"):
        guild_id = str(args.get("guild_id", "")).strip()
        guild = _guild(ctx, guild_id)
        if guild is None:
            return {"error": "guild not found"}
        vc = guild.voice_client
        if vc is None or not vc.is_connected():
            return {"error": "not connected to voice"}
        if name == "voice_leave":
            await vc.disconnect()
            return {"left": True}
        text = str(args.get("text", "")).strip()
        if not text:
            return {"error": "text is required"}
        try:
            engine = "gtts"
            lang = "en"
            try:
                from integrations import get_setting as _gs
                engine = _gs("VOICE_TTS_ENGINE", "gtts")
                lang = _gs("VOICE_TTS_LANG", "en")
            except Exception:
                pass
            path = await asyncio.to_thread(_tts, text, engine, lang)
            vc.play(__import__("discord").FFmpegPCMAudio(path))
            # wait a bit
            while vc.is_playing():
                await asyncio.sleep(0.5)
            try:
                os.remove(path)
            except OSError:
                pass
            return {"spoken": True}
        except Exception as e:
            return {"error": str(e)}
    return {"error": f"unknown tool: {name}"}
