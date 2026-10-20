from abc import ABC, abstractmethod
from typing import List, Optional
from pydantic import BaseModel, Field

class AIProviderError(Exception):
    """Raised when an AI provider fails due to API keys, rate limits, network, or server errors."""
    pass

class MediaMatchResult(BaseModel):
    is_match: bool = Field(description="Whether the release title matches the candidate media")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(description="Brief explanation of how the match was verified or rejected")

# Backward compatibility alias
SeriesMatchResult = MediaMatchResult

class EpisodeMatchResult(BaseModel):
    season_number: Optional[int] = Field(None, description="Detected season number")
    episode_numbers: List[int] = Field(default_factory=list, description="List of detected episode numbers (e.g. [5] or [1, 2])")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(description="Brief explanation of how the episode was extracted")

class BaseAIProvider(ABC):
    """Abstract interface for AI Providers (Gemini, Ollama, etc.)"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

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
