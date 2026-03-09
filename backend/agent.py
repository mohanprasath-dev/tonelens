import json
import logging
import os
import re
from datetime import datetime, timezone

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "notional-cirrus-458606-e0")
REGION = "us-central1"


def _vertex_client() -> genai.Client:
    return genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location=REGION,
    )


# ---------------------------------------------------------------------------
# Tool 1: search_cultural_context
# ---------------------------------------------------------------------------
async def search_cultural_context(language: str, phrase: str) -> dict:
    """Use Vertex AI Gemini to generate a cultural context insight for a phrase.

    Args:
        language: The language of the phrase (e.g. French, Japanese, Hindi).
        phrase: The non-English phrase or expression to look up.

    Returns:
        Dict with tip, do, avoid, and language keys.
    """
    try:
        client = _vertex_client()
        prompt = (
            f"You are a cultural intelligence expert.\n"
            f'For the {language} phrase: "{phrase}"\n'
            f"Respond ONLY as a valid JSON object with exactly these four string keys:\n"
            f'{{"tip": "one sentence cultural insight", '
            f'"do": "one thing to do", '
            f'"avoid": "one thing to avoid", '
            f'"language": "{language}"}}'
        )
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )
        text = (response.text or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        logger.warning(f"search_cultural_context failed: {e}")

    return {
        "tip": f"This phrase carries cultural nuance in {language}-speaking communities.",
        "do": "Listen attentively and respond with respect.",
        "avoid": "Avoid assuming the literal translation captures the full meaning.",
        "language": language,
    }


# ---------------------------------------------------------------------------
# Tool 2: find_emergency_services
# ---------------------------------------------------------------------------
def find_emergency_services(
    situation: str, latitude: float, longitude: float
) -> dict:
    """Return emergency service map links for the user's current location.

    Args:
        situation: Brief description of the emergency situation.
        latitude: User's current latitude coordinate.
        longitude: User's current longitude coordinate.

    Returns:
        Dict with message, map URLs, emergency number, and situation.
    """
    return {
        "message": "Emergency services information",
        "maps_hospital": (
            f"https://www.google.com/maps/search/hospital/@{latitude},{longitude},15z"
        ),
        "maps_police": (
            f"https://www.google.com/maps/search/police/@{latitude},{longitude},15z"
        ),
        "emergency_number": "112",
        "situation": situation,
    }


# ---------------------------------------------------------------------------
# Tool 3: save_meeting_note
# ---------------------------------------------------------------------------
async def save_meeting_note(
    session_id: str, key_point: str, speaker_emotion: str
) -> dict:
    """Save an important meeting note to Firestore.

    Args:
        session_id: The current session identifier.
        key_point: The important point, decision, or action item to save.
        speaker_emotion: The speaker's current emotional state.

    Returns:
        Dict with saved status, key_point, and note_id.
    """
    try:
        from google.cloud import firestore  # type: ignore

        db = firestore.AsyncClient()
        ref = (
            db.collection("sessions")
            .document(session_id)
            .collection("notes")
            .document()
        )
        await ref.set(
            {
                "key_point": key_point,
                "speaker_emotion": speaker_emotion,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        return {"saved": True, "key_point": key_point, "note_id": ref.id}
    except Exception as e:
        logger.warning(f"save_meeting_note failed: {e}")
        return {"saved": False, "key_point": key_point, "note_id": ""}


# ---------------------------------------------------------------------------
# Tool 4: get_stress_report
# ---------------------------------------------------------------------------
async def get_stress_report(session_id: str) -> dict:
    """Query Firestore for recent exchanges and return a stress analysis.

    Args:
        session_id: The current session identifier.

    Returns:
        Dict with average_stress, dominant_emotion, recommendation, total_exchanges.
    """
    stress_map = {
        "angry": 1.0,
        "frustrated": 0.9,
        "nervous": 0.75,
        "uncertain": 0.6,
        "excited": 0.4,
        "calm": 0.2,
        "confident": 0.15,
        "happy": 0.1,
    }
    try:
        from collections import Counter

        from google.cloud import firestore  # type: ignore

        db = firestore.AsyncClient()
        docs = (
            await db.collection("sessions")
            .document(session_id)
            .collection("exchanges")
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(20)
            .get()
        )
        emotions: list[str] = [doc.to_dict().get("emotion", "calm") for doc in docs]

        if not emotions:
            return {
                "average_stress": 0.2,
                "dominant_emotion": "calm",
                "recommendation": "No exchanges recorded yet.",
                "total_exchanges": 0,
            }

        stress_values = [stress_map.get(e, 0.5) for e in emotions]
        avg = sum(stress_values) / len(stress_values)
        dominant = Counter(emotions).most_common(1)[0][0]

        if avg > 0.7:
            rec = "High stress detected. Consider pausing for a brief break and breathing deeply."
        elif avg > 0.4:
            rec = "Moderate stress — stay grounded and maintain steady breathing."
        else:
            rec = "Stress is low. You are managing the conversation well."

        return {
            "average_stress": round(avg, 2),
            "dominant_emotion": dominant,
            "recommendation": rec,
            "total_exchanges": len(emotions),
        }
    except Exception as e:
        logger.warning(f"get_stress_report failed: {e}")
        return {
            "average_stress": 0.5,
            "dominant_emotion": "unknown",
            "recommendation": "Unable to retrieve stress data at this time.",
            "total_exchanges": 0,
        }


# ---------------------------------------------------------------------------
# Exports used by GeminiBridge
# ---------------------------------------------------------------------------
TOOL_FUNCTIONS: dict = {
    "search_cultural_context": search_cultural_context,
    "find_emergency_services": find_emergency_services,
    "save_meeting_note": save_meeting_note,
    "get_stress_report": get_stress_report,
}

FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_cultural_context",
        description=(
            "Search for cultural context and tips for a phrase in a given language. "
            "Call this IMMEDIATELY when non-English speech is detected, before responding."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "language": {
                    "type": "STRING",
                    "description": "The language of the phrase (e.g. French, Japanese, Hindi)",
                },
                "phrase": {
                    "type": "STRING",
                    "description": "The non-English phrase or expression to look up",
                },
            },
            "required": ["language", "phrase"],
        },
    ),
    types.FunctionDeclaration(
        name="find_emergency_services",
        description=(
            "Find nearby hospitals and police stations. Call this immediately when you "
            "detect fear, danger, distress, or words like help, emergency, hurt, scared, or danger."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "situation": {
                    "type": "STRING",
                    "description": "Brief description of the emergency situation",
                },
                "latitude": {
                    "type": "NUMBER",
                    "description": "User's current latitude coordinate",
                },
                "longitude": {
                    "type": "NUMBER",
                    "description": "User's current longitude coordinate",
                },
            },
            "required": ["situation", "latitude", "longitude"],
        },
    ),
    types.FunctionDeclaration(
        name="save_meeting_note",
        description=(
            "Save an important meeting note to the session. Call this in meeting mode "
            "when the speaker states a decision, action item, or key fact."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "session_id": {
                    "type": "STRING",
                    "description": "The current session identifier",
                },
                "key_point": {
                    "type": "STRING",
                    "description": "The important point, decision, or action item to save",
                },
                "speaker_emotion": {
                    "type": "STRING",
                    "description": "The speaker's current emotional state",
                },
            },
            "required": ["session_id", "key_point", "speaker_emotion"],
        },
    ),
    types.FunctionDeclaration(
        name="get_stress_report",
        description=(
            "Get a stress analysis report for the current session. Call this when the "
            "user asks how they are doing, or proactively after 10 or more exchanges."
        ),
        parameters={
            "type": "OBJECT",
            "properties": {
                "session_id": {
                    "type": "STRING",
                    "description": "The current session identifier",
                },
            },
            "required": ["session_id"],
        },
    ),
]
