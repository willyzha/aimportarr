import json
import logging
import httpx
from typing import List, Optional
from .base import BaseAIProvider, MediaMatchResult, EpisodeMatchResult

logger = logging.getLogger("aimportarr.ai.gemini")

class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key = api_key
        self.model = model
        self.base_url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"

    async def _call_gemini(self, prompt: str, schema_desc: str) -> dict:
        url = f"{self.base_url}?key={self.api_key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "You are an expert TV and Movie media parser assistant for the *arr suite (Sonarr/Radarr).\n"
                                "Your job is to accurately match messy release filenames, Chinese/Asian Pinyin titles, and foreign characters to official media.\n"
                                f"{schema_desc}\n\n"
                                f"{prompt}"
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError(f"No candidates returned from Gemini: {data}")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise ValueError("Empty response parts from Gemini")

        raw_text = parts[0].get("text", "{}")
        return json.loads(raw_text)

    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- is_match (boolean): true if the release definitely refers to this series, false otherwise\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining the decision, citing Pinyin translation, English title, or tracker conventions)"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Series: {candidate_series_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate TV series. "
            "Consider Chinese Pinyin translations, Romaji, and common Asian tracker naming conventions (e.g., Avistaz, BaoziPT)."
        )

        try:
            res = await self._call_gemini(prompt, schema_desc)
            return MediaMatchResult(**res)
        except Exception as e:
            logger.error(f"Gemini verify_series_match failed: {e}")
            return MediaMatchResult(
                is_match=False,
                confidence=0.0,
                reasoning=f"Gemini API error: {str(e)}"
            )

    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- is_match (boolean): true if the release definitely refers to this movie, false otherwise\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining the decision, citing Pinyin translation, English title, or tracker conventions)"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Movie: {candidate_movie_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate movie in Radarr. "
            "Account for foreign release names, Pinyin, translated movie titles, and release year discrepancies (+/- 1 year between festival and theatrical release)."
        )

        try:
            res = await self._call_gemini(prompt, schema_desc)
            return MediaMatchResult(**res)
        except Exception as e:
            logger.error(f"Gemini verify_movie_match failed: {e}")
            return MediaMatchResult(
                is_match=False,
                confidence=0.0,
                reasoning=f"Gemini API error: {str(e)}"
            )

    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- season_number (integer or null): the identified season number\n"
            "- episode_numbers (list of integers): the identified episode number(s), e.g. [1] or [1, 2]\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining how the season/episode was derived from the file/folder name)"
        )

        ep_titles = [f"E{e.get('episodeNumber')}: {e.get('title')}" for e in known_episodes_in_season[:30]]

        prompt = (
            f"Series Title: {series_title}\n"
            f"Folder Name: {folder_name}\n"
            f"File Name: {file_name}\n"
            f"Known Seasons: {known_seasons}\n"
            f"Sample Episodes in candidate season: {ep_titles}\n\n"
            "Task: Extract the Season Number and Episode Number(s) from this file path. "
            "Look for patterns like 'S01E02', 'EP02', '02.mkv', '第02集', 'Part 1', or episode titles in the filename."
        )

        try:
            res = await self._call_gemini(prompt, schema_desc)
            return EpisodeMatchResult(**res)
        except Exception as e:
            logger.error(f"Gemini parse_episode failed: {e}")
            return EpisodeMatchResult(
                season_number=None,
                episode_numbers=[],
                confidence=0.0,
                reasoning=f"Gemini API error: {str(e)}"
            )
