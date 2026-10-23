import json
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any, Tuple
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
    Stage1TriageResult,
    ApprovedFileItem,
    Stage2DecisionResult,
    BackfillPackMatchResult,
    AIProviderError,
    compact_stage2_context,
    validate_stage1_payload,
    validate_stage2_payload,
    validate_backfill_payload,
    build_stage3_repair_prompt,
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

    async def _call_ollama_with_raw(
        self,
        system_prompt: str,
        user_prompt: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None,
    ) -> Tuple[Optional[dict], str, Optional[str]]:
        options = {"temperature": 0.1}
        if self.num_gpu is not None:
            options["num_gpu"] = self.num_gpu

        full_prompt = f"[System]\n{system_prompt}\n\n[User]\n{user_prompt}"
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
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False, prompt_text=full_prompt, response_text=str(e))
            raise AIProviderError(f"Ollama server returned HTTP {e.response.status_code}: {e.response.text}") from e
        except (httpx.RequestError, httpx.TimeoutException) as e:
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False, prompt_text=full_prompt, response_text=str(e))
            raise AIProviderError(f"Cannot connect to Ollama at '{self.chat_url}': {e}") from e
        except Exception as e:
            metrics.record_token_usage(operation=operation, model=self.model, prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=int((time.time() - start_time) * 1000), item_title=item_title, success=False, prompt_text=full_prompt, response_text=str(e))
            raise AIProviderError(f"Unexpected error calling Ollama: {e}") from e

        p_tok = data.get("prompt_eval_count", 0)
        c_tok = data.get("eval_count", 0)
        t_tok = p_tok + c_tok
        duration_ns = data.get("total_duration", 0)
        duration_ms = int(duration_ns / 1_000_000) if duration_ns else int((time.time() - start_time) * 1000)

        message = data.get("message", {})
        raw_text = message.get("content", "{}")

        metrics.record_token_usage(
            operation=operation,
            model=self.model,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            total_tokens=t_tok,
            duration_ms=duration_ms,
            item_title=item_title,
            success=True,
            prompt_text=full_prompt,
            response_text=raw_text,
        )

        try:
            parsed = json.loads(raw_text)
            return parsed, raw_text, None
        except json.JSONDecodeError as e:
            return None, raw_text, f"Invalid JSON syntax: {e}"

    async def _call_ollama(
        self,
        system_prompt: str,
        user_prompt: str,
        operation: str = "ai_query",
        item_title: Optional[str] = None
    ) -> dict:
        parsed, raw_text, err = await self._call_ollama_with_raw(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            operation=operation,
            item_title=item_title,
        )
        if err or parsed is None:
            raise AIProviderError(f"Failed to parse Ollama JSON output: {raw_text}")
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
        repaired = await self._call_ollama(
            sys_p,
            usr_p,
            operation="stage3_output_interpreter",
            item_title=item_title,
        )
        return repaired

    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_schema = (
            "{\n"
            '  "action": "extract" | "no_extraction" | "reject_delete",\n'
            '  "category_tag": "malware_or_corrupt" | "sample_only" | "archive_extracted" | "triage_passed",\n'
            '  "confidence": 0.95,\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        system_prompt = (
            f"You are a media and archive triage assistant for Sonarr and Radarr. Current date: {today_str}. "
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
            "- Choose 'no_extraction' if valid unpacked video files are already on disk.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{target_schema}"
        )
        res, raw_text, parse_err = await self._call_ollama_with_raw(
            system_prompt,
            user_prompt,
            operation="evaluate_stage1_triage",
            item_title=release_title
        )
        val_errors = [parse_err] if parse_err else validate_stage1_payload(res)
        if val_errors:
            res = await self._repair_with_stage3(
                stage_name="evaluate_stage1_triage",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=target_schema,
                reference_context={
                    "release_title": release_title,
                    "error_messages": error_messages[:10],
                    "disk_files": disk_files[:15],
                    "archive_name": archive_name,
                },
                item_title=release_title,
            )
            post_errors = validate_stage1_payload(res)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 1 output: {'; '.join(post_errors)}"
                )

        assert isinstance(res, dict)
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
        comp_candidates, comp_existing, comp_known = compact_stage2_context(
            manual_import_files=manual_import_files,
            existing_disk_files=existing_disk_files,
            known_episodes=known_episodes,
        )
        target_schema = (
            "{\n"
            '  "decision": "import" | "reject_keep_in_client" | "reject_delete" | "dismiss_pack",\n'
            '  "matched_media_id": integer or null (MUST be series/movie id from Candidate Media or Library Candidates, NOT an episode id),\n'
            '  "files_to_import": [{"path": "exact file path string", "season_number": integer or null, "episode_numbers": [integer]}],\n'
            '  "category_tag": "short_snake_case_reason",\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "clear explanation"\n'
            "}"
        )
        system_prompt = (
            f"You are an expert media import and quality evaluator for Sonarr and Radarr. Current date: {today_str}. "
            "Respond ONLY with a valid JSON object with keys: "
            "'decision' ('import' | 'reject_keep_in_client' | 'reject_delete' | 'dismiss_pack'), "
            "'matched_media_id' (integer or null), "
            "'files_to_import' (list of objects with 'path', 'season_number', 'episode_numbers'), "
            "'category_tag' (string), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Media Type: {media_type}\n"
            f"Release Title: {release_title}\n"
            f"Folder Name: {folder_name}\n"
            f"Queue Status Messages: {json.dumps(queue_messages[:10], ensure_ascii=False)}\n"
            f"Candidate Media: {json.dumps(candidate_media, ensure_ascii=False)}\n"
            f"Library Candidates: {json.dumps((library_candidates or [])[:40], ensure_ascii=False)}\n"
            f"Candidate Files (/manualimport): {json.dumps(comp_candidates, ensure_ascii=False)}\n"
            f"Raw Files on Disk: {json.dumps((raw_disk_files or [])[:25], ensure_ascii=False)}\n"
            f"Existing Imported Files on Disk: {json.dumps(comp_existing, ensure_ascii=False)}\n"
            f"Known Episodes: {json.dumps(comp_known, ensure_ascii=False)}\n\n"
            "Rules:\n"
            "1. Verify identity against Candidate Media or Library Candidates.\n"
            "2. Check if any existing file on disk already has equal or higher quality/Custom Format score; if downgrade, choose 'reject_keep_in_client'.\n"
            "3. Choose 'reject_delete' ONLY for virus/malware, corrupt files, or 100% disposable sample clips.\n"
            "4. Choose 'dismiss_pack' for partially imported season packs where remaining episodes are already satisfied.\n"
            "5. Choose 'import' and populate matched_media_id and files_to_import (excluding sample clips) if valid and an upgrade/new file.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA (do NOT use 'action'; use 'decision' and include 'confidence' and 'reasoning'):\n{target_schema}"
        )
        res, raw_text, parse_err = await self._call_ollama_with_raw(
            system_prompt,
            user_prompt,
            operation="evaluate_import_decision",
            item_title=release_title
        )
        val_errors = (
            [parse_err]
            if parse_err
            else validate_stage2_payload(res, candidate_media, library_candidates, manual_import_files)
        )
        if val_errors:
            res = await self._repair_with_stage3(
                stage_name="evaluate_import_decision",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=target_schema,
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
            post_errors = validate_stage2_payload(res, candidate_media, library_candidates, manual_import_files)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair Stage 2 output: {'; '.join(post_errors)}"
                )

        assert isinstance(res, dict)
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
            decision=str(res.get("decision", "reject_keep_in_client")),
            matched_media_id=int(res["matched_media_id"]) if res.get("matched_media_id") is not None else None,
            files_to_import=files,
            category_tag=str(res.get("category_tag", "ai_decision")),
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
            model_name=self.name,
        )

    async def evaluate_backfill_season_pack(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_schema = (
            "{\n"
            '  "matched_guid": "exact guid string from Candidate Releases or null",\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        system_prompt = (
            f"You are an expert TV media release matcher for Sonarr. Current date: {today_str}. "
            "Respond ONLY with a valid JSON object with keys: "
            "'matched_guid' (string or null), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Season Number: Season {season_number:02d}\n"
            f"Candidate Releases: {json.dumps(candidate_releases[:20], ensure_ascii=False)}\n\n"
            "Identify which candidate release (if any) is a valid full-season pack for the Target Series and Season "
            "(considering aliases, Pinyin, Romaji, and foreign titles), preferring higher customFormatScore / qualityWeight / seeders.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{target_schema}"
        )
        series_label = f"{series_info.get('title', 'Series')} S{season_number:02d}"
        res, raw_text, parse_err = await self._call_ollama_with_raw(
            system_prompt,
            user_prompt,
            operation="evaluate_backfill_season_pack",
            item_title=series_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(res, candidate_releases)
        if val_errors:
            res = await self._repair_with_stage3(
                stage_name="evaluate_backfill_season_pack",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=target_schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "candidate_releases": candidate_releases[:20],
                },
                item_title=series_label,
            )
            post_errors = validate_backfill_payload(res, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair backfill output: {'; '.join(post_errors)}"
                )
        assert isinstance(res, dict)
        return BackfillPackMatchResult(
            matched_guid=str(res["matched_guid"]) if res.get("matched_guid") else None,
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
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
        today_str = datetime.now().strftime("%Y-%m-%d")
        target_schema = (
            "{\n"
            '  "matched_guid": "exact guid string from Candidate Releases or null",\n'
            '  "confidence": float (0.0 to 1.0),\n'
            '  "reasoning": "brief explanation"\n'
            "}"
        )
        system_prompt = (
            f"You are an expert TV episode release matcher for Sonarr. Current date: {today_str}. "
            "Respond ONLY with a valid JSON object with keys: "
            "'matched_guid' (string or null), 'confidence' (float 0.0-1.0), and 'reasoning' (string)."
        )
        user_prompt = (
            f"Target Series: {json.dumps(series_info, ensure_ascii=False)}\n"
            f"Target Episode: S{season_number:02d}E{episode_number:02d} ({episode_title})\n"
            f"Candidate Releases: {json.dumps(candidate_releases[:20], ensure_ascii=False)}\n\n"
            f"Identify which candidate release (if any) is a valid episode release for S{season_number:02d}E{episode_number:02d} "
            "of the Target Series (considering aliases, Pinyin, Romaji, and foreign titles), preferring higher customFormatScore / qualityWeight / seeders.\n\n"
            f"REQUIRED JSON OUTPUT SCHEMA:\n{target_schema}"
        )
        ep_label = f"{series_info.get('title', 'Series')} S{season_number:02d}E{episode_number:02d}"
        res, raw_text, parse_err = await self._call_ollama_with_raw(
            system_prompt,
            user_prompt,
            operation="evaluate_backfill_episode",
            item_title=ep_label,
        )
        val_errors = [parse_err] if parse_err else validate_backfill_payload(res, candidate_releases)
        if val_errors:
            res = await self._repair_with_stage3(
                stage_name="evaluate_backfill_episode",
                raw_output=raw_text,
                validation_errors=val_errors,
                target_schema=target_schema,
                reference_context={
                    "target_series": series_info,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "candidate_releases": candidate_releases[:20],
                },
                item_title=ep_label,
            )
            post_errors = validate_backfill_payload(res, candidate_releases)
            if post_errors:
                raise AIProviderError(
                    f"Stage 3 AI Output Interpreter failed to repair episode retry output: {'; '.join(post_errors)}"
                )
        assert isinstance(res, dict)
        return BackfillPackMatchResult(
            matched_guid=str(res["matched_guid"]) if res.get("matched_guid") else None,
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
            model_name=self.name,
        )
