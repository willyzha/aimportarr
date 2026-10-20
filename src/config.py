import os
import yaml
from pathlib import Path
from typing import Optional, Literal
from pydantic import BaseModel, Field

class GeneralConfig(BaseModel):
    dry_run: bool = False
    check_interval_seconds: int = 120
    min_confidence: float = 0.85
    log_level: str = "INFO"

class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "gemini-2.0-flash"

class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:3b"

class AIConfig(BaseModel):
    provider: Literal["gemini", "ollama"] = "gemini"
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)

class SonarrConfig(BaseModel):
    enabled: bool = True
    url: str = "http://localhost:8989"
    api_key: str = ""
    auto_approve_id_matches: bool = True
    auto_resolve_unknown_episodes: bool = True

class RadarrConfig(BaseModel):
    enabled: bool = False
    url: str = "http://localhost:7878"
    api_key: str = ""
    auto_approve_id_matches: bool = True

class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    sonarr: SonarrConfig = Field(default_factory=SonarrConfig)
    radarr: RadarrConfig = Field(default_factory=RadarrConfig)

def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file and apply environment variable overrides."""
    candidates = [
        config_path,
        os.getenv("AIMPORTARR_CONFIG"),
        "/config/config.yml",
        "/config/config.yaml",
        "config.yml",
        "config.yaml",
    ]
    
    selected_path = None
    for p in candidates:
        if p and Path(p).is_file():
            selected_path = Path(p)
            break

    data = {}
    if selected_path:
        with open(selected_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig(**data)

    # General overrides
    if env_dry_run := os.getenv("DRY_RUN"):
        config.general.dry_run = env_dry_run.lower() in ("true", "1", "yes")
    
    # AI overrides
    if env_provider := os.getenv("AI_PROVIDER"):
        if env_provider in ("gemini", "ollama"):
            config.ai.provider = env_provider
    if env_gemini_key := os.getenv("GEMINI_API_KEY"):
        config.ai.gemini.api_key = env_gemini_key
    if env_gemini_model := os.getenv("GEMINI_MODEL"):
        config.ai.gemini.model = env_gemini_model
    if env_ollama_url := os.getenv("OLLAMA_BASE_URL"):
        config.ai.ollama.base_url = env_ollama_url
    if env_ollama_model := os.getenv("OLLAMA_MODEL"):
        config.ai.ollama.model = env_ollama_model

    # Sonarr overrides
    if env_sonarr_enabled := os.getenv("SONARR_ENABLED"):
        config.sonarr.enabled = env_sonarr_enabled.lower() in ("true", "1", "yes")
    if env_sonarr_url := os.getenv("SONARR_URL"):
        config.sonarr.url = env_sonarr_url
    if env_sonarr_key := os.getenv("SONARR_API_KEY"):
        config.sonarr.api_key = env_sonarr_key

    # Radarr overrides
    if env_radarr_enabled := os.getenv("RADARR_ENABLED"):
        config.radarr.enabled = env_radarr_enabled.lower() in ("true", "1", "yes")
    if env_radarr_url := os.getenv("RADARR_URL"):
        config.radarr.url = env_radarr_url
    if env_radarr_key := os.getenv("RADARR_API_KEY"):
        config.radarr.api_key = env_radarr_key

    # Normalize URLs (strip trailing slashes)
    config.sonarr.url = config.sonarr.url.rstrip("/")
    config.radarr.url = config.radarr.url.rstrip("/")
    config.ai.ollama.base_url = config.ai.ollama.base_url.rstrip("/")

    return config
