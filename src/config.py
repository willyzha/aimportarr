import os
import yaml
from pathlib import Path
from typing import Optional, Literal
from pydantic import BaseModel, Field

class GeneralConfig(BaseModel):
    dry_run: Optional[bool] = None  # Deprecated: dry_run is now configured per category
    check_interval_seconds: int = 120
    min_confidence: float = 0.85
    log_level: str = "INFO"

class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "auto"

class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:1.5b"

class AIConfig(BaseModel):
    provider: Literal["gemini", "ollama"] = "gemini"
    fallback_enabled: bool = True
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)

class SonarrConfig(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = True
    url: str = "http://localhost:8989"
    api_key: str = ""

class RadarrConfig(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = False
    url: str = "http://localhost:7878"
    api_key: str = ""

class CategoryRule(BaseModel):
    enabled: bool = True
    dry_run: bool = True  # Defaults to True (safe simulation) if omitted

class CategoriesConfig(BaseModel):
    fake_or_malware: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=False))
    id_match: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=False))
    unknown_media: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=True))
    unknown_episode: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=True))
    sample_or_extra: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=True))
    quality_or_language: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=True))
    missing_episodes: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=False))
    other: CategoryRule = Field(default_factory=lambda: CategoryRule(enabled=True, dry_run=True))  # Fallback category

    def is_dry_run(self, category: str, default_dry_run: Optional[bool] = None) -> bool:
        rule = getattr(self, category, None)
        if rule and rule.dry_run is not None:
            return rule.dry_run
        if default_dry_run is not None:
            return default_dry_run
        return True

    def is_enabled(self, category: str) -> bool:
        rule = getattr(self, category, None)
        if rule is not None:
            return rule.enabled
        return True

class ExtractionConfig(BaseModel):
    enabled: bool = True
    delete_after_import: bool = True
    staging_suffix: str = "_extracted"

class PathMapping(BaseModel):
    model_config = {"populate_by_name": True}
    from_path: str = Field(alias="from")
    to_path: str = Field(alias="to")

class AppConfig(BaseModel):
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    sonarr: SonarrConfig = Field(default_factory=SonarrConfig)
    radarr: RadarrConfig = Field(default_factory=RadarrConfig)
    categories: CategoriesConfig = Field(default_factory=CategoriesConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    path_mappings: List[PathMapping] = Field(default_factory=list)

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

    # Backward compatibility: migrate legacy auto_approve / auto_resolve flags if categories not explicitly defined
    sonarr_data = data.get("sonarr", {}) if isinstance(data, dict) else {}
    radarr_data = data.get("radarr", {}) if isinstance(data, dict) else {}
    cats_data = data.get("categories", {}) if isinstance(data, dict) else {}

    if "auto_approve_id_matches" in sonarr_data and "id_match" not in cats_data:
        config.categories.id_match.enabled = bool(sonarr_data["auto_approve_id_matches"])
    elif "auto_approve_id_matches" in radarr_data and "id_match" not in cats_data:
        config.categories.id_match.enabled = bool(radarr_data["auto_approve_id_matches"])

    if "auto_resolve_unknown_episodes" in sonarr_data and "unknown_episode" not in cats_data:
        config.categories.unknown_episode.enabled = bool(sonarr_data["auto_resolve_unknown_episodes"])

    # General overrides
    if env_dry_run := os.getenv("DRY_RUN"):
        is_dry = env_dry_run.lower() in ("true", "1", "yes")
        config.general.dry_run = is_dry
        for cat_name in ["id_match", "unknown_media", "unknown_episode", "sample_or_extra", "quality_or_language", "missing_episodes", "other"]:
            getattr(config.categories, cat_name).dry_run = is_dry
    elif config.general.dry_run is not None:
        # Legacy support: if general.dry_run is set in YAML, apply to categories without explicit dry_run
        for cat_name in ["id_match", "unknown_media", "unknown_episode", "sample_or_extra", "quality_or_language", "missing_episodes", "other"]:
            rule_data = cats_data.get(cat_name)
            if not isinstance(rule_data, dict) or "dry_run" not in rule_data:
                getattr(config.categories, cat_name).dry_run = config.general.dry_run
    
    # AI overrides
    if env_provider := os.getenv("AI_PROVIDER"):
        if env_provider in ("gemini", "ollama"):
            config.ai.provider = env_provider
    if env_fallback := os.getenv("AI_FALLBACK_ENABLED"):
        config.ai.fallback_enabled = env_fallback.lower() in ("true", "1", "yes")
    if env_gemini_key := os.getenv("GEMINI_API_KEY"):
        config.ai.gemini.api_key = env_gemini_key
    if env_gemini_model := os.getenv("GEMINI_MODEL"):
        config.ai.gemini.model = env_gemini_model.strip()
    if not config.ai.gemini.model:
        config.ai.gemini.model = "auto"
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

    # Category overrides from environment variables
    # e.g. CATEGORY_ID_MATCH_DRY_RUN=false, CATEGORY_UNKNOWN_MEDIA_DRY_RUN=true, CATEGORY_OTHER_DRY_RUN=false
    for cat_name in ["fake_or_malware", "id_match", "unknown_media", "unknown_episode", "sample_or_extra", "quality_or_language", "missing_episodes", "other"]:
        rule = getattr(config.categories, cat_name, None)
        if not rule:
            continue
        prefix = f"CATEGORY_{cat_name.upper()}"
        if env_dry := os.getenv(f"{prefix}_DRY_RUN"):
            rule.dry_run = env_dry.lower() in ("true", "1", "yes")
        if env_enabled := os.getenv(f"{prefix}_ENABLED"):
            rule.enabled = env_enabled.lower() in ("true", "1", "yes")

    # Extraction overrides
    if env_extraction_enabled := os.getenv("EXTRACTION_ENABLED"):
        config.extraction.enabled = env_extraction_enabled.lower() in ("true", "1", "yes")
    if env_delete_after_import := os.getenv("EXTRACTION_DELETE_AFTER_IMPORT"):
        config.extraction.delete_after_import = env_delete_after_import.lower() in ("true", "1", "yes")

    # Path mappings override from environment variable (e.g. PATH_MAPPINGS="/downloads:/data/downloads,/remote:/local")
    if env_path_mappings := os.getenv("PATH_MAPPINGS"):
        mappings = []
        for pair in env_path_mappings.split(","):
            if ":" in pair:
                f, _, t = pair.partition(":")
                mappings.append(PathMapping(**{"from": f.strip(), "to": t.strip()}))
        if mappings:
            config.path_mappings = mappings

    # Normalize URLs (strip trailing slashes)
    config.sonarr.url = config.sonarr.url.rstrip("/")
    config.radarr.url = config.radarr.url.rstrip("/")
    config.ai.ollama.base_url = config.ai.ollama.base_url.rstrip("/")

    return config
