"""
OpenRouter Live Session adapter for KIVO.
Provides a Gemini Live WebSocket compatible interface backed by OpenRouter
(OpenAI-compatible REST API) with EdgeTTS streaming and speech recognition.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import numpy as np
import requests
import miniaudio
import edge_tts

try:
    from core.stt import WhisperSTT
    _WHISPER_AVAILABLE = True
except Exception:
    _WHISPER_AVAILABLE = False

try:
    import speech_recognition as sr
    _SR_AVAILABLE = True
except ImportError:
    _SR_AVAILABLE = False


class ToolCallItem:
    def __init__(self, name: str, id_: str, args: dict):
        self.name = name
        self.id = id_
        self.args = args


class ResponseWrapper:
    def __init__(
        self,
        text: str | None = None,
        pcm_bytes: bytes | None = None,
        tool_calls: list[dict] | None = None,
        turn_complete: bool = False,
        user_text: str | None = None,
    ):
        self.data = pcm_bytes
        self.server_content = None
        self.tool_call = None

        if text is not None or user_text is not None or turn_complete:
            self.server_content = type(
                "ServerContent",
                (),
                {
                    "output_transcription": type("OutTx", (), {"text": text})() if text else None,
                    "input_transcription": type("InTx", (), {"text": user_text})() if user_text else None,
                    "turn_complete": turn_complete,
                },
            )()

        if tool_calls:
            self.tool_call = type(
                "ToolCall",
                (),
                {
                    "function_calls": [
                        ToolCallItem(tc["name"], tc["id"], tc["args"])
                        for tc in tool_calls
                    ]
                },
            )()


def _convert_tool_declarations(decls: list) -> list[dict]:
    openai_tools = []
    for d in decls:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        desc = d.get("description", "")
        params = d.get("parameters", {}).copy() if isinstance(d.get("parameters"), dict) else {}

        def _lower_types(obj):
            if isinstance(obj, dict):
                res = {}
                for k, v in obj.items():
                    if k == "type" and isinstance(v, str):
                        res[k] = v.lower()
                    else:
                        res[k] = _lower_types(v)
                return res
            elif isinstance(obj, list):
                return [_lower_types(x) for x in obj]
            return obj

        norm_params = _lower_types(params)
        if "type" not in norm_params:
            norm_params["type"] = "object"

        openai_tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": norm_params,
            },
        })
    return openai_tools


_VOICE_MAP = {
    "Charon": "en-GB-RyanNeural",
    "Puck": "en-US-GuyNeural",
    "Kore": "en-GB-SoniaNeural",
    "Fenrir": "en-US-ChristopherNeural",
    "Aoede": "en-US-JennyNeural",
}


class OpenRouterLiveSession:
    """Emulates google.genai Live session using OpenRouter + EdgeTTS."""

    def __init__(
        self,
        api_key: str,
        model: str = "nvidia/nemotron-3-ultra-550b-a55b:free",
        system_prompt: str = "",
        system_instruction: str = "",
        tools: list | None = None,
        voice: str = "en-GB-RyanNeural",
        voice_name: str = "",
    ):
        self.api_key = api_key
        self.model = model
        self.system_prompt = system_prompt or system_instruction or ""
        raw_voice = voice_name or voice or "en-GB-RyanNeural"
        self.voice = _VOICE_MAP.get(raw_voice, raw_voice if "Neural" in raw_voice else "en-GB-RyanNeural")
        self.tools = _convert_tool_declarations(tools or [])

        self.messages: list[dict] = []
        if self.system_prompt:
            self.messages.append({"role": "system", "content": self.system_prompt})

        self._queue: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._whisper = None
        if _WHISPER_AVAILABLE:
            try:
                self._whisper = WhisperSTT("tiny.en")
            except Exception as e:
                print(f"[STT] Could not initialize Whisper: {e}")
        self._recognizer = sr.Recognizer() if _SR_AVAILABLE else None

        self._audio_buffer = bytearray()
        self._preroll_buffer = bytearray()
        self._speech_active = False
        self._last_speech_time = 0.0
        self._speech_start_time = 0.0
        self._energy_threshold = 350.0  # RMS threshold for voice activity
        self._silence_gap = 0.55        # 550ms pause finishes utterance
        self._is_generating = False
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        self._closed = True

    async def send_client_content(self, turns: any, turn_complete: bool = True):
        """Called by main.py for text commands, startup briefing, etc."""
        turn_list = turns if isinstance(turns, list) else [turns]
        text_parts = []
        images = []

        for t in turn_list:
            if isinstance(t, dict):
                parts = t.get("parts", [])
            else:
                parts = getattr(t, "parts", []) or []

            for p in parts:
                if isinstance(p, dict):
                    txt = p.get("text")
                    idata = p.get("inline_data")
                else:
                    txt = getattr(p, "text", None)
                    idata = getattr(p, "inline_data", None)

                if txt:
                    text_parts.append(txt)
                elif idata:
                    if isinstance(idata, dict):
                        b64_data = idata.get("data", "")
                        mime = idata.get("mime_type", "image/jpeg")
                    else:
                        b64_data = getattr(idata, "data", "")
                        mime = getattr(idata, "mime_type", "image/jpeg")
                    if isinstance(b64_data, bytes):
                        b64_data = base64.b64encode(b64_data).decode("ascii")
                    images.append(f"data:{mime};base64,{b64_data}")

        full_text = " ".join(text_parts).strip()
        if not full_text and not images:
            return

        user_content: str | list[dict]
        if images:
            content_list: list[dict] = []
            for img in images:
                content_list.append({"type": "image_url", "image_url": {"url": img}})
            if full_text:
                content_list.append({"type": "text", "text": full_text})
            user_content = content_list
        else:
            user_content = full_text

        async with self._lock:
            self.messages.append({"role": "user", "content": user_content})

        await self._trigger_generation(user_text=full_text or "(Image attached)")

    async def send_tool_response(self, function_responses: list):
        """Called by main.py after executing tools."""
        async with self._lock:
            for fr in function_responses:
                fid = getattr(fr, "id", None) or (fr.get("id") if isinstance(fr, dict) else "call_0")
                fname = getattr(fr, "name", None) or (fr.get("name") if isinstance(fr, dict) else "tool")
                fres = getattr(fr, "response", None) or (fr.get("response") if isinstance(fr, dict) else {})
                if isinstance(fres, dict) and "result" in fres:
                    result_str = str(fres["result"])
                else:
                    result_str = json.dumps(fres)

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": fid,
                    "name": fname,
                    "content": result_str,
                })

        await self._trigger_generation(user_text=None)

    async def send_realtime_input(self, audio=None):
        """Receives PCM audio chunks from mic streaming and uses VAD to capture full phrases."""
        if audio is None:
            return
        data = getattr(audio, "data", None) or (audio.get("data") if isinstance(audio, dict) else None)
        if not data:
            return

        now = time.monotonic()
        arr = np.frombuffer(data, dtype=np.int16)
        if len(arr) == 0:
            return
        rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))

        if rms >= self._energy_threshold:
            if not self._speech_active:
                self._speech_active = True
                self._speech_start_time = now
                self._audio_buffer.clear()
                # Include preroll so the beginning of the word is never cut off
                self._audio_buffer.extend(self._preroll_buffer)
            self._last_speech_time = now
            self._audio_buffer.extend(data)
        elif self._speech_active:
            self._audio_buffer.extend(data)
            # Check if user paused after speaking
            if (now - self._last_speech_time) >= self._silence_gap:
                self._speech_active = False
                speech_dur = len(self._audio_buffer) / 32000.0  # 16kHz 16-bit mono = 32000 B/s
                if speech_dur >= 0.35:
                    buf = bytes(self._audio_buffer)
                    self._audio_buffer.clear()
                    asyncio.create_task(self._transcribe_audio_pcm(buf))
                else:
                    self._audio_buffer.clear()
        else:
            # Silence: retain ~200ms rolling preroll buffer
            self._preroll_buffer.extend(data)
            if len(self._preroll_buffer) > 6400:
                self._preroll_buffer = self._preroll_buffer[-6400:]

    async def _transcribe_audio_pcm(self, pcm_bytes: bytes):
        if not pcm_bytes:
            return
        loop = asyncio.get_running_loop()
        text = ""
        try:
            if self._whisper is not None:
                def _run_whisper():
                    audio_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                    audio_float = audio_int16.astype(np.float32) / 32768.0
                    return self._whisper.transcribe(audio_float)
                text = await loop.run_in_executor(None, _run_whisper)
            elif _SR_AVAILABLE and self._recognizer is not None:
                def _rec():
                    audio_data = sr.AudioData(pcm_bytes, 16000, 2)
                    return self._recognizer.recognize_google(audio_data)
                text = await loop.run_in_executor(None, _rec)
        except Exception as e:
            print(f"[STT] Recognition error: {e}")
            return

        text = (text or "").strip()
        if text:
            print(f"[STT] 🗣️ Heard: '{text}'")
            await self.send_client_content(
                turns={"role": "user", "parts": [{"text": text}]},
                turn_complete=True,
            )

    async def _trigger_generation(self, user_text: str | None = None):
        """Calls OpenRouter and streams the response back to _queue."""
        loop = asyncio.get_running_loop()

        if user_text:
            await self._queue.put(ResponseWrapper(user_text=user_text))

        def _fetch():
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/KIVO",
                "X-Title": "KIVO",
            }
            payload = {
                "model": self.model,
                "messages": self.messages,
            }
            if self.tools:
                payload["tools"] = self.tools

            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60,
            )
            if resp.status_code != 200:
                print(f"[OpenRouter] API error {resp.status_code}: {resp.text[:200]}")
                if resp.status_code == 429:
                    return {"choices": [{"message": {"role": "assistant", "content": "I apologize, your OpenRouter key has exceeded its daily request limit."}}]}
                return None
            return resp.json()

        try:
            data = await loop.run_in_executor(None, _fetch)
        except Exception as e:
            print(f"[OpenRouter] Request failed: {e}")
            await self._queue.put(ResponseWrapper(turn_complete=True))
            return

        if not data or "choices" not in data or not data["choices"]:
            await self._queue.put(ResponseWrapper(turn_complete=True))
            return

        choice = data["choices"][0]
        msg = choice.get("message", {})

        # Tool calls returned?
        if msg.get("tool_calls"):
            tcs = []
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                args = {}
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    pass
                tcs.append({
                    "name": fn.get("name", ""),
                    "id": tc.get("id", "call_0"),
                    "args": args,
                })

            async with self._lock:
                self.messages.append(msg)

            await self._queue.put(ResponseWrapper(tool_calls=tcs))
            return

        # Text reply
        reply_text = msg.get("content") or ""
        async with self._lock:
            self.messages.append({"role": "assistant", "content": reply_text})

        # Synthesize audio with edge_tts
        pcm_audio = b""
        if reply_text.strip():
            try:
                comm = edge_tts.Communicate(reply_text, self.voice)
                raw_mp3 = b""
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        raw_mp3 += chunk["data"]
                if raw_mp3:
                    decoded = miniaudio.decode(
                        raw_mp3,
                        output_format=miniaudio.SampleFormat.SIGNED16,
                        nchannels=1,
                        sample_rate=24000,
                    )
                    pcm_audio = bytes(decoded.samples)
            except Exception as e:
                print(f"[OpenRouter] EdgeTTS error: {e}")

        # Send text transcription and PCM slices (2400 bytes each)
        if reply_text:
            await self._queue.put(ResponseWrapper(text=reply_text))

        if pcm_audio:
            _SLICE = 2400
            for i in range(0, len(pcm_audio), _SLICE):
                await self._queue.put(ResponseWrapper(pcm_bytes=pcm_audio[i : i + _SLICE]))

        await self._queue.put(ResponseWrapper(turn_complete=True))

    async def receive(self):
        """Async generator mimicking session.receive() in main.py."""
        while not self._closed:
            item = await self._queue.get()
            yield item
