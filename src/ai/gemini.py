import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
    MediaMatchResult,
    EpisodeMatchResult,
    SampleEvaluationResult,
    LibraryMatchResult,
    MalwareEvaluationResult,
    AIProviderError
)

logger = logging.getLogger("aimportarr.ai.gemini")

class GeminiProvider(BaseAIProvider):
    def __init__(self, api_key: str, model: str = "auto"):
        self.api_key = api_key
        self.model = model.strip() if model else "auto"
        self._resolved_model: Optional[str] = None
        self._failed_models: set = set()
        self._rate_limited_models: Dict[str, float] = {}
        self._discovered_candidates: List[str] = []
        self._last_call_time: float = 0.0
        self._min_call_interval: float = 2.0  # Pacing delay between calls to respect rate limits

    @property
    def name(self) -> str:
        active = self._resolved_model or self.model
        return f"Gemini ({active})"

    async def get_candidate_models(self) -> List[str]:
        """Fetch and score candidate models once or when refreshed."""
        if self._discovered_candidates:
            return self._discovered_candidates

        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(
                "Failed to auto-discover Gemini models catalog: %s. Using default flash fallback list.",
                e
            )
            return [
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
                "gemini-3.5-flash",
                "gemini-3-flash-preview",
                "gemini-flash-latest"
            ]

        candidates = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            name = m.get("name", "").replace("models/", "")
            if "generateContent" not in methods:
                continue
            # Exclude specialized modalities (image generation, tts, robotics, audio, transcription)
            if any(x in name for x in ["image", "tts", "robotics", "clip", "banana", "transcribe", "argon", "computer-use"]):
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
        self._discovered_candidates = candidates
        return candidates

    async def resolve_smallest_model(self, force_refresh: bool = False) -> str:
        """
        Select the best available model for text generation without wasting API calls on probe pings.
        Filters out models that returned 404 (retired) or are currently on rate-limit cooldown (429/503).
        """
        now = time.time()
        # Clean expired rate limits
        expired = [m for m, exp in self._rate_limited_models.items() if exp <= now]
        for m in expired:
            del self._rate_limited_models[m]

        if not force_refresh and self._resolved_model:
            if self._resolved_model not in self._failed_models and self._resolved_model not in self._rate_limited_models:
                return self._resolved_model

        candidates = await self.get_candidate_models()
        available = [
            c for c in candidates
            if c not in self._failed_models and c not in self._rate_limited_models
        ]

        if available:
            chosen = available[0]
            if chosen != self._resolved_model:
                logger.info("Active Gemini model selected: '%s'", chosen)
            self._resolved_model = chosen
            return chosen

        # If all candidates are currently rate-limited, try the one whose cooldown expires soonest
        if self._rate_limited_models:
            soonest = min(self._rate_limited_models.keys(), key=lambda k: self._rate_limited_models[k])
            logger.warning("All Gemini candidate models are rate-limited. Trying soonest available '%s'...", soonest)
            self._resolved_model = soonest
            return soonest

        fallback = "gemini-3.6-flash"
        self._resolved_model = fallback
        return fallback

    async def _call_gemini(
        self,
        prompt: str,
        schema_desc: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> dict:
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

        # Allow up to 4 attempts with model rotation on 404, 429, or 503
        max_attempts = 4
        last_error = None

        for attempt in range(max_attempts):
            if self.model in ("auto", "smallest", ""):
                current_model = await self.resolve_smallest_model()
            else:
                current_model = self._resolved_model or self.model

            # Pacing: enforce minimum delay between calls to avoid bursting over rate limits
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self._min_call_interval:
                await asyncio.sleep(self._min_call_interval - elapsed)
            self._last_call_time = time.time()

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={self.api_key}"

            try:
                start_time = time.time()
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(url, json=payload)
                    status = resp.status_code

                    if status == 404:
                        self._failed_models.add(current_model)
                        logger.warning("Gemini model '%s' returned 404 (retired). Rotating to next model...", current_model)
                        await self.resolve_smallest_model(force_refresh=True)
                        continue

                    if status == 429:
                        self._rate_limited_models[current_model] = time.time() + 180  # 3 min cooldown
                        logger.warning("Gemini model '%s' hit rate limit / quota (HTTP 429). Rotating to next model...", current_model)
                        await self.resolve_smallest_model(force_refresh=True)
                        continue

                    if status == 503:
                        self._rate_limited_models[current_model] = time.time() + 60   # 1 min cooldown
                        logger.warning("Gemini model '%s' is overloaded (HTTP 503). Rotating to next model...", current_model)
                        await self.resolve_smallest_model(force_refresh=True)
                        continue

                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise AIProviderError(f"No response candidates returned from Gemini: {data}")
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if not parts:
                        raise AIProviderError("Empty response parts from Gemini")

                    raw_text = parts[0].get("text", "").strip()
                    clean_json = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
                    clean_json = re.sub(r"\s*```$", "", clean_json, flags=re.MULTILINE)

                    usage = data.get("usageMetadata", {})
                    p_tok = usage.get("promptTokenCount", 0)
                    c_tok = usage.get("candidatesTokenCount", 0)
                    t_tok = usage.get("totalTokenCount", p_tok + c_tok)
                    duration_ms = int((time.time() - start_time) * 1000)

                    metrics.record_token_usage(
                        operation=operation,
                        model=current_model,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        duration_ms=duration_ms,
                        item_title=item_title,
                        success=True
                    )

                    return json.loads(clean_json)

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    raise AIProviderError(f"Gemini API Key is invalid or unauthorized (HTTP {status})") from e
                last_error = e
            except (httpx.RequestError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning("Network/timeout error calling Gemini '%s': %s. Retrying...", current_model, e)
            except json.JSONDecodeError as e:
                raise AIProviderError(f"Failed to parse JSON response from Gemini: {raw_text}") from e
            except Exception as e:
                last_error = e
                break

        if last_error:
            metrics.record_token_usage(
                operation=operation,
                model=self._resolved_model or self.model or "gemini",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                duration_ms=0,
                item_title=item_title,
                success=False
            )

        if isinstance(last_error, httpx.HTTPStatusError) and last_error.response.status_code == 429:
            raise AIProviderError("All Gemini candidate models hit rate limit or quota (HTTP 429 / Out of tokens)") from last_error
        raise AIProviderError(f"Failed to get valid response from Gemini after {max_attempts} attempts: {last_error}")

    async def match_unknown_media(
        self,
        release_title: str,
        folder_name: str,
        media_type: str,
        candidates: List[dict]
    ) -> LibraryMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "matched_id": integer or null,\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation of match"\n'
            "}"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Media Type: {media_type}\n"
            f"Candidate Library Items:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
            f"Identify if this release matches any candidate in the library. If no confident match exists, set matched_id to null."
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="match_unknown_media",
            item_title=release_title
        )
        return LibraryMatchResult(
            matched_id=data.get("matched_id"),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )

    async def verify_series_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_series_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "is_match": boolean,\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Series: {candidate_series_title} ({year or 'Unknown Year'})\n"
            f"Alternate Titles: {', '.join(alternate_titles) if alternate_titles else 'None'}\n\n"
            f"Does this release belong to the candidate series? Handle Pinyin, English, and Chinese translations."
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="verify_series_match",
            item_title=release_title
        )
        return MediaMatchResult(
            is_match=bool(data.get("is_match", False)),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )

    async def verify_movie_match(
        self,
        release_title: str,
        folder_name: str,
        candidate_movie_title: str,
        alternate_titles: List[str],
        year: Optional[int] = None
    ) -> MediaMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "is_match": boolean,\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Movie: {candidate_movie_title} ({year or 'Unknown Year'})\n"
            f"Alternate Titles: {', '.join(alternate_titles) if alternate_titles else 'None'}\n\n"
            f"Does this release belong to the candidate movie?"
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="verify_movie_match",
            item_title=release_title
        )
        return MediaMatchResult(
            is_match=bool(data.get("is_match", False)),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )

    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "season_number": integer,\n'
            '  "episode_numbers": [integer],\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        simplified_eps = [
            {"season": e.get("seasonNumber"), "episode": e.get("episodeNumber"), "title": e.get("title")}
            for e in known_episodes_in_season[:100]
        ]
        prompt = (
            f"File Name: {file_name}\n"
            f"Folder Name: {folder_name}\n"
            f"Series Title: {series_title}\n"
            f"Known Seasons: {known_seasons}\n"
            f"Sample Known Episodes in Season:\n{json.dumps(simplified_eps, ensure_ascii=False, indent=2)}\n\n"
            f"Extract the season number and episode number(s) (e.g. for multi-episode files [1, 2])."
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="parse_episode",
            item_title=file_name
        )
        ep_nums = data.get("episode_numbers", [])
        if isinstance(ep_nums, int):
            ep_nums = [ep_nums]
        return EpisodeMatchResult(
            season_number=int(data.get("season_number", 1)),
            episode_numbers=[int(x) for x in ep_nums],
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )

    async def evaluate_sample_or_extra(
        self,
        file_name: str,
        folder_name: str,
        media_title: str
    ) -> SampleEvaluationResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "is_valid_media": boolean,\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        prompt = (
            f"File Name: {file_name}\n"
            f"Folder Name: {folder_name}\n"
            f"Media Title: {media_title}\n\n"
            f"Is this file a valid full media episode/movie file, or is it a disposable sample, trailer, preview, or extra clip?"
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="evaluate_sample_or_extra",
            item_title=file_name
        )
        return SampleEvaluationResult(
            is_valid_media=bool(data.get("is_valid_media", True)),
            confidence=float(data.get("confidence", 1.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )

    async def evaluate_fake_or_malware(
        self,
        release_title: str,
        file_name: str,
        error_messages: List[str],
        archive_files: Optional[List[str]] = None
    ) -> MalwareEvaluationResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "is_fake_or_malware": boolean,\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        archive_section = f"Archive Manifest (Files inside): {', '.join(archive_files[:25])}\n" if archive_files else ""
        prompt = (
            f"Release Title: {release_title}\n"
            f"File Name: {file_name}\n"
            f"{archive_section}"
            f"Error / Status Messages: {', '.join(error_messages) if error_messages else 'None'}\n\n"
            "Task: Determine if this release is a fake, scam, trojan, or malware release pretending to be TV or Movie media.\n\n"
            "Guidelines:\n"
            "- Legitimate scene and P2P TV/movie release groups (-FLUX, -EZTV, -MeGusta, -TGx, etc.) distribute video streams in video containers (.mkv, .mp4, .avi) or multi-part scene RAR archives.\n"
            "- They NEVER package standalone TV/movie releases as '.7z' archives containing Windows executables (.exe, .scr, codec packs, crypted binaries).\n"
            "- If an archive contains executables (.exe, .scr, .bat) and no actual video stream, or has an unsupported extension like .7z masquerading as scene video, classify it as fake/malware."
        )
        data = await self._call_gemini(
            prompt,
            schema,
            operation="evaluate_fake_or_malware",
            item_title=release_title
        )
        return MalwareEvaluationResult(
            is_fake_or_malware=bool(data.get("is_fake_or_malware", False)),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
            model_name=self.name
        )
