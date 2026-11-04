import json
from app.config import settings

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:  # The app still runs with deterministic sample outputs without the SDK.
    genai = None
    types = None
    GENAI_AVAILABLE = False

class GeminiService:
    def __init__(self):
        self.client = genai.Client(api_key=settings.google_api_key) if settings.google_api_key and GENAI_AVAILABLE else None
        self.model = settings.gemini_model

    @property
    def configured(self) -> bool:
        return self.client is not None

    def generate_text(self, prompt: str, system_instruction: str | None = None) -> str | None:
        if not self.client:
            return None
        try:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction or "You are a careful nonprofit decision-support analyst. Ground conclusions in supplied evidence. Never invent facts, deadlines, eligibility rules or sources. Distinguish observations, interpretations and unknowns. Ask a human to verify uncertainty.",
                temperature=0.2,
                max_output_tokens=settings.model_output_tokens,
            )
            response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
            return (response.text or "").strip() or None
        except Exception:
            return None

    def generate_json(self, prompt: str, fallback: dict) -> dict:
        text = self.generate_text(prompt + "\nReturn valid JSON only.")
        if not text:
            return fallback
        cleaned = text.strip()
        if cleaned.startswith("```json"): cleaned = cleaned[7:]
        elif cleaned.startswith("```"): cleaned = cleaned[3:]
        if cleaned.endswith("```"): cleaned = cleaned[:-3]
        try:
            value = json.loads(cleaned.strip())
            return value if isinstance(value, dict) else fallback
        except json.JSONDecodeError:
            return fallback

gemini = GeminiService()
