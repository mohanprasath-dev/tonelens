import asyncio
import base64
import json
import logging
import re
import os
import time

from google import genai
from google.genai import types

from backend.agent import SYSTEM_PROMPT, TOOL_FUNCTIONS

logger = logging.getLogger(__name__)

PROJECT_ID = "notional-cirrus-458606-e0"
REGION = "us-central1"
MODEL = "gemini-2.5-flash-native-audio-latest"

LIVE_SYSTEM_PROMPT = (
    "You are ToneLens. Your ENTIRE response must be EXACTLY these 4 lines, nothing else:\n"
    "TRANSLATION: [if not English, translate to English. If English, write 'English detected']\n"
    "EMOTION: [exactly one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
    "SUBTEXT: [exactly one sentence: the real meaning]\n"
    "SUGGEST: [exactly one sentence: how to respond]\n\n"
    "RULES:\n"
    "- Never write anything before TRANSLATION:\n"
    "- Never write anything after the SUGGEST line\n"
    "- Never use markdown, asterisks, or bold\n"
    "- Never explain your reasoning\n"
    "- Always output exactly 4 lines"
)

PRESENT_SYSTEM_PROMPT = (
    "You are ToneLens in Presentation Coach mode. Your ENTIRE response must be EXACTLY these 4 lines, nothing else:\n"
    "TRANSLATION: [list any filler words heard: um, uh, like, you know, so, basically — or 'None detected']\n"
    "EMOTION: [exactly one word: calm/nervous/confident/frustrated/happy/uncertain/excited/angry] - [number]%\n"
    "SUBTEXT: [exactly one sentence: assess speaking pace — too fast, too slow, or good pace]\n"
    "SUGGEST: [exactly one sentence: specific coaching tip to improve delivery right now]\n\n"
    "RULES:\n"
    "- Never write anything before TRANSLATION:\n"
    "- Never write anything after the SUGGEST line\n"
    "- Never use markdown, asterisks, or bold\n"
    "- Never explain your reasoning\n"
    "- Always output exactly 4 lines"
)

# ---------------------------------------------------------------------------
# Function declarations mirroring ADK tools for the Live API session
# ---------------------------------------------------------------------------
FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="analyze_emotion",
        description="Analyze emotional state from facial expression and voice tone descriptions",
        parameters={
            "type": "OBJECT",
            "properties": {
                "face_description": {
                    "type": "STRING",
                    "description": "Description of the person's facial expression and body language",
                },
                "voice_tone": {
                    "type": "STRING",
                    "description": "Description of the person's voice tone, pitch, and speaking pattern",
                },
            },
            "required": ["face_description", "voice_tone"],
        },
    ),
    types.FunctionDeclaration(
        name="get_cultural_context",
        description="Provide cultural context for a phrase or expression in a specific language",
        parameters={
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "The phrase to analyze"},
                "language": {"type": "STRING", "description": "Source language"},
                "situation": {"type": "STRING", "description": "Context of the phrase"},
            },
            "required": ["text", "language", "situation"],
        },
    ),
    types.FunctionDeclaration(
        name="suggest_response",
        description="Generate a contextually appropriate response suggestion",
        parameters={
            "type": "OBJECT",
            "properties": {
                "emotion": {"type": "STRING", "description": "Detected emotion"},
                "words": {"type": "STRING", "description": "What the speaker said"},
                "mode": {
                    "type": "STRING",
                    "description": "User mode: travel, meeting, or present",
                },
            },
            "required": ["emotion", "words", "mode"],
        },
    ),
    types.FunctionDeclaration(
        name="log_exchange",
        description="Log a conversation exchange to the session store",
        parameters={
            "type": "OBJECT",
            "properties": {
                "session_id": {"type": "STRING", "description": "Session identifier"},
                "data": {"type": "OBJECT", "description": "Exchange data to log"},
            },
            "required": ["session_id", "data"],
        },
    ),
]


