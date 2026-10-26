import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
    Stage1TriageResult,
    ApprovedFileItem,
    Stage2DecisionResult,
    BackfillPackMatchResult,
    EpisodeMatchItem,
    BackfillBulkEpisodesMatchResult,
    AIProviderError,
    compact_stage2_context,
    validate_stage1_payload,
    validate_stage2_payload,
    validate_backfill_payload,
    validate_bulk_episodes_payload,
    build_stage3_repair_prompt,
)

logger = logging.getLogger("aimportarr.ai.gemini")


def extract_gemini_error_details(e: Exception) -> Tuple[str, str]:
    """Extract structured (error_code, detailed_reason) from a Gemini exception."""
    if hasattr(e, "response") and getattr(e.response, "status_code", None) is not None:
        status = e.response.status_code
        err_body = getattr(e.response, "text", "")
        try:
            err_json = e.response.json()
            if isinstance(err_json, dict) and "error" in err_json:
                err_obj = err_json["error"]
                if isinstance(err_obj, dict):
                    g_status = err_obj.get("status")  # e.g. "RESOURCE_EXHAUSTED", "INVALID_ARGUMENT"
                    g_msg = err_obj.get("message")
                    code_label = f"HTTP {status} ({g_status})" if g_status else f"HTTP {status}"
                    return code_label, f"Gemini API returned {code_label}: {g_msg or err_body}"
        except Exception:
            pass
        return f"HTTP {status}", f"Gemini API returned HTTP {status}: {err_body or str(e)}"
    err_name = type(e).__name__.lower()
    if "timeout" in err_name:
        return "TIMEOUT", f"Gemini API request timed out: {e}"
    elif "connect" in err_name:
        return "CONNECT_ERROR", f"Failed to connect to Google Gemini API: {e}"
    elif "request" in err_name:
        return "NETWORK_ERROR", f"Network request error contacting Gemini: {e}"
    else:
        err_type = type(e).__name__
        return err_type, f"{err_type}: {e}"


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
            if any(x in name for x in ["image", "tts", "robotics", "clip", "banana", "transcribe", "argon", "computer-use"]):
                continue
            candidates.append(name)

        def model_score(name: str):
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

        if self._rate_limited_models:
            soonest = min(self._rate_limited_models.keys(), key=lambda k: self._rate_limited_models[k])
            logger.warning("All Gemini candidate models are rate-limited. Trying soonest available '%s'...", soonest)
            self._resolved_model = soonest
            return soonest

        fallback = "gemini-3.6-flash"
        self._resolved_model = fallback
        return fallback

    async def _call_gemini_with_raw(
        self,
        prompt: str,
        schema_desc: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> Tuple[Optional[dict], str, Optional[str]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        full_prompt = (
            "You are an expert TV and Movie media parser assistant for the *arr suite (Sonarr/Radarr).\n"
            f"Current Date: {today_str}.\n"
            "Your job is to accurately match messy release filenames, Chinese/Asian Pinyin titles, and foreign characters to official media.\n"
            f"{schema_desc}\n\n"
            f"{prompt}"
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": full_prompt
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }

        max_attempts = 4
        last_error = None
        overall_start_time = time.time()

        for attempt in range(max_attempts):
            if self.model in ("auto", "smallest", ""):
                current_model = await self.resolve_smallest_model()
            else:
                current_model = self._resolved_model or self.model

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
                        self._rate_limited_models[current_model] = time.time() + 180
                        logger.warning("Gemini model '%s' hit rate limit / quota (HTTP 429). Rotating to next model...", current_model)
                        await self.resolve_smallest_model(force_refresh=True)
                        continue

                    if status == 503:
                        self._rate_limited_models[current_model] = time.time() + 60
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
                    if p_tok == 0 and full_prompt:
                        p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                    c_tok = usage.get("candidatesTokenCount", 0)
                    th_tok = usage.get("thoughtsTokenCount", 0)
                    t_tok = usage.get("totalTokenCount", p_tok + c_tok + th_tok)
                    if th_tok == 0 and t_tok > (p_tok + c_tok):
                        th_tok = t_tok - (p_tok + c_tok)
                    duration_ms = int((time.time() - start_time) * 1000)

                    metrics.record_token_usage(
                        operation=operation,
                        model=current_model,
                        prompt_tokens=p_tok,
                        thinking_tokens=th_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        duration_ms=duration_ms,
                        item_title=item_title,
                        success=True,
                        prompt_text=full_prompt,
                        response_text=raw_text,
                    )

                    try:
                        return json.loads(clean_json), raw_text, None
                    except json.JSONDecodeError as e:
                        return None, raw_text, f"Invalid JSON syntax: {e}"

            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status in (401, 403):
                    dur_ms = int((time.time() - overall_start_time) * 1000)
                    err_code, err_msg = extract_gemini_error_details(e)
                    p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
                    metrics.record_token_usage(
                        operation=operation,
                        model=self._resolved_model or self.model,
                        prompt_tokens=p_tok,
                        completion_tokens=0,
                        total_tokens=p_tok,
                        duration_ms=dur_ms,
                        item_title=item_title,
                        success=False,
                        error_code=err_code,
                        prompt_text=full_prompt,
                        response_text=err_msg,
                    )
                    raise AIProviderError(f"Gemini API Key is invalid or unauthorized ({err_code})") from e
                last_error = e
            except (httpx.RequestError, httpx.TimeoutException) as e:
                last_error = e
                logger.warning("Network/timeout error calling Gemini '%s': %s. Retrying...", current_model, e)
            except Exception as e:
                last_error = e
                break

        dur_ms = int((time.time() - overall_start_time) * 1000)
        p_tok = max(1, math.ceil(len(full_prompt) / 3.8))
        if last_error:
            err_code, err_msg = extract_gemini_error_details(last_error)
            metrics.record_token_usage(
                operation=operation,
                model=self._resolved_model or self.model,
                prompt_tokens=p_tok,
                completion_tokens=0,
                total_tokens=p_tok,
                duration_ms=dur_ms,
                item_title=item_title,
                success=False,
                error_code=err_code,
                prompt_text=full_prompt,
                response_text=err_msg,
            )
            raise AIProviderError(f"Gemini API failed after {max_attempts} attempts: {err_msg}") from last_error

        metrics.record_token_usage(
            operation=operation,
            model=self._resolved_model or self.model,
            prompt_tokens=p_tok,
            completion_tokens=0,
            total_tokens=p_tok,
            duration_ms=dur_ms,
            item_title=item_title,
            success=False,
            error_code="RATE_LIMITED_ALL",
            prompt_text=full_prompt,
            response_text="Gemini API failed: all candidate models exhausted or rate-limited",
        )
        raise AIProviderError("Gemini API failed: all candidate models exhausted or rate-limited")

    async def _call_gemini(
        self,
        prompt: str,
        schema_desc: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> dict:
        parsed, raw_text, err = await self._call_gemini_with_raw(
            prompt=prompt,
            schema_desc=schema_desc,
            operation=operation,
            item_title=item_title,
        )
        if err or parsed is None:
            raise AIProviderError(f"Failed to parse JSON response from Gemini: {raw_text}")
        return parsed

    async def _repair_with_stage3(
        self,
        stage_name: str,
        raw_output: str,
        validation_errors: List[str],
        target_schema: str,
        reference_context: Dict[str, Any],
        item_title: Optional[str] = None,
    ) -> dict:
        logger.warning(
            "Stage '%s' output from '%s' failed schema validation (%s). Running Stage 3 AI Output Interpreter...",
            stage_name,
            self.name,
            "; ".join(validation_errors),
        )
        sys_p, usr_p = build_stage3_repair_prompt(
            stage_name=stage_name,
            raw_output=raw_output,
            validation_errors=validation_errors,
            target_schema=target_schema,
            reference_context=reference_context,
        )
        return await self._call_gemini(
            prompt=usr_p,
            schema_desc=sys_p,
            operation="stage3_output_interpreter",
            item_title=item_title,
        )

    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "brief explanation of triage evaluation",\n'
            '  "action": "extract" | "no_extraction" | "reject_delete",\n'
            '  "category_tag": "malware_or_corrupt" | "sample_only" | "archive_extracted" | "triage_passed",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Release Title: {release_title}\n"
            f"Queue Error / Warning Messages: {json.dumps(error_messages, ensure_ascii=False)}\n"
            f"Files on Disk: {json.dumps(disk_files[:30], ensure_ascii=False)}\n"
            f"Archive Detected: {archive_name or 'None'}\n"
            f"Files Inside Archive: {json.dumps((archive_files or [])[:30], ensure_ascii=False)}\n\n"
            "Stage 1 Triage Task:\n"
            "1. Choose 'reject_delete' ONLY if the release is a fake/malware/virus release (e.g. .exe, .scr, .bat, .lnk, .iso, crypted binaries, password-protected scam archive), corrupt/empty archive, or contains ONLY disposable sample/junk files with no real video.\n"
            "2. Choose 'extract' if there is a valid archive containing real video media files that need unpacking.\n"
            "3. Choose 'no_extraction' if valid unpacked video files are already on disk.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_stage1_triage",
            item_title=release_title
        )
        val_errors = [parse_err] if parse_err else validate_stage1_payload(data)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_stage1_triage",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "release_title": release_title,
                    "error_messages": error_messages[:10],
                    "disk_files": disk_files[:15],
                    "archive_name": archive_name,
                },
                item_title=release_title,
            )
            post_errors = validate_stage1_payload(data)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 1 output: {'; '.join(post_errors)}"
                )

        assert isinstance(data, dict)
        return Stage1TriageResult(
            action=str(data.get("action", "no_extraction")),
            category_tag=str(data.get("category_tag", "malware_or_corrupt")),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name
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
        comp_candidates, comp_existing, comp_known = compact_stage2_context(
            manual_import_files=manual_import_files,
            existing_disk_files=existing_disk_files,
            known_episodes=known_episodes,
        )
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "clear step-by-step explanation of identity match, file mapping, and quality upgrade/downgrade check",\n'
            '  "decision": "import" | "reject_keep_in_client" | "reject_delete" | "dismiss_pack",\n'
            '  "matched_media_id": integer or null,\n'
            '  "files_to_import": [\n'
            '    {\n'
            '      "path": "exact file path from candidate files",\n'
            '      "season_number": integer or null,\n'
            '      "episode_numbers": [integer]\n'
            '    }\n'
            '  ],\n'
            '  "category_tag": "id_match" | "unknown_media" | "episode_parsed" | "quality_downgrade" | "wrong_media" | "sample_only" | "malware_or_corrupt" | "season_pack",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Media Type: {media_type} ({'Sonarr TV Series' if media_type == 'series' else 'Radarr Movie'})\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Queue Status Messages: {json.dumps(queue_messages[:15], ensure_ascii=False)}\n"
            f"Candidate Media (if known by *arr): {json.dumps(candidate_media, ensure_ascii=False)}\n"
            f"Library Candidates (if media unknown): {json.dumps((library_candidates or [])[:50], ensure_ascii=False)}\n"
            f"Candidate Files from /manualimport: {json.dumps(comp_candidates, ensure_ascii=False, indent=2)}\n"
            f"Raw Files on Disk (if /manualimport found 0): {json.dumps((raw_disk_files or [])[:30], ensure_ascii=False)}\n"
            f"Existing Imported Files Already on Disk: {json.dumps(comp_existing, ensure_ascii=False)}\n"
            f"Known Episodes in Series (if TV): {json.dumps(comp_known, ensure_ascii=False)}\n\n"
            "Stage 2 Unified Import vs. Reject Evaluation Rules:\n"
            "1. Identity Verification (CRITICAL):\n"
            "   - Verify that the release title genuinely corresponds to Candidate Media (or one of Library Candidates, accounting for aliases, Pinyin, Romaji, and festival/release year +/- 1).\n"
            "   - CAUTION / ANTI-HALLUCINATION WARNING: Do NOT assume a release belongs to Candidate Media simply because Sonarr/Radarr matched it by ID or grab history! Indexers frequently suffer from ID collisions or incorrect grab mappings. If the release title represents a DIFFERENT show or movie (e.g. Release title 'A Love Other Than Yours' vs Candidate Media '100 Days of Deception'), they are two different series! In such cases, if the release does not match any other series in Library Candidates, you MUST choose 'reject_keep_in_client' with category_tag 'wrong_media'. NEVER import a release into Candidate Media if the titles represent distinct works.\n"
            "   - In your 'reasoning', you MUST explicitly justify the title match (explaining the title correspondence, translation, or alias). Merely stating 'matched via grab history' or 'matched by ID' is strictly forbidden as justification for import.\n"
            "2. Upgrade vs. Downgrade Check: Compare each candidate file's quality/resolution and Custom Format score against Existing Imported Files Already on Disk for that movie or episode, as well as any Sonarr/Radarr quality rejection messages.\n"
            "   - If the target episode/movie is ALREADY on disk with equal or higher quality (or Sonarr/Radarr rejected it as 'Existing file on disk has equal or higher quality' or 'Not a Custom Format upgrade'), DO NOT import it.\n"
            "3. Sample / Extra Filtering: Exclude any disposable sample clips or trailers from files_to_import, while keeping genuine full episodes/specials/movies.\n"
            "4. Choose ONE decision:\n"
            "   - 'import': The release matches the target media AND at least one valid video file is either missing on disk or a genuine quality upgrade. Populate matched_media_id and files_to_import (with season_number and episode_numbers for TV series).\n"
            "   - 'reject_keep_in_client': The release is a quality downgrade/not an upgrade over existing disk files, OR is the wrong series/movie or unwanted language. (Preserves files in download client while blocklisting in *arr).\n"
            "   - 'reject_delete': ONLY when the files are virus/malware, 100% disposable samples/trailers with no real media, or corrupt/unusable files. (Deletes from client and blocklists).\n"
            "   - 'dismiss_pack': A multi-episode/season pack where needed episodes were already imported and remaining episodes are missing or already satisfied on disk.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_import_decision",
            item_title=release_title
        )
        val_errors = (
            [parse_err]
            if parse_err
            else validate_stage2_payload(data, candidate_media, library_candidates, manual_import_files)
        )
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_import_decision",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "release_title": release_title,
                    "candidate_media": candidate_media,
                    "library_candidates": (library_candidates or [])[:20],
                    "valid_candidate_files": [
                        {
                            "path": m.get("path"),
                            "name": m.get("name"),
                            "episodes": m.get("episodes", []),
                        }
                        for m in comp_candidates
                    ],
                },
                item_title=release_title,
            )
            post_errors = validate_stage2_payload(data, candidate_media, library_candidates, manual_import_files)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 2 output: {'; '.join(post_errors)}"
                )

        assert isinstance(data, dict)
        files = []
        for f in data.get("files_to_import") or []:
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
            decision=str(data.get("decision", "reject_keep_in_client")),
            matched_media_id=int(data["matched_media_id"]) if data.get("matched_media_id") is not None else None,
            files_to_import=files,
            category_tag=str(data.get("category_tag", "ai_decision")),
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_season_pack(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check series title & aliases; 2. check exact season specification; 3. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Season Number: Season {season_number:02d}\n"
            f"Candidate Season Pack Releases (flagged by Sonarr as 'Unknown Series'):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Series Identity: Verify that the candidate release belongs to Target Series (accounting for alternate titles, Chinese Pinyin, Romaji, foreign titles, or year variations).\n"
            f"2. Season Verification: Candidate release MUST be a full-season pack specifically for Season {season_number:02d} (e.g. 'S{season_number:02d}', 'Season {season_number}', or 'Season {season_number:02d}').\n"
            "   - CAUTION: Reject multi-season packs or complete series packs (e.g. 'S01-S02', 'complete S01 and 2', 'Seasons 1-4', 'Complete Series') when searching for a single target season, as they span multiple seasons or may be misidentified.\n"
            "   - Reject releases belonging to other seasons (e.g. S02, S03).\n"
            f"3. If multiple candidates are valid packs for Season {season_number:02d}, pick the one with highest customFormatScore / qualityWeight / seeders.\n"
            f"4. If none of the candidates are valid packs for Season {season_number:02d}, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        series_label = f"{series_info.get('title', 'Series')} S{season_number:02d}"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_season_pack",
            item_title=series_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_season_pack",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=series_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair backfill output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_episode(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        episode_number: int,
        episode_title: str,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check series title & aliases; 2. verify exact season and episode; 3. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Episode: S{season_number:02d}E{episode_number:02d} ({episode_title})\n"
            f"Candidate Episode Releases (flagged by Sonarr as 'Unknown Series'):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Series Identity: Verify that the candidate release belongs to Target Series (accounting for alternate titles, Chinese Pinyin, Romaji, foreign titles, or year variations).\n"
            f"2. Episode Verification: Candidate release MUST be a genuine episode release specifically for S{season_number:02d}E{episode_number:02d} "
            "(or a multi-episode release containing this episode). Reject releases belonging to different seasons or episodes.\n"
            "3. If multiple candidates match the series and episode, pick the one with the highest customFormatScore / qualityWeight / seeders.\n"
            "4. If none of the candidates actually belong to this series and episode, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        ep_label = f"{series_info.get('title', 'Series')} S{season_number:02d}E{episode_number:02d}"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_episode",
            item_title=ep_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_episode",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=ep_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair episode retry output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_episodes_bulk(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        missing_episodes: List[Dict[str, Any]],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillBulkEpisodesMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "matches": [\n'
            '    {\n'
            '      "episode_number": integer,\n'
            '      "reasoning": "step-by-step rationale for matching or rejecting candidate releases for this episode",\n'
            '      "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '      "confidence": float (0.0 to 1.0)\n'
            '    }\n'
            '  ]\n'
            "}"
        )
        prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Season: Season {season_number:02d}\n"
            f"Missing Episodes to Match: {json.dumps([{'episode_number': e.get('episodeNumber'), 'title': e.get('title')} for e in missing_episodes], ensure_ascii=False)}\n"
            f"Candidate Releases from Season Search:\n{json.dumps(candidate_releases[:30], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. For each missing episode, determine if any candidate release is a genuine release for that episode of the Target Series "
            "(accounting for aliases, Chinese Pinyin, Romaji, and foreign release titles).\n"
            "2. Note: A multi-episode release (e.g. EP01-EP02 or S01E01-E02) may match multiple missing episodes.\n"
            "3. If multiple releases match an episode, pick the one with highest qualityWeight / customFormatScore / seeders.\n"
            "4. If no candidate release matches an episode, set matched_guid to null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        series_label = f"{series_info.get('title', 'Series')} S{season_number:02d} Bulk ({len(missing_episodes)} eps)"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_backfill_episodes_bulk",
            item_title=series_label,
        )
        val_errors = [parse_err] if parse_err else validate_bulk_episodes_payload(data, candidate_releases, missing_episodes)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_backfill_episodes_bulk",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "missing_episodes": missing_episodes,
                    "candidate_releases": candidate_releases[:30],
                },
                item_title=series_label,
            )
            post_errors = validate_bulk_episodes_payload(data, candidate_releases, missing_episodes)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair bulk episode retry output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        matches = [
            EpisodeMatchItem(
                episode_number=int(m["episode_number"]),
                matched_guid=str(m["matched_guid"]) if m.get("matched_guid") else None,
                confidence=float(m.get("confidence", 0.0)),
                reasoning=str(m.get("reasoning", "")),
            )
            for m in (data.get("matches") or [])
            if isinstance(m, dict) and m.get("episode_number") is not None
        ]
        return BackfillBulkEpisodesMatchResult(matches=matches, model_name=self.name)

    async def evaluate_missing_movie(
        self,
        movie_info: Dict[str, Any],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        schema = (
            "Return JSON adhering strictly to this schema:\n"
            "{\n"
            '  "reasoning": "step-by-step analysis: 1. check movie title & aliases; 2. check release year (allow ±1 year for festival/streaming/theatrical variance); 3. verify full movie release (not trailer/sample/OST); 4. quality comparison and conclusion",\n'
            '  "matched_guid": "exact guid from candidate_releases or null if none match",\n'
            '  "confidence": float (0.0 to 1.0)\n'
            "}"
        )
        prompt = (
            f"Target Movie: {json.dumps(movie_info, ensure_ascii=False)}\n"
            f"Candidate Movie Releases (flagged by Radarr as 'Unknown Movie' or rejected):\n"
            f"{json.dumps(candidate_releases[:25], ensure_ascii=False, indent=2)}\n\n"
            "Task:\n"
            "1. Movie Identity: Verify that the candidate release belongs to Target Movie (accounting for alternate titles, original foreign titles, Romaji, Chinese Pinyin, translated names, or year variations ±1 year).\n"
            "2. Content Verification: Candidate release MUST be a full movie release. Reject trailers, samples, soundtracks/OST, featurettes, or different movies.\n"
            "3. If multiple candidates are valid matches for Target Movie, pick the one with highest customFormatScore / qualityWeight / seeders.\n"
            "4. If none of the candidates match Target Movie, return matched_guid: null with confidence 0.0.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{schema}"
        )
        movie_label = f"{movie_info.get('title', 'Movie')} ({movie_info.get('year', '')})"
        data, raw_text, parse_err = await self._call_gemini_with_raw(
            prompt,
            schema,
            operation="evaluate_missing_movie",
            item_title=movie_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(data, candidate_releases)
        if val_errors:
            data = await self._repair_with_stage3(
                stage_name="evaluate_missing_movie",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=schema,
                reference_context={
                    "target_movie": movie_info,
                    "candidate_releases": candidate_releases[:25],
                },
                item_title=movie_label,
            )
            post_errors = validate_backfill_payload(data, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair movie match output: {'; '.join(post_errors)}"
                )
        assert isinstance(data, dict)
        return BackfillPackMatchResult(
            matched_guid=str(data["matched_guid"]) if data.get("matched_guid") else None,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            model_name=self.name,
        )
