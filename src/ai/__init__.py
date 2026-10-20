import logging
from .base import BaseAIProvider, MediaMatchResult, SeriesMatchResult, EpisodeMatchResult, AIProviderError
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .fallback import FallbackProvider
from ..config import AIConfig

logger = logging.getLogger("aimportarr.ai")

def get_ai_provider(config: AIConfig) -> BaseAIProvider:
    """
    Instantiate AI provider based on configuration.
    If fallback_enabled is True, wraps the primary provider with an automatic fallback.
    """
    if config.provider == "gemini":
        if not config.gemini.api_key:
            raise ValueError("Gemini API key is required when ai.provider is set to 'gemini'")
        
        primary = GeminiProvider(
            api_key=config.gemini.api_key,
            model=config.gemini.model
        )

        if config.fallback_enabled:
            fallback = OllamaProvider(
                base_url=config.ollama.base_url,
                model=config.ollama.model
            )
            logger.info(f"AI Provider initialized: Gemini ({config.gemini.model}) with automatic fallback to Ollama ({config.ollama.model})")
            return FallbackProvider(primary=primary, fallback=fallback)
        
        return primary

    elif config.provider == "ollama":
        primary = OllamaProvider(
            base_url=config.ollama.base_url,
            model=config.ollama.model
        )

        if config.fallback_enabled and config.gemini.api_key:
            fallback = GeminiProvider(
                api_key=config.gemini.api_key,
                model=config.gemini.model
            )
            logger.info(f"AI Provider initialized: Ollama ({config.ollama.model}) with automatic fallback to Gemini ({config.gemini.model})")
            return FallbackProvider(primary=primary, fallback=fallback)

        return primary

    else:
        raise ValueError(f"Unsupported AI provider: {config.provider}")

__all__ = [
    "BaseAIProvider",
    "MediaMatchResult",
    "SeriesMatchResult",
    "EpisodeMatchResult",
    "AIProviderError",
    "GeminiProvider",
    "OllamaProvider",
    "FallbackProvider",
    "get_ai_provider",
]
