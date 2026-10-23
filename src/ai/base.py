from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class AIProviderError(Exception):
    """Raised when an AI provider fails due to API keys, rate limits, network, or server errors."""
    pass

class MediaMatchResult(BaseModel):
    is_match: bool = Field(default=False, description="Whether the release title matches the candidate media")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Brief explanation of how the match was verified or rejected")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

# Backward compatibility alias
SeriesMatchResult = MediaMatchResult

class EpisodeMatchResult(BaseModel):
    season_number: Optional[int] = Field(default=None, description="Detected season number")
    episode_numbers: List[int] = Field(default_factory=list, description="List of detected episode numbers (e.g. [5] or [1, 2])")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Brief explanation of how the episode was extracted")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

class SampleEvaluationResult(BaseModel):
    is_valid_media: bool = Field(default=True, description="Whether the file is genuine media rather than a disposable sample/trailer")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the sample evaluation")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

class LibraryMatchResult(BaseModel):
    matched_id: Optional[int] = Field(default=None, description="ID of the matching media in the user's library, or null")
    matched_title: Optional[str] = Field(default=None, description="Title of the matching media in the library")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Brief explanation of how the match was found")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

class MalwareEvaluationResult(BaseModel):
    is_fake_or_malware: bool = Field(default=False, description="Whether the release is a fake, scam, or malware/virus release")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Brief explanation of why this is or isn't a fake/malware release")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

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
        default="reject_keep_seeding",
        description="One of: 'import', 'reject_keep_seeding', 'reject_delete', 'dismiss_pack'"
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
    """Abstract interface for AI Providers (Gemini, Ollama, etc.)"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

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
        res = await self.evaluate_fake_or_malware(
            release_title=release_title,
            file_name=archive_name or (disk_files[0] if disk_files else release_title),
            error_messages=error_messages,
            archive_files=archive_files or disk_files,
        )
        if res.is_fake_or_malware:
            return Stage1TriageResult(
                action="reject_delete",
                confidence=res.confidence,
                reasoning=res.reasoning,
                category_tag="malware_or_corrupt",
                model_name=res.model_name,
            )
        return Stage1TriageResult(
            action="extract" if archive_name else "no_extraction",
            confidence=res.confidence,
            reasoning=res.reasoning,
            category_tag="archive_extracted" if archive_name else "triage_passed",
            model_name=res.model_name,
        )

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
        Stage 2 Unified AI Decision: Determine whether to 'import', 'reject_keep_seeding' (downgrade/wrong media),
        'reject_delete' (virus/sample-only/corrupt), or 'dismiss_pack' (already satisfied season pack).
        """
        raise NotImplementedError("Subclasses must implement evaluate_import_decision")

    @abstractmethod
    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        """Determine if a release title matches a candidate TV series."""
        pass

    @abstractmethod
    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        """Determine if a release title matches a candidate Movie (Radarr)."""
        pass

    @abstractmethod
    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        """Extract Season and Episode numbers from ambiguous filenames."""
        pass

    @abstractmethod
    async def evaluate_sample_or_extra(
        self,
        file_name: str,
        folder_name: str,
        media_title: str
    ) -> SampleEvaluationResult:
        """Determine if a file flagged as 'sample' is actually a valid media item."""
        pass

    @abstractmethod
    async def match_unknown_media(
        self,
        release_title: str,
        folder_name: str,
        media_type: str,
        candidates: List[dict]
    ) -> LibraryMatchResult:
        """Match an unknown release to one of the tracked media items in the user's library."""
        pass

    @abstractmethod
    async def evaluate_fake_or_malware(
        self,
        release_title: str,
        file_name: str,
        error_messages: List[str],
        archive_files: Optional[List[str]] = None
    ) -> MalwareEvaluationResult:
        """Determine if a release is a fake, scam, or malware/virus release."""
        pass
