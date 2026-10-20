import json
import logging
import re
from datetime import datetime
from typing import List, Optional
import httpx
from .base import (
    BaseAIProvider,
    MediaMatchResult,
    EpisodeMatchResult,
    SampleEvaluationResult,
    LibraryMatchResult,
    AIProviderError
)

logger = logging.getLogger("aimportarr.ai.gemini")

class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: str, model: str = "auto"):
        self.api_key = api_key
        self.model = model.strip() if model else "auto"
        self._resolved_model: Optional[str] = None
        self._failed_models: set = set()

    @property
    def name(self) -> str:
        active = self._resolved_model or self.model
        return f"Gemini ({active})"

    async def resolve_smallest_model(self, force_refresh: bool = False) -> str:
        """
        Dynamically query Google Generative Language API to discover and verify the smallest
        and fastest working text generation model for the current API key.
        Prioritizes:
          1. Standard flash models (e.g. gemini-3.5-flash, gemini-3-flash-preview)
          2. flash-lite models
          3. Higher version numbers over older versions
        Probes candidate models to ensure they respond with HTTP 200 rather than timing out.
        """
        if not force_refresh and self._resolved_model and self._resolved_model not in self._failed_models:
            return self._resolved_model

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(
                "Failed to auto-discover Gemini models catalog: %s. Falling back to default 'gemini-3.5-flash'.",
                e
            )
            fallback = "gemini-3.5-flash" if "gemini-3.5-flash" not in self._failed_models else "gemini-3-flash-preview"
            self._resolved_model = fallback
            return fallback

        candidates = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in methods:
                continue
            # Exclude specialized modalities (image generation, tts, robotics, audio, transcription)
            if any(x in name for x in ["image", "tts", "robotics", "clip", "banana", "transcribe", "argon", "computer-use"]):
                continue
            if name in self._failed_models:
                continue
            candidates.append(name)

        def model_score(name: str):
            # Tier 1: standard flash (fastest and most reliable on API)
            # Tier 2: flash-lite
            # Tier 3: other (pro, etc.)
            tier = 3
            if "flash" in name and "flash-lite" not in name:
                tier = 1
            elif "flash-lite" in name:
                tier = 2

            v_match = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            version = float(v_match.group(1)) if v_match else 0.0
            is_preview = 1 if "preview" in name else 0
            is_generic_latest = 1 if "latest" in name else 0
            return (tier, is_generic_latest, -version, is_preview, name)

        candidates.sort(key=model_score)

        # Probe candidate models with a lightweight ping to verify responsiveness
        async with httpx.AsyncClient(timeout=5.0) as probe_client:
            for candidate in candidates[:8]:
                probe_url = f"https://generativelanguage.googleapis.com/v1beta/models/{candidate}:generateContent?key={self.api_key}"
                probe_payload = {"contents": [{"parts": [{"text": "ping"}]}]}
                try:
                    r = await probe_client.post(probe_url, json=probe_payload)
                    if r.status_code == 200:
                        logger.info("Auto-resolved active Gemini model: '%s' (verified responsive)", candidate)
                        self._resolved_model = candidate
                        return candidate
                    else:
                        logger.debug("Candidate model '%s' returned HTTP %s during probe. Trying next...", candidate, r.status_code)
                        self._failed_models.add(candidate)
                except Exception as probe_err:
                    logger.debug("Candidate model '%s' timed out or failed probe: %s. Trying next...", candidate, probe_err)
                    self._failed_models.add(candidate)

        logger.warning("No probed Gemini models responded successfully. Defaulting to 'gemini-3.5-flash'.")
        self._resolved_model = "gemini-3.5-flash"
        return "gemini-3.5-flash"

    async def _call_gemini(self, prompt: str, schema_desc: str) -> dict:
        if self.model in ("auto", "smallest", ""):
            current_model = await self.resolve_smallest_model()
        else:
            current_model = self._resolved_model or self.model

        today_str = datetime.now().strftime("%Y-%m-%d")
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "You are an expert TV and Movie media parser assistant for the *arr suite (Sonarr/Radarr).\n"
                                f"Current Date: {today_str}.\n"
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

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 404:
                    # Model deprecated or sunset by Google! Mark as failed and self-heal
                    self._failed_models.add(current_model)
                    logger.warning(
                        "Gemini model '%s' returned HTTP 404 (model retired or unavailable). Auto-discovering replacement...",
                        current_model
                    )
                    new_model = await self.resolve_smallest_model(force_refresh=True)
                    if new_model != current_model:
                        logger.info("Retrying request with newly resolved model: '%s'...", new_model)
                        current_model = new_model
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={self.api_key}"
                        resp = await client.post(url, json=payload)

                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                raise AIProviderError(f"Gemini API Key is invalid or unauthorized (HTTP {status})") from e
            elif status == 429:
                raise AIProviderError(f"Gemini rate limit or quota exceeded (HTTP 429 / Out of tokens)") from e
            else:
                raise AIProviderError(f"Gemini HTTP {status} error: {e.response.text}") from e
        except (httpx.RequestError, httpx.TimeoutException) as e:
            raise AIProviderError(f"Gemini connection / timeout error: {e}") from e
        except Exception as e:
            raise AIProviderError(f"Unexpected error calling Gemini API: {e}") from e

        candidates = data.get("candidates", [])
        if not candidates:
            raise AIProviderError(f"No response candidates returned from Gemini: {data}")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise AIProviderError("Empty response parts from Gemini")

        raw_text = parts[0].get("text", "{}")
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise AIProviderError(f"Failed to parse Gemini JSON output: {raw_text}") from e

    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- is_match (boolean): true if the release definitely refers to this series, false otherwise\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining the decision, citing Pinyin translation, English title, or naming conventions)"
        )
        prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Series: {candidate_series_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate TV series. "
            "Consider Chinese Pinyin translations, Romaji, and common international media naming conventions.\n\n"
            f"Important Context: Today is {today_str}. Media with release year {current_year} is currently active and releasing now. Do NOT reject matches by assuming {current_year} releases are unreleased or in the future."
        )

        res = await self._call_gemini(prompt, schema_desc)
        return MediaMatchResult(**res)

    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- is_match (boolean): true if the release definitely refers to this movie, false otherwise\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining the decision, citing Pinyin translation, English title, or naming conventions)"
        )
        prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Movie: {candidate_movie_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate movie in Radarr. "
            "Account for foreign release names, Pinyin, translated movie titles, and release year discrepancies (+/- 1 year between festival and theatrical release).\n\n"
            f"Important Context: Today is {today_str}. Movies with release year {current_year} are currently active in theaters or on digital streaming. Do NOT reject matches by assuming {current_year} releases are unreleased or in the future."
        )

        res = await self._call_gemini(prompt, schema_desc)
        return MediaMatchResult(**res)

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

        res = await self._call_gemini(prompt, schema_desc)
        return EpisodeMatchResult(**res)

    async def evaluate_sample_or_extra(
        self,
        file_name: str,
        folder_name: str,
        media_title: str
    ) -> SampleEvaluationResult:
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- is_valid_media (boolean): true if the file is genuine episode or feature content, false if it is merely a preview, sample, or disposable extra\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining your evaluation)"
        )
        prompt = (
            f"Media Title: {media_title}\n"
            f"Folder Name: {folder_name}\n"
            f"File Name: {file_name}\n\n"
            "Task: Determine if this file is a genuine media release (such as an episode, OVA, special, or main feature) "
            "or if it is merely a disposable sample clip/trailer that should be ignored."
        )
        res = await self._call_gemini(prompt, schema_desc)
        return SampleEvaluationResult(**res)

    async def match_unknown_media(
        self,
        release_title: str,
        folder_name: str,
        media_type: str,
        candidates: List[dict]
    ) -> LibraryMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        schema_desc = (
            "Return a JSON object with exactly these keys:\n"
            "- matched_id (integer or null): the ID of the candidate media item that matches, or null if no match\n"
            "- matched_title (string or null): the title of the matched media item, or null\n"
            "- confidence (float between 0.0 and 1.0)\n"
            "- reasoning (string explaining the decision, citing Pinyin translation, aliases, or naming matches)"
        )
        candidate_summary = [
            f"ID {c.get('id')}: '{c.get('title')}' ({c.get('year', 'N/A')}) | Aliases: {c.get('aliases', [])}"
            for c in candidates[:50]
        ]
        candidates_str = "\n".join(candidate_summary)

        prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Media Type: {media_type}\n\n"
            f"User's Library Candidates:\n{candidates_str}\n\n"
            "Task: Identify which library candidate this release belongs to. "
            "Consider Chinese Pinyin translations, Romaji, alternate titles, and year variations.\n\n"
            f"Important Context: Today is {today_str}. Media with release year {current_year} is currently active and releasing now."
        )
        res = await self._call_gemini(prompt, schema_desc)
        return LibraryMatchResult(**res)