class GeminiBridge:
    """Manages a bidirectional live streaming session with the Gemini Live API."""

    def __init__(self):
        self.client = genai.Client(
            api_key=os.environ.get("GOOGLE_API_KEY"),
            http_options={"api_version": "v1alpha"}
        )
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
        prompt = PRESENT_SYSTEM_PROMPT if self.mode == "present" else LIVE_SYSTEM_PROMPT
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
                    {
                        "type": "error",
                        "msg": "Rate limit reached. Retrying in 5 seconds...",
                    }
                )
                await asyncio.sleep(5)
            if self._connected:
                await self._try_reconnect()

    async def _process_message(self, msg):
        try:
            # --- Model content ---
            if hasattr(msg, "server_content") and msg.server_content:
                sc = msg.server_content
                if hasattr(sc, "model_turn") and sc.model_turn:
                    for part in sc.model_turn.parts:
                        if hasattr(part, "text") and part.text:
                            self._text_buffer += part.text
                        if hasattr(part, "inline_data") and part.inline_data:
                            audio_b64 = base64.b64encode(
                                part.inline_data.data
                            ).decode()
                            await self._send_ws(
                                {"type": "audio", "data": audio_b64}
                            )
                if hasattr(sc, "turn_complete") and sc.turn_complete:
                    if self._text_buffer:
                        await self._parse_and_send(self._text_buffer)
                        self._text_buffer = ""

            # --- Tool calls ---
            if hasattr(msg, "tool_call") and msg.tool_call:
                await self._handle_tool_call(msg.tool_call)
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
                    {
                        "type": "translation",
                        "text": content,
                        "source_language": source_lang,
                    }
                )
                exchange["translation"] = content

            elif upper.startswith("EMOTION:"):
                content = line.split(":", 1)[1].strip()
                label, confidence = self._parse_emotion(content)
                await self._send_ws(
                    {
                        "type": "emotion",
                        "label": label,
                        "confidence": confidence,
                        "signals": [],
                    }
                )
                exchange["emotion"] = label

            elif upper.startswith("SUBTEXT:"):
                content = line.split(":", 1)[1].strip()
                await self._send_ws({"type": "subtext", "text": content})
                exchange["subtext"] = content

            elif upper.startswith("SUGGEST:"):
                content = line.split(":", 1)[1].strip()
                await self._send_ws({"type": "suggestion", "text": content})
                exchange["suggestion"] = content

            else:
                await self._send_ws({"type": "transcript", "text": line})

        if exchange:
            try:
                from backend.agent import log_exchange_tool

                await log_exchange_tool(self.session_id, exchange)
            except Exception as e:
                logger.warning(f"Failed to log exchange: {e}")

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
    # Tool call handling
    # ------------------------------------------------------------------
    async def _handle_tool_call(self, tool_call):
        try:
            responses = []
            for fc in tool_call.function_calls:
                func = TOOL_FUNCTIONS.get(fc.name)
                if not func:
                    logger.warning(f"Unknown tool: {fc.name}")
                    responses.append(
                        types.FunctionResponse(
                            name=fc.name,
                            response={"error": f"Unknown tool: {fc.name}"},
                        )
                    )
                    continue

                args = dict(fc.args) if fc.args else {}
                if fc.name == "log_exchange":
                    args.setdefault("session_id", self.session_id)

                if asyncio.iscoroutinefunction(func):
                    result = await func(**args)
                else:
                    result = func(**args)

                # Forward structured results to the frontend
                if fc.name == "analyze_emotion" and isinstance(result, dict):
                    await self._send_ws(
                        {
                            "type": "emotion",
                            "label": result.get("emotion", "calm"),
                            "confidence": result.get("confidence", 0.7),
                            "signals": result.get("signals", []),
                        }
                    )

                serialized = (
                    json.dumps(result) if not isinstance(result, str) else result
                )
                responses.append(
                    types.FunctionResponse(
                        name=fc.name, response={"result": serialized}
                    )
                )

            if responses:
                await self.live_session.send(
                    input=types.LiveClientToolResponse(
                        function_responses=responses
                    )
                )
        except Exception as e:
            logger.error(f"Error handling tool call: {e}")

    # ------------------------------------------------------------------
    # Reconnection
    # ------------------------------------------------------------------
    async def _try_reconnect(self):
        if self.reconnect_attempts >= 3:
            logger.error("Max reconnection attempts reached")
            await self._send_ws(
                {
                    "type": "error",
                    "msg": "Connection lost. Please refresh the page.",
                }
            )
            return

        self.reconnect_attempts += 1
        logger.info(
            f"Reconnection attempt {self.reconnect_attempts}/3 for {self.session_id}"
        )
        await asyncio.sleep(2)

        # Tear down old session
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
