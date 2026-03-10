import asyncio
import base64
import json
import logging
import re
import os
import time

from google import genai
from google.genai import types

from backend.agent import (
    find_emergency_services,
    save_meeting_note,
    search_cultural_context,
)

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "notional-cirrus-458606-e0")
REGION = "us-central1"
MODEL = "gemini-2.5-flash-native-audio-latest"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
AGENT_SYSTEM_PROMPT = (
    "You are ToneLens, a live real-time emotional intelligence agent. "
    "You see through the user's camera and hear what people around them are saying.\n\n"
    "For every utterance you detect, respond in EXACTLY these 4 lines, nothing else:\n\n"
    "TRANSLATION: [if not English, translate to English. If English, write 'English detected']\n"
    "EMOTION: [exactly one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
    "SUBTEXT: [exactly one sentence: the real meaning behind the words]\n"
    "SUGGEST: [exactly one sentence: how to respond]\n\n"
    "RULES:\n"
    "- Never write anything before TRANSLATION:\n"
    "- Never write anything after the SUGGEST line\n"
    "- Never use markdown, asterisks, or bold\n"
    "- Never explain your reasoning\n"
    "- Always output exactly 4 lines\n"
    "- Tailor suggestions to mode: "
    "travel=be helpful and warm, "
    "meeting=be strategic and professional, "
    "present=focus on audience engagement\n"
    "Supported languages: English, French, Spanish, Japanese, Hindi, Tamil, "
    "Mandarin, Arabic, Portuguese, German, Korean, Italian, Russian, Dutch"
)

PRESENT_SYSTEM_PROMPT = (
    "You are ToneLens in Presentation Coach mode.\n\n"
    "Your ENTIRE structured response must be EXACTLY these 4 lines, nothing else:\n"
    "TRANSLATION: [list any filler words heard: um, uh, like, you know, so, basically - or 'None detected']\n"
    "EMOTION: [exactly one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
    "SUBTEXT: [exactly one sentence: assess speaking pace - too fast, too slow, or good pace]\n"
    "SUGGEST: [exactly one sentence: specific coaching tip to improve delivery right now]\n\n"
    "RULES:\n"
    "- Never write anything before TRANSLATION:\n"
    "- Never write anything after the SUGGEST line\n"
    "- Never use markdown, asterisks, or bold\n"
    "- Never explain your reasoning\n"
    "- Always output exactly 4 lines"
)

NEGOTIATE_SYSTEM_PROMPT = (
    "You are ToneLens in Negotiation Coach mode.\n\n"
    "You see through the user's camera and hear both sides of a negotiation.\n\n"
    "For every utterance, respond in EXACTLY these 5 lines, nothing else:\n\n"
    "TRANSLATION: [if not English, translate. If English, write 'English detected']\n"
    "EMOTION: [one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
    "SUBTEXT: [one sentence: the tactical intent behind the words]\n"
    "POWER: [number 1-100 indicating leverage balance. 50=balanced, <50=they lead, >50=you lead]\n"
    "SUGGEST: [one sentence: your best tactical move right now]\n\n"
    "RULES:\n"
    "- Never write anything before TRANSLATION:\n"
    "- Never write anything after the SUGGEST line\n"
    "- Never use markdown, asterisks, or bold\n"
    "- Never explain your reasoning\n"
    "- Always output exactly 5 lines\n"
    "- POWER should shift based on concessions, anchoring, body language, and verbal cues\n"
    "- SUGGEST should be tactical: counter-offers, silence usage, walk-away points"
)


