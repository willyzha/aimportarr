import logging
from .base import (
    BaseAIProvider,
    Stage1TriageResult,
    ApprovedFileItem,
    Stage2DecisionResult,
    BackfillPackMatchResult,
    AIProviderError,
)
from .gemini import GeminiProvider
from .ollama import OllamaProvider
from .fallback import FallbackProvider
from ..config import AIConfig

logger = logging.getLogger("aimportarr.ai")


def _is_valid_key(key: str) -> bool:
    return bool(key) and not key.startswith("YOUR_")


def get_ai_provider(config: AIConfig) -> BaseAIProvider:
    """
    Instantiate AI provider based on configuration.
    If fallback_enabled is True, wraps the primary provider with an automatic fallback.
    """
    if config.provider == "gemini":
        has_gemini_key = _is_valid_key(config.gemini.api_key)

        if not has_gemini_key:
            if config.fallback_enabled:
                logger.warning(
                    "Gemini API key is missing or not configured. Automatically starting with Ollama (%s) as primary provider.",
                    config.ollama.model
                )
                return OllamaProvider(
                    base_url=config.ollama.base_url,
                    model=config.ollama.model,
                    timeout=config.ollama.timeout_seconds,
                )
            raise ValueError("Gemini API key is required when ai.provider is set to 'gemini' and fallback is disabled.")

        primary = GeminiProvider(
            api_key=config.gemini.api_key,
            model=config.gemini.model
        )

        if config.fallback_enabled:
            fallback = OllamaProvider(
                base_url=config.ollama.base_url,
                model=config.ollama.model,
                timeout=config.ollama.timeout_seconds,
            )
            logger.info(f"AI Provider initialized: Gemini ({config.gemini.model}) with automatic fallback to Ollama ({config.ollama.model})")
            return FallbackProvider(primary=primary, fallback=fallback)

        return primary

    elif config.provider == "ollama":
        primary = OllamaProvider(
            base_url=config.ollama.base_url,
            model=config.ollama.model,
            timeout=config.ollama.timeout_seconds,
        )

        has_gemini_key = _is_valid_key(config.gemini.api_key)
        if config.fallback_enabled and has_gemini_key:
            fallback = GeminiProvider(
                api_key=config.gemini.api_key,
                model=config.gemini.model
            )
            logger.info(f"AI Provider initialized: Ollama ({config.ollama.model}) with automatic fallback to Gemini ({config.gemini.model})")
            return FallbackProvider(primary=primary, fallback=fallback)

        logger.info(f"AI Provider initialized: Ollama ({config.ollama.model})")
        return primary

    else:
        raise ValueError(f"Unsupported AI provider: {config.provider}")


__all__ = [
    "BaseAIProvider",
    "Stage1TriageResult",
    "ApprovedFileItem",
    "Stage2DecisionResult",
    "BackfillPackMatchResult",
    "AIProviderError",
    "GeminiProvider",
    "OllamaProvider",
    "FallbackProvider",
    "get_ai_provider",
]
