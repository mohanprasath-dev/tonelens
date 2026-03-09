import asyncio
import logging

from google.adk.agents import Agent

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are ToneLens, a real-time emotional intelligence agent.\n"
    "You simultaneously see through the user's camera and hear "
    "what people around them are saying.\n\n"
    "For every utterance you detect, respond in this EXACT format:\n\n"
    "TRANSLATION: [If not English, translate to English. "
    "If English, write 'English detected']\n"
    "EMOTION: [Single word: nervous/confident/frustrated/happy/"
    "uncertain/deceptive/calm/excited] - [XX]% confidence\n"
    "SUBTEXT: [One sentence: what they are actually meaning]\n"
    "SUGGEST: [One sentence: how the user should respond, "
    "tailored to their current mode]\n\n"
    "Rules:\n"
    "- Never exceed 1 sentence per section\n"
    "- Respond within 2 seconds\n"
    "- Be direct, not verbose\n"
    "- Consider cultural context in your subtext\n"
    "- Tailor suggestions to mode: "
    "travel=be helpful and warm, "
    "meeting=be strategic and professional, "
    "present=focus on audience engagement\n\n"
    "Supported languages: English, French, Spanish, Japanese, "
    "Hindi, Tamil, Mandarin, Arabic, Portuguese, German, "
    "Korean, Italian, Russian, Dutch"
)


# ---------------------------------------------------------------------------
# Tool 1: analyze_emotion
# ---------------------------------------------------------------------------
def analyze_emotion(face_description: str, voice_tone: str) -> dict:
    """Analyze emotional state from facial expression and voice tone.

    Args:
        face_description: Description of the person's facial expression and body language.
        voice_tone: Description of the person's voice tone, pitch, and speaking pattern.

    Returns:
        Dictionary with emotion label, confidence score, and observed signals.
    """
    signals: list[str] = []

    face_lower = face_description.lower()
    voice_lower = voice_tone.lower()

    facial_map = {
        "frown": "furrowed brow",
        "smile": "genuine smile",
        "wide eyes": "widened eyes",
        "narrow": "narrowed eyes",
        "lip": "lip tension",
        "jaw": "jaw clenching",
        "blink": "rapid blinking",
        "eyebrow": "eyebrow movement",
        "avoid": "avoiding eye contact",
        "look away": "gaze avoidance",
        "nod": "head nodding",
        "shake": "head shaking",
        "sweat": "visible perspiration",
        "red": "facial flushing",
        "pale": "facial pallor",
        "tense": "facial tension",
        "relax": "relaxed expression",
    }
    for kw, sig in facial_map.items():
        if kw in face_lower:
            signals.append(sig)

    voice_map = {
        "shak": "voice trembling",
        "loud": "raised volume",
        "quiet": "lowered volume",
        "fast": "rapid speech",
        "slow": "deliberate pacing",
        "high": "elevated pitch",
        "low": "lowered pitch",
        "monotone": "flat intonation",
        "crack": "voice cracking",
        "stutter": "speech hesitation",
        "pause": "frequent pauses",
        "clear": "clear articulation",
        "mumbl": "mumbling",
        "firm": "firm tone",
        "soft": "soft tone",
    }
    for kw, sig in voice_map.items():
        if kw in voice_lower:
            signals.append(sig)

    emotion_indicators = {
        "nervous": ["trembling", "hesitation", "rapid", "perspiration", "blinking", "pallor"],
        "confident": ["firm", "clear", "genuine smile", "relaxed", "deliberate"],
        "frustrated": ["clenching", "furrowed", "raised volume", "tension", "narrowed"],
        "happy": ["smile", "elevated pitch", "nodding", "relaxed"],
        "uncertain": ["pauses", "mumbling", "avoidance", "gaze", "hesitation"],
        "deceptive": ["avoidance", "gaze", "blinking", "hesitation", "tension"],
        "calm": ["relaxed", "deliberate", "soft", "clear", "lowered"],
        "excited": ["raised volume", "rapid", "elevated pitch", "widened", "smile"],
    }

    best_emotion = "calm"
    best_score = 0
    for emo, indicators in emotion_indicators.items():
        score = sum(1 for ind in indicators if any(ind in s for s in signals))
        if score > best_score:
            best_score = score
            best_emotion = emo

    confidence = min(0.95, 0.5 + (best_score * 0.1)) if signals else 0.7

    return {
        "emotion": best_emotion,
        "confidence": round(confidence, 2),
        "signals": signals[:5],
    }


