import os
import yaml
from pathlib import Path
from typing import Optional, Literal, List, Dict, Any
from pydantic import BaseModel, Field

from .cron import interval_hours_to_cron, normalize_cron_expr

class GeneralConfig(BaseModel):
    dry_run: bool = False
    check_interval_seconds: int = 30
    ai_cooldown_seconds: float = 4.0
    min_confidence: float = 0.85
    reject_on_low_confidence: bool = False
    log_level: str = "INFO"

class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "auto"

class OllamaConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b"

class AIConfig(BaseModel):
    provider: Literal["gemini", "ollama"] = "gemini"
    fallback_enabled: bool = True
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)

class SeasonPackBackfillConfig(BaseModel):
    enabled: bool = False
    cron: str = "0 2 * * *"
    interval_hours: Optional[int] = None
    min_missing_days: int = 3
    max_seasons_per_run: int = 3
    season_cooldown_days: int = 7

    def __init__(self, **kwargs):
        if "cron" not in kwargs and "interval_hours" in kwargs and kwargs["interval_hours"] is not None:
            kwargs["cron"] = interval_hours_to_cron(kwargs["interval_hours"])
        elif "cron" in kwargs and kwargs["cron"]:
            kwargs["cron"] = normalize_cron_expr(str(kwargs["cron"]), fallback="0 2 * * *")
        super().__init__(**kwargs)


class MissingEpisodeRetryConfig(BaseModel):
    enabled: bool = False
    cron: str = "0 */6 * * *"
    interval_hours: Optional[int] = None
    min_air_delay_hours: int = 6
    max_episodes_per_run: int = 5
    episode_cooldown_hours: int = 24

    def __init__(self, **kwargs):
        if "cron" not in kwargs and "interval_hours" in kwargs and kwargs["interval_hours"] is not None:
            kwargs["cron"] = interval_hours_to_cron(kwargs["interval_hours"])
        elif "cron" in kwargs and kwargs["cron"]:
            kwargs["cron"] = normalize_cron_expr(str(kwargs["cron"]), fallback="0 */6 * * *")
        super().__init__(**kwargs)


