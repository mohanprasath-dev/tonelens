import logging
from datetime import datetime, timezone

from google.cloud.firestore import AsyncClient, ArrayUnion

logger = logging.getLogger(__name__)

PROJECT_ID = "notional-cirrus-458606-e0"

_db = None


def _get_db():
    global _db
    if _db is None:
        try:
            _db = AsyncClient(project=PROJECT_ID)
        except Exception as e:
            logger.error(f"Failed to initialize Firestore client: {e}")
    return _db


async def create_session() -> str:
    db = _get_db()
    if not db:
        return ""
    try:
        doc_ref = db.collection("sessions").document()
        await doc_ref.set(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "mode": "travel",
                "exchanges": [],
            }
        )
        logger.info(f"Created session: {doc_ref.id}")
        return doc_ref.id
    except Exception as e:
        logger.error(f"Failed to create session: {e}")
        return ""


async def log_exchange(session_id: str, exchange: dict) -> None:
    db = _get_db()
    if not db:
        return
    try:
        doc_ref = db.collection("sessions").document(session_id)
        await doc_ref.update(
            {
                "exchanges": ArrayUnion(
                    [
                        {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "translation": exchange.get("translation", ""),
                            "emotion": exchange.get("emotion", ""),
                            "subtext": exchange.get("subtext", ""),
                            "suggestion": exchange.get("suggestion", ""),
                        }
                    ]
                )
            }
        )
    except Exception as e:
        logger.error(f"Failed to log exchange for session {session_id}: {e}")


async def get_session(session_id: str) -> list:
    db = _get_db()
    if not db:
        return []
    try:
        doc = await db.collection("sessions").document(session_id).get()
        if doc.exists:
            return doc.to_dict().get("exchanges", [])
        return []
    except Exception as e:
        logger.error(f"Failed to get session {session_id}: {e}")
        return []
