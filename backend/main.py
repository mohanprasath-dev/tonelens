import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend.gemini_bridge import GeminiBridge
from backend.session_manager import create_session

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
async def index():
    index_path = FRONTEND_DIR / "index.html"
    if not index_path.exists():
        return JSONResponse({"error": "Frontend not found"}, status_code=404)
    return FileResponse(str(index_path), media_type="text/html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "tonelens"}


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
                await bridge.send_audio(msg.get("data", ""))

            elif msg_type == "mode":
                new_mode = msg.get("mode", "travel")
                if new_mode in ("travel", "meeting", "present"):
                    bridge.mode = new_mode
                    logger.info(f"[{session_id}] Mode changed to {new_mode}")
                    # Reconnect with updated system prompt
                    await bridge.disconnect()
                    await bridge.connect(websocket, effective_id, mode=new_mode)
            else:
                logger.warning(f"[{session_id}] Unknown message type: {msg_type}")

    except WebSocketDisconnect:
        ts = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{ts}] WebSocket disconnected: session={session_id}")
    except Exception as e:
        ts = datetime.now(timezone.utc).isoformat()
        logger.error(f"[{ts}] WebSocket error for {session_id}: {e}")
    finally:
        await bridge.disconnect()
        _active_bridges.pop(session_id, None)
        ts = datetime.now(timezone.utc).isoformat()
        logger.info(f"[{ts}] Cleanup complete for session={session_id}")
