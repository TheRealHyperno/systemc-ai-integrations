"""EXPERIMENTAL voice call integration: turn-based listen -> transcribe -> reply -> TTS.

This is NOT full-duplex. It joins a voice channel, records a short utterance,
transcribes it with Google Web Speech, runs a normal agent turn, then speaks
the reply with gTTS. Repeat until 'hang up' is heard or the turn limit is hit.

Requirements: discord-ext-voice-recv, gTTS, SpeechRecognition, ffmpeg.
"""

import asyncio
import io
import os
import tempfile
import time
import wave

NAME = "voice_call"
DESCRIPTION = "Experimental turn-based voice call (listen -> transcribe -> reply -> speak)."

PANEL = {
    "title": "Voice Call (experimental)",
    "icon": "📞",
    "fields": [
        {"key": "VOICE_CALL_MAX_TURNS", "label": "Max turns", "type": "number",
         "default": 20, "min": 1, "max": 100},
    ],
}

PROMPT = ("Use voice_call_start when the user wants to talk in voice. Requires the user "
          "to be in a voice channel (pass its ID). voice_call_stop ends the call.")

TOOLS = [
    {"name": "voice_call_start", "description": "Start a turn-based voice call in a Discord voice channel.",
     "input_schema": {"type": "object", "properties": {"channel_id": {"type": "string"}}, "required": ["channel_id"]}},
    {"name": "voice_call_stop", "description": "Stop an active voice call.",
     "input_schema": {"type": "object", "properties": {"guild_id": {"type": "string"}}, "required": ["guild_id"]}},
    {"name": "voice_call_status", "description": "List active voice calls.",
     "input_schema": {"type": "object", "properties": {}}},
]

_CALLS = {}


def _raw(ctx):
    return getattr(ctx, "raw_channel", None) or getattr(ctx, "channel", None)


def _client(ctx):
    ch = _raw(ctx)
    cli = getattr(ch, "_client", None)
    if cli is None and hasattr(ch, "_state"):
        try:
            cli = ch._state._get_client()
        except Exception:
            cli = None
    if cli is None and getattr(ch, "guild", None) is not None:
        st = getattr(ch.guild, "_state", None)
        cli = getattr(st, "_get_client", lambda: None)() if st else None
    return cli


async def _run_call(ctx, channel, vc, channel_id, guild):
    import speech_recognition as sr
    from gtts import gTTS
    try:
        from discord.ext.voice_recv import VoiceRecvClient, BasicSink
    except Exception as e:
        await channel.send(f"voice_recv missing: {e}")
        return
    try:
        await channel.send(f"Voice call started in channel {channel_id}. Say 'hang up' to stop.")
        turns = 20
        try:
            from integrations import get_setting as _gs
            turns = int(_gs("VOICE_CALL_MAX_TURNS", 20))
        except Exception:
            pass
        for _ in range(turns):
            buf = {"pcm": bytearray(), "last": 0.0}
            import time
            def sink_cb(user, data, buf=buf):
                if user is None:
                    return
                buf["pcm"].extend(data.pcm)
                buf["last"] = time.time()
            sink = BasicSink(sink_cb)
            vc.listen(sink)
            start = time.time()
            await asyncio.sleep(0.5)
            while time.time() - start < 15:
                await asyncio.sleep(0.2)
                if buf["pcm"] and (time.time() - buf["last"] > 1.2):
                    break
            vc.stop_listening()
            pcm = bytes(buf["pcm"])
            if not pcm or len(pcm) < 48000:
                continue
            fd, wav_path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000)
                w.writeframes(pcm)
            text = None
            try:
                r = sr.Recognizer()
                with sr.AudioFile(wav_path) as src:
                    audio = r.record(src)
                text = r.recognize_google(audio)
            except Exception:
                text = None
            try:
                os.remove(wav_path)
            except OSError:
                pass
            if not text:
                continue
            await channel.send(f"heard: {text}")
            if "hang up" in text.lower() or "hangup" in text.lower():
                break
            # invoke the main bot's agent
            try:
                import bot as _bot
                ag = getattr(_bot, "agent", None)
                if ag is not None:
                    await ag.run_turn(channel, text, [], author={"id": ctx.author_id, "name": ctx.author_name})
            except Exception as e:
                await channel.send(f"agent error: {e}")
        await channel.send("Voice call ended.")
    finally:
        if vc is not None and getattr(vc, "is_connected", lambda: False)():
            await vc.disconnect()
        _CALLS.pop(str(guild.id), None)


async def handle(ctx, name, args):
    if name == "voice_call_status":
        return {"active": list(_CALLS.keys()), "count": len(_CALLS)}
    if name == "voice_call_stop":
        gid = str(args.get("guild_id", "")).strip()
        t = _CALLS.pop(gid, None)
        if t:
            t.cancel()
        ch = _raw(ctx)
        guild = getattr(ch, "guild", None)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect()
        return {"stopped": True}
    if name != "voice_call_start":
        return {"error": f"unknown tool: {name}"}
    channel_id = str(args.get("channel_id", "")).strip()
    if not channel_id.isdigit():
        return {"error": "channel_id must be numeric"}
    ch = _raw(ctx)
    cli = _client(ctx)
    guild = None
    channel = None
    if ch is not None and getattr(ch, "guild", None) is not None:
        guild = ch.guild
        channel = guild.get_channel(int(channel_id))
    if channel is None and cli is not None:
        for g in getattr(cli, "guilds", []) or []:
            c = g.get_channel(int(channel_id))
            if c is not None:
                guild = g
                channel = c
                break
    if channel is None or guild is None:
        return {"error": "voice channel not found"}
    if str(guild.id) in _CALLS:
        return {"error": "a call is already active in this guild"}
    try:
        from discord.ext.voice_recv import VoiceRecvClient
        vc = await channel.connect(cls=VoiceRecvClient)
    except Exception as e:
        return {"error": f"join failed: {e}"}
    t = asyncio.create_task(_run_call(ctx, ch, vc, channel_id, guild))
    _CALLS[str(guild.id)] = t
    return {"started": True, "channel_id": channel_id, "guild_id": str(guild.id)}
