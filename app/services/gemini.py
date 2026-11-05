from functools import lru_cache
from typing import Any

from google import genai

from app.config import settings


class GeminiNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_gemini_client():
    if not settings.gemini_api_key:
        raise GeminiNotConfigured("GEMINI_API_KEY is not configured.")
    return genai.Client(api_key=settings.gemini_api_key)


def generate_text(prompt: str, *, system_instruction: str | None = None) -> str:
    """Generate a draft with Gemini; callers must validate and route it for review."""
    client = get_gemini_client()
    config: dict[str, Any] = {}
    if system_instruction:
        config["system_instruction"] = system_instruction
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=config or None,
    )
    return response.text or ""
