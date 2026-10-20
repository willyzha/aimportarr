import json
import logging
import httpx
from typing import List, Optional
from .base import BaseAIProvider, MediaMatchResult, EpisodeMatchResult

logger = logging.getLogger("aimportarr.ai.ollama")

class OllamaProvider(BaseAIProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:3b"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.chat_url = f"{self.base_url}/api/chat"

    async def _call_ollama(self, system_prompt: str, user_prompt: str) -> dict:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1
            }
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.chat_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        message = data.get("message", {})
        raw_text = message.get("content", "{}")
        return json.loads(raw_text)

    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        system_prompt = (
            "You are an expert TV media parser assistant for Sonarr. "
            "You must respond ONLY with a valid JSON object with keys: 'is_match' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Series: {candidate_series_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release matches the candidate TV series. "
            "Consider Chinese Pinyin translations, Romaji, and common international media naming conventions."
        )

        try:
            res = await self._call_ollama(system_prompt, user_prompt)
            return MediaMatchResult(**res)
        except Exception as e:
            logger.error(f"Ollama verify_series_match failed: {e}")
            return MediaMatchResult(
                is_match=False,
                confidence=0.0,
                reasoning=f"Ollama API error: {str(e)}"
            )

    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        system_prompt = (
            "You are an expert Movie media parser assistant for Radarr. "
            "You must respond ONLY with a valid JSON object with keys: 'is_match' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Movie: {candidate_movie_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate movie in Radarr. "
            "Account for foreign release names, Pinyin, translated movie titles, and release year discrepancies (+/- 1 year between festival and theatrical release)."
        )

        try:
            res = await self._call_ollama(system_prompt, user_prompt)
            return MediaMatchResult(**res)
        except Exception as e:
            logger.error(f"Ollama verify_movie_match failed: {e}")
            return MediaMatchResult(
                is_match=False,
                confidence=0.0,
                reasoning=f"Ollama API error: {str(e)}"
            )

    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        system_prompt = (
            "You are an expert TV media parser assistant for Sonarr. "
            "You must respond ONLY with a valid JSON object with keys: "
            "'season_number' (integer or null), 'episode_numbers' (list of integers), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )

        ep_titles = [f"E{e.get('episodeNumber')}: {e.get('title')}" for e in known_episodes_in_season[:30]]

        user_prompt = (
            f"Series Title: {series_title}\n"
            f"Folder Name: {folder_name}\n"
            f"File Name: {file_name}\n"
            f"Known Seasons: {known_seasons}\n"
            f"Sample Episodes: {ep_titles}\n\n"
            "Task: Extract the Season Number and Episode Number(s) from this file path. "
            "Look for patterns like 'S01E02', 'EP02', '02.mkv', '第02集', 'Part 1', or episode titles."
        )

        try:
            res = await self._call_ollama(system_prompt, user_prompt)
            return EpisodeMatchResult(**res)
        except Exception as e:
            logger.error(f"Ollama parse_episode failed: {e}")
            return EpisodeMatchResult(
                season_number=None,
                episode_numbers=[],
                confidence=0.0,
                reasoning=f"Ollama API error: {str(e)}"
            )
