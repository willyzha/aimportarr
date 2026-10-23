import logging
from typing import List, Optional, Dict, Any
from .base import (
    BaseAIProvider,
    Stage1TriageResult,
    Stage2DecisionResult,
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

    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        try:
            return await self.primary.evaluate_stage1_triage(
                release_title=release_title,
                error_messages=error_messages,
                disk_files=disk_files,
                archive_name=archive_name,
                archive_files=archive_files,
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return Stage1TriageResult(action="no_extraction", confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.evaluate_stage1_triage(
                    release_title=release_title,
                    error_messages=error_messages,
                    disk_files=disk_files,
                    archive_name=archive_name,
                    archive_files=archive_files,
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return Stage1TriageResult(action="no_extraction", confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")

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
        try:
            return await self.primary.evaluate_import_decision(
                media_type=media_type,
                release_title=release_title,
                folder_name=folder_name,
                queue_messages=queue_messages,
                candidate_media=candidate_media,
                library_candidates=library_candidates,
                manual_import_files=manual_import_files,
                existing_disk_files=existing_disk_files,
                known_episodes=known_episodes,
                raw_disk_files=raw_disk_files,
            )
        except (AIProviderError, Exception) as e:
            if not self.fallback:
                logger.error(f"Primary AI provider '{self.primary.name}' failed: {e}. No fallback configured.")
                return Stage2DecisionResult(decision="reject_keep_in_client", confidence=0.0, reasoning=f"AI error: {e}")

            logger.warning(f"Primary AI provider '{self.primary.name}' failed: {e}. Automatically falling back to '{self.fallback.name}'...")
            try:
                return await self.fallback.evaluate_import_decision(
                    media_type=media_type,
                    release_title=release_title,
                    folder_name=folder_name,
                    queue_messages=queue_messages,
                    candidate_media=candidate_media,
                    library_candidates=library_candidates,
                    manual_import_files=manual_import_files,
                    existing_disk_files=existing_disk_files,
                    known_episodes=known_episodes,
                    raw_disk_files=raw_disk_files,
                )
            except Exception as fe:
                logger.error(f"Fallback AI provider '{self.fallback.name}' also failed: {fe}")
                return Stage2DecisionResult(decision="reject_keep_in_client", confidence=0.0, reasoning=f"Both primary and fallback failed: {fe}")
