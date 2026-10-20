import logging
from typing import List, Optional
from .base import (
    BaseAIProvider,
    MediaMatchResult,
    EpisodeMatchResult,
    SampleEvaluationResult,
    LibraryMatchResult,
    AIProviderError
)

logger = logging.getLogger("aimportarr.ai.fallback")

class FallbackProvider(BaseAIProvider):
    """
    Wraps a primary AI provider (e.g. Gemini) and a secondary fallback provider (e.g. Ollama).
    If the primary provider fails due to invalid API keys, rate limits, token exhaustion, or network errors,
    it automatically delegates the request to the fallback provider.
    """
    def __init__(self, primary: BaseAIProvider, fallback: Optional[BaseAIProvider] = None):
        self.primary = primary
        self.fallback = fallback

    @property
    def name(self) -> str:
        if self.fallback:
            return f"{self.primary.name} with Fallback to {self.fallback.name}"
        return self.primary.name

    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        try:
            return await self.primary.verify_series_match(
                release_title=release_title,
                folder_name=folder_name,
                candidate_series_title=candidate_series_title,
                alternate_titles=alternate_titles,
                year=year
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return MediaMatchResult(is_match=False, confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.verify_series_match(
                    release_title=release_title,
                    folder_name=folder_name,
                    candidate_series_title=candidate_series_title,
                    alternate_titles=alternate_titles,
                    year=year
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return MediaMatchResult(is_match=False, confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")

    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        try:
            return await self.primary.verify_movie_match(
                release_title=release_title,
                folder_name=folder_name,
                candidate_movie_title=candidate_movie_title,
                alternate_titles=alternate_titles,
                year=year
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return MediaMatchResult(is_match=False, confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.verify_movie_match(
                    release_title=release_title,
                    folder_name=folder_name,
                    candidate_movie_title=candidate_movie_title,
                    alternate_titles=alternate_titles,
                    year=year
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return MediaMatchResult(is_match=False, confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")

    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        try:
            return await self.primary.parse_episode(
                file_name=file_name,
                folder_name=folder_name,
                series_title=series_title,
                known_seasons=known_seasons,
                known_episodes_in_season=known_episodes_in_season
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return EpisodeMatchResult(season_number=None, episode_numbers=[], confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.parse_episode(
                    file_name=file_name,
                    folder_name=folder_name,
                    series_title=series_title,
                    known_seasons=known_seasons,
                    known_episodes_in_season=known_episodes_in_season
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return EpisodeMatchResult(season_number=None, episode_numbers=[], confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")

    async def evaluate_sample_or_extra(
        self,
        file_name: str,
        folder_name: str,
        media_title: str
    ) -> SampleEvaluationResult:
        try:
            return await self.primary.evaluate_sample_or_extra(
                file_name=file_name,
                folder_name=folder_name,
                media_title=media_title
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return SampleEvaluationResult(is_valid_media=False, confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.evaluate_sample_or_extra(
                    file_name=file_name,
                    folder_name=folder_name,
                    media_title=media_title
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return SampleEvaluationResult(is_valid_media=False, confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")

    async def match_unknown_media(
        self,
        release_title: str,
        folder_name: str,
        media_type: str,
        candidates: List[dict]
    ) -> LibraryMatchResult:
        try:
            return await self.primary.match_unknown_media(
                release_title=release_title,
                folder_name=folder_name,
                media_type=media_type,
                candidates=candidates
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return LibraryMatchResult(matched_id=None, matched_title=None, confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.match_unknown_media(
                    release_title=release_title,
                    folder_name=folder_name,
                    media_type=media_type,
                    candidates=candidates
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return LibraryMatchResult(matched_id=None, matched_title=None, confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")
