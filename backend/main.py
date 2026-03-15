import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend.gemini_bridge import GeminiBridge
from backend.session_manager import create_session, get_session

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="ToneLens", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Track active bridges for cleanup
_active_bridges: dict[str, GeminiBridge] = {}


# ------------------------------------------------------------------
# REST endpoints
# ------------------------------------------------------------------
@app.get("/")
async def landing():
    path = FRONTEND_DIR / "landing.html"
    if not path.exists():
        return JSONResponse({"error": "Frontend not found"}, status_code=404)
    return FileResponse(str(path), media_type="text/html")


@app.get("/app")
async def index():
    path = FRONTEND_DIR / "index.html"
    if not path.exists():
        return JSONResponse({"error": "Frontend not found"}, status_code=404)
    return FileResponse(str(path), media_type="text/html")


@app.get("/about")
async def about():
    path = FRONTEND_DIR / "about.html"
    if not path.exists():
        return JSONResponse({"error": "Frontend not found"}, status_code=404)
    return FileResponse(str(path), media_type="text/html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "tonelens"}


@app.get("/api/history/{session_id}")
async def history(session_id: str):
    exchanges = await get_session(session_id)
    return {"exchanges": exchanges[-10:]}


# ------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------
@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    ts = datetime.now(timezone.utc).isoformat()
    logger.info(f"[{ts}] WebSocket connected: session={session_id}")

    # Persist session to Firestore
    fs_session_id = await create_session()
    effective_id = fs_session_id or session_id
    logger.info(f"Firestore session: {effective_id}")

    bridge = GeminiBridge()
    _active_bridges[session_id] = bridge

    # ------------------------------------------------------------------
    # Audio queue — buffers chunks received before/during session
    # establishment or reconnection so they are never silently dropped.
    # Capacity: 60 chunks (~6 s at 100 ms per chunk). When full, the
    # oldest chunk is evicted to make room for the newest.
    # ------------------------------------------------------------------
    _AUDIO_QUEUE_MAX = 60
    audio_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_AUDIO_QUEUE_MAX)

    async def _audio_drain():
        """Continuously forward queued audio chunks to the live session.

        Waits (non-blocking spin) while the session is not yet ready,
        then flushes buffered chunks and keeps forwarding in real time.
        This handles both the initial 2-second setup delay and any gap
        caused by _try_reconnect() setting live_session back to None.
        """
        while True:
            chunk = await audio_queue.get()
            try:
                # Spin-wait up to 5 s for session to become ready.
                waited = 0.0
                while bridge.live_session is None and bridge._connected and waited < 5.0:
                    await asyncio.sleep(0.05)
                    waited += 0.05
                if not bridge._connected:
                    continue
                if bridge.live_session is None:
                    logger.warning(
                        "[%s] audio_drain: session still None after 5 s, dropping chunk",
                        session_id,
                    )
                    continue
                await bridge.send_audio(chunk)
                logger.info(
                    "[%s] Audio chunk forwarded to Gemini (%d b64 chars)",
                    session_id,
                    len(chunk),
                )
            except Exception as e:
                logger.warning("[%s] audio_drain error: %s", session_id, e)
            finally:
                audio_queue.task_done()

    drain_task = asyncio.create_task(_audio_drain())

    try:
        await bridge.connect(websocket, effective_id, mode="travel")

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON WebSocket message")
                continue

            msg_type = msg.get("type")

            if msg_type == "frame":
                await bridge.send_frame(msg.get("data", ""))

            elif msg_type == "audio":
                data = msg.get("data", "")
                logger.info(
                    "[%s] Audio chunk received (%d b64 chars), queue_size=%d",
                    session_id,
                    len(data),
                    audio_queue.qsize(),
                )
                if audio_queue.full():
                    # Evict oldest to avoid stale backlog
                    try:
                        audio_queue.get_nowait()
                        audio_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                await audio_queue.put(data)

            elif msg_type == "mode":
                new_mode = msg.get("mode", "travel")
                if new_mode in ("travel", "meeting", "present", "negotiate"):
                    bridge.mode = new_mode
                    logger.info(f"[{session_id}] Mode changed to {new_mode}")
                    # Reconnect with updated system prompt
                    await bridge.disconnect()
                    await bridge.connect(websocket, effective_id, mode=new_mode)

            elif msg_type == "location":
                lat = msg.get("lat", 0.0)
                lng = msg.get("lng", 0.0)
                if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                    bridge.user_lat = float(lat)
                    bridge.user_lng = float(lng)
                    logger.info(f"[{session_id}] Location updated: {lat},{lng}")
            else:
                logger.warning(f"[{session_id}] Unknown message type: {msg_type}")

    except WebSocketDisconnect:
        ts = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{ts}] WebSocket disconnected: session={session_id}")
    except Exception as e:
        ts = datetime.now(timezone.utc).isoformat()
        logger.error(f"[{ts}] WebSocket error for {session_id}: {e}")
    finally:
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        await bridge.disconnect()
        _active_bridges.pop(session_id, None)
        ts = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{ts}] Cleanup complete for session={session_id}")
