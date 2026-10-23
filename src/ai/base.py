from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class AIProviderError(Exception):
    """Raised when an AI provider fails due to API keys, rate limits, network, or server errors."""
    pass


class Stage1TriageResult(BaseModel):
    action: str = Field(
        default="no_extraction",
        description="One of: 'extract', 'no_extraction', 'reject_delete'"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the Stage 1 triage decision")
    category_tag: str = Field(default="malware_or_corrupt", description="Short tag such as malware_or_corrupt, sample_only, archive_extracted")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")


class ApprovedFileItem(BaseModel):
    path: str = Field(default="", description="Exact file path from the candidate files list")
    season_number: Optional[int] = Field(default=None, description="Season number (for Sonarr)")
    episode_numbers: List[int] = Field(default_factory=list, description="Episode numbers (for Sonarr)")


class Stage2DecisionResult(BaseModel):
    decision: str = Field(
        default="reject_keep_in_client",
        description="One of: 'import', 'reject_keep_in_client', 'reject_delete', 'dismiss_pack'"
    )
    matched_media_id: Optional[int] = Field(default=None, description="Matched seriesId or movieId")
    files_to_import: List[ApprovedFileItem] = Field(default_factory=list, description="Files approved for import")
    category_tag: str = Field(default="ai_decision", description="Short tag describing the primary reason")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the Stage 2 import/reject decision")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

    def __init__(self, **kwargs):
        if "files_to_import" in kwargs and isinstance(kwargs["files_to_import"], list):
            kwargs["files_to_import"] = [
                ApprovedFileItem(**f) if isinstance(f, dict) else f
                for f in kwargs["files_to_import"]
            ]
        super().__init__(**kwargs)


class BaseAIProvider(ABC):
    """Abstract interface for Two-Stage AI Providers (Gemini, Ollama, etc.)"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        """
        Stage 1 AI Triage: Determine if a stuck release requires archive extraction ('extract'),
        no extraction ('no_extraction'), or immediate deletion & blocklisting as virus/sample/corrupt ('reject_delete').
        """
        pass

    @abstractmethod
    async def evaluate_import_decision(
        self,
        media_type: str,
        release_title: str,
        folder_name: str,
        queue_messages: List[str],
        candidate_media: Optional[Dict[str, Any]],
        library_candidates: Optional[List[Dict[str, Any]]],
        manual_import_files: List[Dict[str, Any]],
        existing_disk_files: List[Dict[str, Any]],
        known_episodes: Optional[List[Dict[str, Any]]] = None,
        raw_disk_files: Optional[List[str]] = None,
    ) -> Stage2DecisionResult:
        """
        Stage 2 Unified AI Decision: Determine whether to 'import', 'reject_keep_in_client' (downgrade/wrong media),
        'reject_delete' (virus/sample-only/corrupt), or 'dismiss_pack' (already satisfied season pack).
        """
        pass