class MissingMovieSearchConfig(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = False
    cron: str = "0 3 * * *"
    min_release_delay_days: int = 1
    max_movies_per_run: int = 5
    movie_cooldown_days: int = 7

    def __init__(self, **kwargs):
        if "cron" not in kwargs and "interval_hours" in kwargs and kwargs["interval_hours"] is not None:
            kwargs["cron"] = interval_hours_to_cron(kwargs["interval_hours"])
        elif "cron" in kwargs and kwargs["cron"]:
            kwargs["cron"] = normalize_cron_expr(str(kwargs["cron"]), fallback="0 3 * * *")
        super().__init__(**kwargs)


class SonarrConfig(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = True
    url: str = "http://localhost:8989"
    api_key: str = ""
    season_pack_backfill: SeasonPackBackfillConfig = Field(default_factory=SeasonPackBackfillConfig)
    missing_episode_retry: MissingEpisodeRetryConfig = Field(default_factory=MissingEpisodeRetryConfig)

class RadarrConfig(BaseModel):
    model_config = {"extra": "ignore"}
    enabled: bool = False
    url: str = "http://localhost:7878"
    api_key: str = ""
    missing_movie_search: MissingMovieSearchConfig = Field(default_factory=MissingMovieSearchConfig)

class ExtractionConfig(BaseModel):
    enabled: bool = True
    delete_after_import: bool = True
    staging_suffix: str = "_extracted"

class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9890

class DatabaseConfig(BaseModel):
    path: str = "/config/aimportarr.db"
    retention_days: int = 30

class PathMapping(BaseModel):
    model_config = {"populate_by_name": True}
    from_path: str = Field(default="", alias="from")
    to_path: str = Field(default="", alias="to")

    def __init__(self, **kwargs):
        if "from" in kwargs and "from_path" not in kwargs:
            kwargs["from_path"] = kwargs.pop("from")
        if "to" in kwargs and "to_path" not in kwargs:
            kwargs["to_path"] = kwargs.pop("to")
        super().__init__(**kwargs)

class AppConfig(BaseModel):
    model_config = {"extra": "ignore"}
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    sonarr: SonarrConfig = Field(default_factory=SonarrConfig)
    radarr: RadarrConfig = Field(default_factory=RadarrConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    path_mappings: List[PathMapping] = Field(default_factory=list)

    def __init__(self, **kwargs):
        # Ensure nested dicts instantiate proper sub-models even in minimal test environments
        if isinstance(kwargs.get("general"), dict):
            kwargs["general"] = GeneralConfig(**kwargs["general"])
        if isinstance(kwargs.get("ai"), dict):
            ai_d = dict(kwargs["ai"])
            if isinstance(ai_d.get("gemini"), dict):
                ai_d["gemini"] = GeminiConfig(**ai_d["gemini"])
            if isinstance(ai_d.get("ollama"), dict):
                ai_d["ollama"] = OllamaConfig(**ai_d["ollama"])
            kwargs["ai"] = AIConfig(**ai_d)
        if isinstance(kwargs.get("sonarr"), dict):
            s_d = dict(kwargs["sonarr"])
            if isinstance(s_d.get("season_pack_backfill"), dict):
                s_d["season_pack_backfill"] = SeasonPackBackfillConfig(**s_d["season_pack_backfill"])
            if isinstance(s_d.get("missing_episode_retry"), dict):
                s_d["missing_episode_retry"] = MissingEpisodeRetryConfig(**s_d["missing_episode_retry"])
            kwargs["sonarr"] = SonarrConfig(**s_d)
        if isinstance(kwargs.get("radarr"), dict):
            r_d = dict(kwargs["radarr"])
            if isinstance(r_d.get("missing_movie_search"), dict):
                r_d["missing_movie_search"] = MissingMovieSearchConfig(**r_d["missing_movie_search"])
            kwargs["radarr"] = RadarrConfig(**r_d)
        if isinstance(kwargs.get("extraction"), dict):
            kwargs["extraction"] = ExtractionConfig(**kwargs["extraction"])
        if isinstance(kwargs.get("web"), dict):
            kwargs["web"] = WebConfig(**kwargs["web"])
        if isinstance(kwargs.get("database"), dict):
            kwargs["database"] = DatabaseConfig(**kwargs["database"])
        if isinstance(kwargs.get("path_mappings"), list):
            kwargs["path_mappings"] = [
                PathMapping(**m) if isinstance(m, dict) else m
                for m in kwargs["path_mappings"]
            ]
        kwargs.pop("categories", None)
        super().__init__(**kwargs)


def resolve_config_path(config_path: Optional[str] = None) -> Path:
    """Resolve the active configuration file path for reading or saving."""
    candidates = [
        config_path,
        os.getenv("AIMPORTARR_CONFIG"),
        "/config/config.yml",
        "/config/config.yaml",
        "config.yml",
        "config.yaml",
    ]
    for p in candidates:
        if p and Path(p).is_file():
            return Path(p)

    if config_path:
        return Path(config_path)
    if env_p := os.getenv("AIMPORTARR_CONFIG"):
        return Path(env_p)
    if Path("/config").is_dir():
        return Path("/config/config.yml")
    return Path("config.yml")


def config_to_dict(config: AppConfig) -> Dict[str, Any]:
    """Serialize AppConfig into a clean dictionary suitable for YAML or JSON API."""
    return {
        "general": {
            "dry_run": bool(config.general.dry_run),
            "check_interval_seconds": int(config.general.check_interval_seconds),
            "ai_cooldown_seconds": float(config.general.ai_cooldown_seconds),
            "min_confidence": float(config.general.min_confidence),
            "reject_on_low_confidence": bool(config.general.reject_on_low_confidence),
            "log_level": str(config.general.log_level),
        },
        "ai": {
            "provider": str(config.ai.provider),
            "fallback_enabled": bool(config.ai.fallback_enabled),
            "gemini": {
                "api_key": str(config.ai.gemini.api_key),
                "model": str(config.ai.gemini.model),
            },
            "ollama": {
                "base_url": str(config.ai.ollama.base_url),
                "model": str(config.ai.ollama.model),
            },
        },
        "sonarr": {
            "enabled": bool(config.sonarr.enabled),
            "url": str(config.sonarr.url),
            "api_key": str(config.sonarr.api_key),
            "season_pack_backfill": {
                "enabled": bool(config.sonarr.season_pack_backfill.enabled),
                "cron": str(config.sonarr.season_pack_backfill.cron),
                "min_missing_days": int(config.sonarr.season_pack_backfill.min_missing_days),
                "max_seasons_per_run": int(config.sonarr.season_pack_backfill.max_seasons_per_run),
                "season_cooldown_days": int(config.sonarr.season_pack_backfill.season_cooldown_days),
            },
            "missing_episode_retry": {
                "enabled": bool(config.sonarr.missing_episode_retry.enabled),
                "cron": str(config.sonarr.missing_episode_retry.cron),
                "min_air_delay_hours": int(config.sonarr.missing_episode_retry.min_air_delay_hours),
                "max_episodes_per_run": int(config.sonarr.missing_episode_retry.max_episodes_per_run),
                "episode_cooldown_hours": int(config.sonarr.missing_episode_retry.episode_cooldown_hours),
            },
        },
        "radarr": {
            "enabled": bool(config.radarr.enabled),
            "url": str(config.radarr.url),
            "api_key": str(config.radarr.api_key),
            "missing_movie_search": {
                "enabled": bool(config.radarr.missing_movie_search.enabled),
                "cron": str(config.radarr.missing_movie_search.cron),
                "min_release_delay_days": int(config.radarr.missing_movie_search.min_release_delay_days),
                "max_movies_per_run": int(config.radarr.missing_movie_search.max_movies_per_run),
                "movie_cooldown_days": int(config.radarr.missing_movie_search.movie_cooldown_days),
            },
        },
        "extraction": {
            "enabled": bool(config.extraction.enabled),
            "delete_after_import": bool(config.extraction.delete_after_import),
            "staging_suffix": str(config.extraction.staging_suffix),
        },
        "web": {
            "enabled": bool(config.web.enabled),
            "host": str(config.web.host),
            "port": int(config.web.port),
        },
        "database": {
            "path": str(config.database.path),
            "retention_days": int(config.database.retention_days),
        },
        "path_mappings": [
            {"from": m.from_path, "to": m.to_path}
            for m in (config.path_mappings or [])
            if m.from_path and m.to_path
        ],
    }


def update_config_from_dict(config: AppConfig, data: Dict[str, Any]) -> AppConfig:
    """Update an existing AppConfig instance in-place from a dictionary payload."""
    if "general" in data and isinstance(data["general"], dict):
        g = data["general"]
        if "dry_run" in g and g["dry_run"] is not None:
            config.general.dry_run = bool(g["dry_run"])
        if "check_interval_seconds" in g and g["check_interval_seconds"] is not None:
            config.general.check_interval_seconds = max(5, int(g["check_interval_seconds"]))
        if "ai_cooldown_seconds" in g and g["ai_cooldown_seconds"] is not None:
            config.general.ai_cooldown_seconds = max(0.0, float(g["ai_cooldown_seconds"]))
        if "min_confidence" in g and g["min_confidence"] is not None:
            config.general.min_confidence = max(0.0, min(1.0, float(g["min_confidence"])))
        if "reject_on_low_confidence" in g and g["reject_on_low_confidence"] is not None:
            config.general.reject_on_low_confidence = bool(g["reject_on_low_confidence"])
        if "log_level" in g and g["log_level"]:
            config.general.log_level = str(g["log_level"]).upper()

    if "ai" in data and isinstance(data["ai"], dict):
        a = data["ai"]
        if a.get("provider") in ("gemini", "ollama"):
            config.ai.provider = a["provider"]
        if "fallback_enabled" in a and a["fallback_enabled"] is not None:
            config.ai.fallback_enabled = bool(a["fallback_enabled"])
        if "gemini" in a and isinstance(a["gemini"], dict):
            gm = a["gemini"]
            if "api_key" in gm and gm["api_key"] is not None:
                config.ai.gemini.api_key = str(gm["api_key"]).strip()
            if "model" in gm and gm["model"] is not None:
                config.ai.gemini.model = str(gm["model"]).strip() or "auto"
        if "ollama" in a and isinstance(a["ollama"], dict):
            ol = a["ollama"]
            if "base_url" in ol and ol["base_url"]:
                config.ai.ollama.base_url = str(ol["base_url"]).strip().rstrip("/")
            if "model" in ol and ol["model"]:
                config.ai.ollama.model = str(ol["model"]).strip()

    if "sonarr" in data and isinstance(data["sonarr"], dict):
        s = data["sonarr"]
        if "enabled" in s and s["enabled"] is not None:
            config.sonarr.enabled = bool(s["enabled"])
        if "url" in s and s["url"]:
            config.sonarr.url = str(s["url"]).strip().rstrip("/")
        if "api_key" in s and s["api_key"] is not None:
            config.sonarr.api_key = str(s["api_key"]).strip()
        if "season_pack_backfill" in s and isinstance(s["season_pack_backfill"], dict):
            spb = s["season_pack_backfill"]
            if "enabled" in spb and spb["enabled"] is not None:
                config.sonarr.season_pack_backfill.enabled = bool(spb["enabled"])
            if "cron" in spb and spb["cron"] is not None and str(spb["cron"]).strip():
                config.sonarr.season_pack_backfill.cron = normalize_cron_expr(str(spb["cron"]), fallback="0 2 * * *")
            elif "interval_hours" in spb and spb["interval_hours"] is not None:
                config.sonarr.season_pack_backfill.cron = interval_hours_to_cron(int(spb["interval_hours"]))
            if "min_missing_days" in spb and spb["min_missing_days"] is not None:
                config.sonarr.season_pack_backfill.min_missing_days = max(0, int(spb["min_missing_days"]))
            if "max_seasons_per_run" in spb and spb["max_seasons_per_run"] is not None:
                config.sonarr.season_pack_backfill.max_seasons_per_run = max(1, int(spb["max_seasons_per_run"]))
            if "season_cooldown_days" in spb and spb["season_cooldown_days"] is not None:
                config.sonarr.season_pack_backfill.season_cooldown_days = max(1, int(spb["season_cooldown_days"]))
        if "missing_episode_retry" in s and isinstance(s["missing_episode_retry"], dict):
            mer = s["missing_episode_retry"]
            if "enabled" in mer and mer["enabled"] is not None:
                config.sonarr.missing_episode_retry.enabled = bool(mer["enabled"])
            if "cron" in mer and mer["cron"] is not None and str(mer["cron"]).strip():
                config.sonarr.missing_episode_retry.cron = normalize_cron_expr(str(mer["cron"]), fallback="0 */6 * * *")
            elif "interval_hours" in mer and mer["interval_hours"] is not None:
                config.sonarr.missing_episode_retry.cron = interval_hours_to_cron(int(mer["interval_hours"]))
            if "min_air_delay_hours" in mer and mer["min_air_delay_hours"] is not None:
                config.sonarr.missing_episode_retry.min_air_delay_hours = max(0, int(mer["min_air_delay_hours"]))
            if "max_episodes_per_run" in mer and mer["max_episodes_per_run"] is not None:
                config.sonarr.missing_episode_retry.max_episodes_per_run = max(1, int(mer["max_episodes_per_run"]))
            if "episode_cooldown_hours" in mer and mer["episode_cooldown_hours"] is not None:
                config.sonarr.missing_episode_retry.episode_cooldown_hours = max(1, int(mer["episode_cooldown_hours"]))

    if "radarr" in data and isinstance(data["radarr"], dict):
        r = data["radarr"]
        if "enabled" in r and r["enabled"] is not None:
            config.radarr.enabled = bool(r["enabled"])
        if "url" in r and r["url"]:
            config.radarr.url = str(r["url"]).strip().rstrip("/")
        if "api_key" in r and r["api_key"] is not None:
            config.radarr.api_key = str(r["api_key"]).strip()
        if "missing_movie_search" in r and isinstance(r["missing_movie_search"], dict):
            mms = r["missing_movie_search"]
            if "enabled" in mms and mms["enabled"] is not None:
                config.radarr.missing_movie_search.enabled = bool(mms["enabled"])
            if "cron" in mms and mms["cron"] is not None and str(mms["cron"]).strip():
                config.radarr.missing_movie_search.cron = normalize_cron_expr(str(mms["cron"]), fallback="0 3 * * *")
            elif "interval_hours" in mms and mms["interval_hours"] is not None:
                config.radarr.missing_movie_search.cron = interval_hours_to_cron(int(mms["interval_hours"]))
            if "min_release_delay_days" in mms and mms["min_release_delay_days"] is not None:
                config.radarr.missing_movie_search.min_release_delay_days = max(0, int(mms["min_release_delay_days"]))
            if "max_movies_per_run" in mms and mms["max_movies_per_run"] is not None:
                config.radarr.missing_movie_search.max_movies_per_run = max(1, int(mms["max_movies_per_run"]))
            if "movie_cooldown_days" in mms and mms["movie_cooldown_days"] is not None:
                config.radarr.missing_movie_search.movie_cooldown_days = max(1, int(mms["movie_cooldown_days"]))

    if "extraction" in data and isinstance(data["extraction"], dict):
        ex = data["extraction"]
        if "enabled" in ex and ex["enabled"] is not None:
            config.extraction.enabled = bool(ex["enabled"])
        if "delete_after_import" in ex and ex["delete_after_import"] is not None:
            config.extraction.delete_after_import = bool(ex["delete_after_import"])
        if "staging_suffix" in ex and ex["staging_suffix"]:
            config.extraction.staging_suffix = str(ex["staging_suffix"]).strip()

    if "web" in data and isinstance(data["web"], dict):
        w = data["web"]
        if "enabled" in w and w["enabled"] is not None:
            config.web.enabled = bool(w["enabled"])
        if "host" in w and w["host"]:
            config.web.host = str(w["host"]).strip()
        if "port" in w and w["port"] is not None:
            config.web.port = int(w["port"])

    if "database" in data and isinstance(data["database"], dict):
        db = data["database"]
        if "path" in db and db["path"]:
            config.database.path = str(db["path"]).strip()
        if "retention_days" in db and db["retention_days"] is not None:
            config.database.retention_days = max(0, int(db["retention_days"]))

    if "path_mappings" in data and isinstance(data["path_mappings"], list):
        mappings = []
        for m in data["path_mappings"]:
            if isinstance(m, dict):
                f = (m.get("from") or m.get("from_path") or "").strip()
                t = (m.get("to") or m.get("to_path") or "").strip()
                if f and t:
                    mappings.append(PathMapping(**{"from": f, "to": t}))
            elif isinstance(m, PathMapping) and m.from_path and m.to_path:
                mappings.append(m)
        config.path_mappings = mappings

    return config


def save_config(config: AppConfig, config_path: Optional[str] = None) -> Path:
    """Persist AppConfig to YAML on disk."""
    target = resolve_config_path(config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = config_to_dict(config)
    with open(target, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)
    return target


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file and apply environment variable overrides."""
    selected_path = resolve_config_path(config_path)

    data = {}
    if selected_path.is_file():
        with open(selected_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    config = AppConfig()
    if isinstance(data, dict):
        update_config_from_dict(config, data)

    # General overrides
    if env_dry_run := os.getenv("DRY_RUN"):
        config.general.dry_run = env_dry_run.lower() in ("true", "1", "yes")
    if env_reject_low := os.getenv("REJECT_ON_LOW_CONFIDENCE"):
        config.general.reject_on_low_confidence = env_reject_low.lower() in ("true", "1", "yes")
    if env_min_conf := os.getenv("MIN_CONFIDENCE"):
        try:
            config.general.min_confidence = float(env_min_conf)
        except ValueError:
            pass
    if env_interval := os.getenv("CHECK_INTERVAL_SECONDS"):
        try:
            config.general.check_interval_seconds = int(env_interval)
        except ValueError:
            pass
    if env_cooldown := os.getenv("AI_COOLDOWN_SECONDS"):
        try:
            config.general.ai_cooldown_seconds = max(0.0, float(env_cooldown))
        except ValueError:
            pass
    if env_log_level := os.getenv("LOG_LEVEL"):
        config.general.log_level = env_log_level.strip().upper()

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
    if env_bf_enabled := os.getenv("SONARR_BACKFILL_ENABLED"):
        config.sonarr.season_pack_backfill.enabled = env_bf_enabled.lower() in ("true", "1", "yes")
    if env_bf_cron := os.getenv("SONARR_BACKFILL_CRON"):
        config.sonarr.season_pack_backfill.cron = normalize_cron_expr(env_bf_cron, fallback="0 2 * * *")
    elif env_bf_interval := os.getenv("SONARR_BACKFILL_INTERVAL_HOURS"):
        try:
            config.sonarr.season_pack_backfill.cron = interval_hours_to_cron(int(env_bf_interval))
        except ValueError:
            pass
    if env_bf_missing_days := os.getenv("SONARR_BACKFILL_MIN_MISSING_DAYS"):
        try:
            config.sonarr.season_pack_backfill.min_missing_days = max(0, int(env_bf_missing_days))
        except ValueError:
            pass
    if env_bf_max_seasons := os.getenv("SONARR_BACKFILL_MAX_SEASONS_PER_RUN"):
        try:
            config.sonarr.season_pack_backfill.max_seasons_per_run = max(1, int(env_bf_max_seasons))
        except ValueError:
            pass
    if env_bf_cooldown := os.getenv("SONARR_BACKFILL_COOLDOWN_DAYS"):
        try:
            config.sonarr.season_pack_backfill.season_cooldown_days = max(1, int(env_bf_cooldown))
        except ValueError:
            pass
    if env_mer_enabled := os.getenv("SONARR_EPISODE_RETRY_ENABLED"):
        config.sonarr.missing_episode_retry.enabled = env_mer_enabled.lower() in ("true", "1", "yes")
    if env_mer_cron := os.getenv("SONARR_EPISODE_RETRY_CRON"):
        config.sonarr.missing_episode_retry.cron = normalize_cron_expr(env_mer_cron, fallback="0 */6 * * *")
    elif env_mer_interval := os.getenv("SONARR_EPISODE_RETRY_INTERVAL_HOURS"):
        try:
            config.sonarr.missing_episode_retry.cron = interval_hours_to_cron(int(env_mer_interval))
        except ValueError:
            pass
    if env_mer_delay := os.getenv("SONARR_EPISODE_RETRY_MIN_AIR_DELAY_HOURS"):
        try:
            config.sonarr.missing_episode_retry.min_air_delay_hours = max(0, int(env_mer_delay))
        except ValueError:
            pass
    if env_mer_max := os.getenv("SONARR_EPISODE_RETRY_MAX_EPISODES_PER_RUN"):
        try:
            config.sonarr.missing_episode_retry.max_episodes_per_run = max(1, int(env_mer_max))
        except ValueError:
            pass
    if env_mer_cooldown := os.getenv("SONARR_EPISODE_RETRY_COOLDOWN_HOURS"):
        try:
            config.sonarr.missing_episode_retry.episode_cooldown_hours = max(1, int(env_mer_cooldown))
        except ValueError:
            pass

    # Radarr overrides
    if env_radarr_enabled := os.getenv("RADARR_ENABLED"):
        config.radarr.enabled = env_radarr_enabled.lower() in ("true", "1", "yes")
    if env_radarr_url := os.getenv("RADARR_URL"):
        config.radarr.url = env_radarr_url
    if env_radarr_key := os.getenv("RADARR_API_KEY"):
        config.radarr.api_key = env_radarr_key

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

    # Web UI overrides
    if env_web_enabled := os.getenv("WEB_ENABLED"):
        config.web.enabled = env_web_enabled.lower() in ("true", "1", "yes")
    if env_web_host := os.getenv("WEB_HOST"):
        config.web.host = env_web_host.strip()
    if env_web_port := (os.getenv("WEB_PORT") or os.getenv("PORT")):
        try:
            config.web.port = int(env_web_port.strip())
        except ValueError:
            pass

    # Database overrides
    if env_db_path := os.getenv("DATABASE_PATH"):
        config.database.path = env_db_path.strip()
    if env_db_retention := os.getenv("DATABASE_RETENTION_DAYS"):
        try:
            config.database.retention_days = int(env_db_retention.strip())
        except ValueError:
            pass

    # Normalize URLs (strip trailing slashes)
    config.sonarr.url = config.sonarr.url.rstrip("/")
    config.radarr.url = config.radarr.url.rstrip("/")
    config.ai.ollama.base_url = config.ai.ollama.base_url.rstrip("/")

    return config