# ---------------------------------------------------------------------------
# Tool 2: get_cultural_context
# ---------------------------------------------------------------------------
def get_cultural_context(text: str, language: str, situation: str) -> dict:
    """Provide cultural context for a phrase in a specific language and situation.

    Args:
        text: The phrase or expression to analyze.
        language: The source language of the text.
        situation: The context in which the phrase was used.

    Returns:
        Dictionary with literal_meaning, actual_meaning, and cultural_note.
    """
    return {
        "literal_meaning": text,
        "actual_meaning": (
            f"In the context of {situation}, this {language} expression "
            "conveys a nuanced meaning beyond its literal translation."
        ),
        "cultural_note": (
            f"In {language}-speaking cultures, this type of expression is "
            f"commonly used in {situation} situations and may carry implicit "
            "social expectations."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 3: suggest_response
# ---------------------------------------------------------------------------
def suggest_response(emotion: str, words: str, mode: str) -> str:
    """Generate a mode-appropriate response suggestion.

    Args:
        emotion: The detected emotion (nervous/confident/frustrated/happy/uncertain/deceptive/calm/excited).
        words: What the speaker said.
        mode: The user's current mode (travel/meeting/present).

    Returns:
        A one-sentence response suggestion.
    """
    strategies = {
        "travel": {
            "nervous": "Smile warmly and speak slowly to help them feel comfortable communicating with you.",
            "confident": "Match their energy with a friendly and open response to build rapport.",
            "frustrated": "Acknowledge their frustration with empathy and ask how you can help.",
            "happy": "Share in their enthusiasm and use this positive moment to connect.",
            "uncertain": "Offer gentle reassurance and clarify your needs simply.",
            "deceptive": "Stay polite but verify key details independently before proceeding.",
            "calm": "Respond naturally and take the opportunity to ask for local recommendations.",
            "excited": "Show genuine interest in what excites them to deepen the cultural exchange.",
        },
        "meeting": {
            "nervous": "Create psychological safety by acknowledging their point before adding yours.",
            "confident": "Build on their momentum with a strategic follow-up question.",
            "frustrated": "Redirect to common ground and propose a concrete next step.",
            "happy": "Leverage this positive energy to advance your key agenda item.",
            "uncertain": "Provide clear data points to help them reach a decision.",
            "deceptive": "Ask for specific examples or data to ground the conversation in facts.",
            "calm": "Present your strongest argument while the atmosphere is neutral.",
            "excited": "Channel their enthusiasm toward actionable commitments.",
        },
        "present": {
            "nervous": "Use an inclusive phrase to bring them into the discussion and ease their tension.",
            "confident": "Engage them as an ally by asking them to elaborate on their point.",
            "frustrated": "Pause and address their concern directly to keep the audience engaged.",
            "happy": "Amplify their energy with an interactive moment for the whole audience.",
            "uncertain": "Offer a clear example or analogy to address their confusion.",
            "deceptive": "Tactfully redirect the conversation back to verifiable points.",
            "calm": "Introduce a thought-provoking question to elevate audience engagement.",
            "excited": "Build on their excitement to create a memorable peak moment in your presentation.",
        },
    }
    mode_map = strategies.get(mode, strategies["travel"])
    return mode_map.get(
        emotion,
        f"Listen attentively and respond thoughtfully to what they said.",
    )


# ---------------------------------------------------------------------------
# Tool 4: log_exchange
# ---------------------------------------------------------------------------
async def log_exchange_tool(session_id: str, data: dict) -> bool:
    """Log a conversation exchange to the Firestore session store.

    Args:
        session_id: The session identifier.
        data: Dictionary containing translation, emotion, subtext, and suggestion.

    Returns:
        True on success, False on failure.
    """
    try:
        from backend import session_manager

        await session_manager.log_exchange(session_id, data)
        return True
    except Exception as e:
        logger.error(f"Failed to log exchange: {e}")
        return False


# ---------------------------------------------------------------------------
# Function registry (used by GeminiBridge for Live API tool calls)
# ---------------------------------------------------------------------------
TOOL_FUNCTIONS = {
    "analyze_emotion": analyze_emotion,
    "get_cultural_context": get_cultural_context,
    "suggest_response": suggest_response,
    "log_exchange": log_exchange_tool,
}


# ---------------------------------------------------------------------------
# ADK Agent definition
# ---------------------------------------------------------------------------
tonelens_agent = Agent(
    name="ToneLens",
    model="gemini-2.0-flash-live-001",
    instruction=SYSTEM_PROMPT,
    tools=[analyze_emotion, get_cultural_context, suggest_response, log_exchange_tool],
)
