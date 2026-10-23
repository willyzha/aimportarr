import json
import logging
import time
from datetime import datetime
from typing import List, Optional, Dict, Any
import httpx
from ..metrics import metrics
from .base import (
    BaseAIProvider,
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
            "'decision' ('import' | 'reject_keep_in_client' | 'reject_delete' | 'dismiss_pack'), "
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
            "2. Check if any existing file on disk already has equal or higher quality/Custom Format score; if downgrade, choose 'reject_keep_in_client'.\n"
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
            decision=str(res.get("decision", "reject_keep_in_client")),
            matched_media_id=int(res["matched_media_id"]) if res.get("matched_media_id") is not None else None,
            files_to_import=files,
            category_tag=str(res.get("category_tag", "ai_decision")),
            confidence=float(res.get("confidence", 0.0)),
            reasoning=str(res.get("reasoning", "")),
            model_name=self.name,
        )
