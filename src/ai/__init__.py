from .base import BaseAIProvider, SeriesMatchResult, EpisodeMatchResult
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from ..config import AIConfig

def get_ai_provider(config: AIConfig) -> BaseAIProvider:
    if config.provider == "gemini":
        if not config.gemini.api_key:
            raise ValueError("Gemini API key is required when ai.provider is set to 'gemini'")
        return GeminiProvider(
            api_key=config.gemini.api_key,
            model=config.gemini.model
        )
    elif config.provider == "ollama":
        return OllamaProvider(
            base_url=config.ollama.base_url,
            model=config.ollama.model
        )
    else:
        raise ValueError(f"Unsupported AI provider: {config.provider}")

__all__ = [
    "BaseAIProvider",
    "SeriesMatchResult",
    "EpisodeMatchResult",
    "GeminiProvider",
    "OllamaProvider",
    "get_ai_provider",
]
