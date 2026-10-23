import json
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
    MediaMatchResult,
    EpisodeMatchResult,
    SampleEvaluationResult,
    LibraryMatchResult,
    MalwareEvaluationResult,
    Stage1TriageResult,
    ApprovedFileItem,
    Stage2DecisionResult,
    AIProviderError
)

logger = logging.getLogger("aimportarr.ai.ollama")

class OllamaProvider(BaseAIProvider):
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5:1.5b", num_gpu: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_gpu = num_gpu
        self.chat_url = f"{self.base_url}/api/chat"

    @property
    def name(self) -> str:
        return f"Ollama ({self.model})"

    async def _call_ollama(
        self,
        system_prompt: str,
        user_prompt: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> dict:
        options = {"temperature": 0.1}
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "format": "json",
            "stream": False,
            "options": options
        }

        start_time = time.time()
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(self.chat_url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False)
            raise AIProviderError(f"Ollama server returned HTTP {e.response.status_code}: {e.response.text}") from e
        except (httpx.RequestError, httpx.TimeoutException) as e:
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False)
            raise AIProviderError(f"Cannot connect to Ollama at '{self.chat_url}': {e}") from e
        except Exception as e:
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False)
            raise AIProviderError(f"Unexpected error calling Ollama: {e}") from e

        p_tok = data.get("prompt_eval_count", 0)
        c_tok = data.get("eval_count", 0)
        t_tok = p_tok + c_tok
        duration_ns = data.get("total_duration", 0)
        duration_ms = int(duration_ns / 1_000_000) if duration_ns else int((time.time() - start_time) * 1000)

        metrics.record_token_usage(
            operation=operation,
            model=self.model,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            duration_ms=duration_ms,
            item_title=item_title,
            success=True
        )

        message = data.get("message", {})
        raw_text = message.get("content", "{}")
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise AIProviderError(f"Failed to parse Ollama JSON output: {raw_text}") from e

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
        system_prompt = (
            f"You are an expert TV media parser assistant for Sonarr. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: 'is_match' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Series: {candidate_series_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release matches the candidate TV series. "
            "Consider Chinese Pinyin translations, Romaji, and common international media naming conventions.\n\n"
            f"Important Context: Today is {today_str}. Series with release year {current_year} (or recent years) are currently active and being broadcast/streamed now. Do NOT assume that releases from year {current_year} are unreleased or in the future."
        )

        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="verify_series_match",
            item_title=release_title
        )
        return MediaMatchResult(**res, model_name=self.name)

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
        system_prompt = (
            f"You are an expert Movie media parser assistant for Radarr. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: 'is_match' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Candidate Movie: {candidate_movie_title} ({year if year else 'Unknown Year'})\n"
            f"Known Alternate Titles/Aliases: {alternate_titles}\n\n"
            "Task: Determine if this release belongs to the candidate movie in Radarr. "
            "Account for foreign release names, Pinyin, translated movie titles, and release year discrepancies (+/- 1 year between festival and theatrical release).\n\n"
            f"Important Context: Today is {today_str}. Movies with release year {current_year} are currently active in theaters or on digital streaming. Do NOT assume that releases from year {current_year} are unreleased or in the future."
        )

        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="verify_movie_match",
            item_title=release_title
        )
        return MediaMatchResult(**res, model_name=self.name)

    async def parse_episode(
        self,
        file_name: str,
        folder_name: str,
        series_title: str,
        known_seasons: List[int],
        known_episodes_in_season: List[dict]
    ) -> EpisodeMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        system_prompt = (
            f"You are an expert TV media parser assistant for Sonarr. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: "
            "'season_number' (integer or null), 'episode_numbers' (list of integers), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )

        ep_titles = [f"E{e.get('episodeNumber')}: {e.get('title')}" for e in known_episodes_in_season[:30]]

        user_prompt = (
            f"Current Date: {today_str}\n"
            f"Series Title: {series_title}\n"
            f"Folder Name: {folder_name}\n"
            f"File Name: {file_name}\n"
            f"Known Seasons: {known_seasons}\n"
            f"Sample Episodes: {ep_titles}\n\n"
            "Task: Extract the Season Number and Episode Number(s) from this file path. "
            "Look for patterns like 'S01E02', 'EP02', '02.mkv', '第02集', 'Part 1', or episode titles."
        )

        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="parse_episode",
            item_title=file_name
        )
        return EpisodeMatchResult(**res, model_name=self.name)

    async def evaluate_sample_or_extra(
        self,
        file_name: str,
        folder_name: str,
        media_title: str
    ) -> SampleEvaluationResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        system_prompt = (
            f"You are an expert media parser assistant for Sonarr and Radarr. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: "
            "'is_valid_media' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Current Date: {today_str}\n"
            f"Media Title: {media_title}\n"
            f"Folder Name: {folder_name}\n"
            f"File Name: {file_name}\n\n"
            "Task: Determine if this file is a genuine media release (such as an episode, OVA, special, or main feature) "
            "or if it is merely a disposable sample clip/trailer that should be ignored."
        )
        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="evaluate_sample_or_extra",
            item_title=file_name
        )
        return SampleEvaluationResult(**res, model_name=self.name)

    async def match_unknown_media(
        self,
        release_title: str,
        folder_name: str,
        media_type: str,
        candidates: List[dict]
    ) -> LibraryMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        current_year = datetime.now().year
        system_prompt = (
            f"You are an expert {media_type} media parser assistant for the *arr suite. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: "
            "'matched_id' (integer or null), 'matched_title' (string or null), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        candidate_summary = [
            f"ID {c.get('id')}: '{c.get('title')}' ({c.get('year', 'N/A')}) | Aliases: {c.get('aliases', [])}"
            for c in candidates[:50]
        ]
        candidates_str = "\n".join(candidate_summary)

        user_prompt = (
            f"Current Date: {today_str}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Media Type: {media_type}\n\n"
            f"User's Library Candidates:\n{candidates_str}\n\n"
            "Task: Identify which library candidate this release belongs to. "
            "Consider Chinese Pinyin translations, Romaji, alternate titles, and year variations.\n\n"
            f"Important Context: Today is {today_str}. Media with release year {current_year} is currently active and releasing now."
        )
        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="match_unknown_media",
            item_title=release_title
        )
        return LibraryMatchResult(**res, model_name=self.name)

    async def evaluate_fake_or_malware(
        self,
        release_title: str,
        file_name: str,
        error_messages: List[str],
        archive_files: Optional[List[str]] = None
    ) -> MalwareEvaluationResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        system_prompt = (
            f"You are a media security and release verification assistant for Sonarr and Radarr. Current date: {today_str}. "
            "You must respond ONLY with a valid JSON object with keys: "
            "'is_fake_or_malware' (boolean), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        archive_section = f"Archive Manifest (Files inside): {', '.join(archive_files[:25])}\n" if archive_files else ""
        user_prompt = (
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
        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="evaluate_fake_or_malware",
            item_title=release_title
        )
        return MalwareEvaluationResult(**res, model_name=self.name)

    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        system_prompt = (
            f"You are a media security and archive triage assistant for Sonarr and Radarr. Current date: {today_str}. "
            "Respond ONLY with a valid JSON object with keys: "
            "'action' ('extract' | 'no_extraction' | 'reject_delete'), "
            "'category_tag' ('malware_or_corrupt' | 'sample_only' | 'archive_extracted' | 'triage_passed'), "
            "'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Release Title: {release_title}\n"
            f"Queue Messages: {error_messages}\n"
            f"Files on Disk: {disk_files[:30]}\n"
            f"Archive Name: {archive_name or 'None'}\n"
            f"Archive Contents: {(archive_files or [])[:30]}\n\n"
            "Task:\n"
            "- Choose 'reject_delete' ONLY if the release is malware/virus (.exe, .scr, .bat, .lnk, .iso, crypted binaries), corrupt, or sample-only with no real video.\n"
            "- Choose 'extract' if there is a valid archive containing video files.\n"
            "- Choose 'no_extraction' if valid unpacked video files are already on disk."
        )
        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="evaluate_stage1_triage",
            item_title=release_title
        )
        return Stage1TriageResult(
            action=str(res.get("action", "no_extraction")),
            category_tag=str(res.get("category_tag", "malware_or_corrupt")),
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
            model_name=self.name,
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
        today_str = datetime.now().strftime("%Y-%m-%d")
        system_prompt = (
            f"You are an expert media import and quality evaluator for Sonarr and Radarr. Current date: {today_str}. "
            "Respond ONLY with a valid JSON object with keys: "
            "'decision' ('import' | 'reject_keep_seeding' | 'reject_delete' | 'dismiss_pack'), "
            "'matched_media_id' (integer or null), "
            "'files_to_import' (list of objects with 'path', 'season_number', 'episode_numbers'), "
            "'category_tag' (string), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Media Type: {media_type}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Queue Status Messages: {json.dumps(queue_messages, ensure_ascii=False)}\n"
            f"Candidate Media: {json.dumps(candidate_media, ensure_ascii=False)}\n"
            f"Library Candidates: {json.dumps((library_candidates or [])[:40], ensure_ascii=False)}\n"
            f"Candidate Files (/manualimport): {json.dumps(manual_import_files, ensure_ascii=False)}\n"
            f"Raw Files on Disk: {json.dumps((raw_disk_files or [])[:25], ensure_ascii=False)}\n"
            f"Existing Imported Files on Disk: {json.dumps(existing_disk_files[:40], ensure_ascii=False)}\n"
            f"Known Episodes: {json.dumps((known_episodes or [])[:60], ensure_ascii=False)}\n\n"
            "Rules:\n"
            "1. Verify identity against Candidate Media or Library Candidates.\n"
            "2. Check if any existing file on disk already has equal or higher quality/Custom Format score; if downgrade, choose 'reject_keep_seeding'.\n"
            "3. Choose 'reject_delete' ONLY for virus/malware, corrupt files, or 100% disposable sample clips.\n"
            "4. Choose 'dismiss_pack' for partially imported season packs where remaining episodes are already satisfied.\n"
            "5. Choose 'import' and populate matched_media_id and files_to_import (excluding sample clips) if valid and an upgrade/new file."
        )
        res = await self._call_ollama(
            system_prompt,
            user_prompt,
            operation="evaluate_import_decision",
            item_title=release_title
        )
        files = []
        for f in res.get("files_to_import") or []:
            if isinstance(f, dict) and f.get("path"):
                ep_nums = f.get("episode_numbers") or []
                if isinstance(ep_nums, int):
                    ep_nums = [ep_nums]
                files.append(
                    ApprovedFileItem(
                        path=str(f["path"]),
                        season_number=int(f["season_number"]) if f.get("season_number") is not None else None,
                        episode_numbers=[int(x) for x in ep_nums],
                    )
                )
        return Stage2DecisionResult(
            decision=str(res.get("decision", "reject_keep_seeding")),
            matched_media_id=int(res["matched_media_id"]) if res.get("matched_media_id") is not None else None,
            files_to_import=files,
            category_tag=str(res.get("category_tag", "ai_decision")),
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
            model_name=self.name,
        )