class GeminiBridge:
    """Manages a bidirectional live streaming session with the Gemini Live API."""

    def __init__(self):
        # Google AI Studio client — used for the Gemini Live session
        self.client = genai.Client(
            api_key=os.environ.get("GOOGLE_API_KEY"),
            http_options={"api_version": "v1alpha"},
        )
        # Vertex AI client — initialized lazily to avoid ADC errors on local dev
        self._vertex_client = None
        self.live_session = None
        self._session_ctx = None
        self.websocket = None
        self.session_id: str = ""
        self.mode: str = "travel"
        self._connected: bool = False
        self._receive_task: asyncio.Task | None = None
        self.last_frame_time: float = 0.0
        self.reconnect_attempts: int = 0
        self._text_buffer: str = ""
        # User location — defaults to Chennai; overwritten when frontend sends GPS fix
        self.user_lat: float = 13.0827
        self.user_lng: float = 80.2707

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def connect(self, websocket, session_id: str, mode: str = "travel"):
        self.websocket = websocket
        self.session_id = session_id
        self.mode = mode
        self._connected = True
        self.reconnect_attempts = 0
        await self._establish_session()

    async def send_frame(self, base64_jpeg: str):
        if not self.live_session:
            return
        now = time.time()
        if now - self.last_frame_time < 2.0:
            return
        self.last_frame_time = now
        try:
            image_bytes = base64.b64decode(base64_jpeg)
            await self.live_session.send_realtime_input(
                video=types.Blob(mime_type="image/jpeg", data=image_bytes)
            )
        except Exception as e:
            logger.warning(f"Failed to send frame: {e}")

    async def send_audio(self, base64_pcm: str):
        if not self.live_session:
            return
        try:
            audio_bytes = base64.b64decode(base64_pcm)
            await self.live_session.send_realtime_input(
                audio=types.Blob(mime_type="audio/pcm;rate=16000", data=audio_bytes)
            )
        except Exception as e:
            logger.warning(f"Failed to send audio: {e}")

    async def disconnect(self):
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing live session: {e}")
            self._session_ctx = None
            self.live_session = None
        logger.info(f"Disconnected session {self.session_id}")

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------
    async def _establish_session(self):
        if self.mode == "present":
            prompt = PRESENT_SYSTEM_PROMPT
        elif self.mode == "negotiate":
            prompt = NEGOTIATE_SYSTEM_PROMPT
        else:
            prompt = AGENT_SYSTEM_PROMPT
        # NOTE: gemini-2.5-flash-native-audio-latest does not support
        # function_declarations in LiveConnectConfig — tools are omitted.
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=types.Content(
                parts=[types.Part(text=prompt)]
            ),
        )
        try:
            self._session_ctx = self.client.aio.live.connect(
                model=MODEL, config=config
            )
            self.live_session = await self._session_ctx.__aenter__()
            self._receive_task = asyncio.create_task(self._receive_loop())
            logger.info(f"Live session established for {self.session_id}")
        except Exception as e:
            logger.error(f"Failed to establish live session: {e}")
            await self._send_ws(
                {"type": "error", "msg": f"Failed to connect to Gemini: {e}"}
            )

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------
    async def _receive_loop(self):
        try:
            while self._connected:
                async for msg in self.live_session.receive():
                    if not self._connected:
                        break
                    await self._process_message(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            err_name = type(e).__name__
            logger.error(f"Receive loop error ({err_name}): {e}")
            if "ResourceExhausted" in err_name or "ResourceExhausted" in str(e):
                await self._send_ws(
                    {"type": "error", "msg": "Rate limit reached. Retrying in 5 seconds..."}
                )
                await asyncio.sleep(5)
            if self._connected:
                await self._try_reconnect()

    async def _process_message(self, msg):
        try:
            if hasattr(msg, "server_content") and msg.server_content:
                sc = msg.server_content
                if hasattr(sc, "model_turn") and sc.model_turn:
                    for part in sc.model_turn.parts:
                        if hasattr(part, "text") and part.text:
                            self._text_buffer += part.text
                        if hasattr(part, "inline_data") and part.inline_data:
                            audio_b64 = base64.b64encode(part.inline_data.data).decode()
                            await self._send_ws({"type": "audio", "data": audio_b64})
                if hasattr(sc, "turn_complete") and sc.turn_complete:
                    if self._text_buffer:
                        clean = await self._reformat_response(self._text_buffer)
                        self._text_buffer = ""
                        await self._parse_and_send(clean)
        except Exception as e:
            logger.warning(f"Error processing message: {e}")

    # ------------------------------------------------------------------
    # Text parsing
    # ------------------------------------------------------------------
    async def _parse_and_send(self, text: str):
        lines = text.strip().split("\n")
        exchange: dict = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            upper = line.upper()

            if upper.startswith("TRANSLATION:"):
                content = line.split(":", 1)[1].strip()
                source_lang = "Unknown"
                if "english detected" in content.lower():
                    source_lang = "English"
                else:
                    for lang in [
                        "French", "Spanish", "Japanese", "Hindi", "Tamil",
                        "Mandarin", "Arabic", "Portuguese", "German",
                        "Korean", "Italian", "Russian", "Dutch",
                    ]:
                        if lang.lower() in text.lower():
                            source_lang = lang
                            break
                await self._send_ws(
                    {"type": "translation", "text": content, "source_language": source_lang}
                )
                exchange["translation"] = content
                exchange["source_language"] = source_lang

            elif upper.startswith("EMOTION:"):
                content = line.split(":", 1)[1].strip()
                label, confidence = self._parse_emotion(content)
                await self._send_ws(
                    {"type": "emotion", "label": label, "confidence": confidence, "signals": []}
                )
                exchange["emotion"] = label
                exchange["emotion_conf"] = confidence

            elif upper.startswith("SUBTEXT:"):
                content = line.split(":", 1)[1].strip()
                await self._send_ws({"type": "subtext", "text": content})
                exchange["subtext"] = content

            elif upper.startswith("POWER:"):
                content = line.split(":", 1)[1].strip()
                score = 50
                m = re.search(r"(\d+)", content)
                if m:
                    score = max(0, min(100, int(m.group(1))))
                await self._send_ws({"type": "power", "score": score})
                exchange["power"] = score

            elif upper.startswith("SUGGEST:"):
                content = line.split(":", 1)[1].strip()
                await self._send_ws({"type": "suggestion", "text": content})
                exchange["suggestion"] = content

            else:
                await self._send_ws({"type": "transcript", "text": line})

        if exchange:
            try:
                from backend import session_manager

                await session_manager.log_exchange(self.session_id, exchange)
            except Exception as e:
                logger.warning(f"Failed to log exchange: {e}")

            # Fire keyword-based agent actions without blocking the WS send path
            asyncio.create_task(self._run_keyword_actions(exchange, text))

    # ------------------------------------------------------------------
    # Keyword-driven agent actions (replaces Live API function_declarations)
    # ------------------------------------------------------------------
    async def _run_keyword_actions(self, exchange: dict, raw_text: str):
        translation  = exchange.get("translation", "")
        source_lang  = exchange.get("source_language", "Unknown")
        emotion      = exchange.get("emotion", "calm")
        emotion_conf = exchange.get("emotion_conf", 0.7)
        subtext      = exchange.get("subtext", "")

        # 1. Non-English speech → cultural context tip
        if (
            translation
            and "english detected" not in translation.lower()
            and source_lang not in ("Unknown", "English")
        ):
            try:
                result = await search_cultural_context(source_lang, translation)
                await self._send_ws(
                    {"type": "agent_action", "action": "cultural_tip", "data": result}
                )
            except Exception as e:
                logger.warning(f"Cultural context lookup failed: {e}")

        # 2. Distress signal → emergency services
        distress_text = (raw_text + " " + subtext).lower()
        distress_match = bool(
            re.search(r"\b(help|emergency|hurt|scared|danger|distress|threat)\b", distress_text)
        )
        high_stress = emotion in ("frustrated", "angry") and emotion_conf > 0.80
        if distress_match or high_stress:
            try:
                situation = subtext or f"Distress detected ({emotion})"
                result = find_emergency_services(situation, self.user_lat, self.user_lng)
                await self._send_ws(
                    {"type": "agent_action", "action": "emergency", "data": result}
                )
            except Exception as e:
                logger.warning(f"Emergency services lookup failed: {e}")

        # 3. Meeting mode + important subtext → save note
        if self.mode == "meeting" and subtext:
            important = bool(
                re.search(
                    r"\b(decided|will|must|deadline|action|commit|agree|plan"
                    r"|next step|responsible|by end|assigned)\b",
                    subtext.lower(),
                )
            )
            if important:
                try:
                    result = await save_meeting_note(self.session_id, subtext, emotion)
                    await self._send_ws(
                        {"type": "agent_action", "action": "note_saved", "data": result}
                    )
                except Exception as e:
                    logger.warning(f"Save meeting note failed: {e}")

    # ------------------------------------------------------------------
    # Vertex AI client (lazy, with ADC fallback)
    # ------------------------------------------------------------------
    def _get_vertex_client(self):
        if self._vertex_client is None:
            try:
                self._vertex_client = genai.Client(
                    vertexai=True,
                    project=os.environ.get("GOOGLE_CLOUD_PROJECT", PROJECT_ID),
                    location=REGION,
                )
            except Exception as e:
                logger.warning(f"Vertex AI client init failed: {e}")
                return None
        return self._vertex_client

    # ------------------------------------------------------------------
    # Two-model pipeline: reformat Live API output via Flash
    # ------------------------------------------------------------------
    async def _reformat_response(self, raw_text: str) -> str:
        if self.mode == "negotiate":
            mode_context = "This is a negotiation. Focus on tactical intent, leverage, and power dynamics."
            line_count = "5"
            extra_line = "POWER: [number 1-100 for leverage balance. 50=balanced]\n"
        elif self.mode == "present":
            mode_context = "The speaker is giving a presentation. Focus on filler words, confidence, and pace."
            line_count = "4"
            extra_line = ""
        else:
            mode_context = "The speaker is in a conversation."
            line_count = "4"
            extra_line = ""
        prompt = (
            f"Extract and reformat this analysis into EXACTLY {line_count} lines.\n"
            f"Output ONLY these {line_count} lines, nothing else:\n\n"
            f"TRANSLATION: [if non-English detected, translate to English. Otherwise write 'English detected']\n"
            f"EMOTION: [one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
            f"SUBTEXT: [one sentence: the real meaning behind the words]\n"
            f"{extra_line}"
            f"SUGGEST: [one sentence: the best way to respond]\n\n"
            f"Context: {mode_context}\n"
            f"Analysis to reformat:\n{raw_text}"
        )
        try:
            client = self._get_vertex_client()
            if client is None:
                return raw_text
            response = await client.aio.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
            )
            formatted = response.text.strip()
            if "TRANSLATION:" in formatted and "EMOTION:" in formatted:
                logger.debug("Reformat succeeded")
                return formatted
            logger.warning("Reformat output missing required labels, using raw")
            return raw_text
        except Exception as e:
            logger.warning(f"Reformat failed: {e}")
            return raw_text

    @staticmethod
    def _parse_emotion(text: str) -> tuple[str, float]:
        valid = {
            "nervous", "confident", "frustrated", "happy",
            "uncertain", "deceptive", "calm", "excited", "angry",
        }
        label = "calm"
        confidence = 0.7
        text_lower = text.lower()
        for emotion in valid:
            if emotion in text_lower:
                label = emotion
                break
        match = re.search(r"(\d+)%", text)
        if match:
            confidence = max(0.0, min(1.0, int(match.group(1)) / 100.0))
        return label, confidence

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------
    async def _try_reconnect(self):
        if self.reconnect_attempts >= 3:
            logger.error("Max reconnection attempts reached")
            await self._send_ws(
                {"type": "error", "msg": "Connection lost. Please refresh the page."}
            )
            return

        self.reconnect_attempts += 1
        logger.info(f"Reconnection attempt {self.reconnect_attempts}/3 for {self.session_id}")
        await asyncio.sleep(2)

        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_ctx = None
            self.live_session = None

        try:
            await self._establish_session()
            self.reconnect_attempts = 0
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            if self._connected:
                await self._try_reconnect()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _send_ws(self, data: dict):
        try:
            await self.websocket.send_json(data)
        except Exception as e:
            logger.warning(f"Failed to send WebSocket message: {e}")
