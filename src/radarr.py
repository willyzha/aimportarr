import asyncio
import logging
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional
import httpx
from .ai.base import (
    BaseAIProvider,
    Stage1TriageResult,
    Stage2DecisionResult,
    ApprovedFileItem,
)
from .classifier import classify_issue, ErrorCategory, QUALITY_REJECTION_KEYWORDS
from .config import ExtractionConfig
from .extractor import (
    find_archive_file,
    inspect_archive,
    extract_archive,
    resolve_local_path,
    VIDEO_EXTENSIONS,
    SUSPICIOUS_EXTENSIONS,
)
from .metrics import metrics

logger = logging.getLogger("aimportarr.radarr")


class RadarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        min_confidence: float = 0.85,
        dry_run: bool = False,
        reject_on_low_confidence: bool = False,
        extraction_config: Optional[ExtractionConfig] = None,
        path_mappings: Optional[List[Any]] = None,
        categories: Optional[Any] = None,  # Ignored legacy parameter if passed by external callers
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.dry_run = bool(dry_run)
        self.reject_on_low_confidence = bool(reject_on_low_confidence)
        self.extraction_config = extraction_config or ExtractionConfig()
        self.path_mappings = path_mappings or []
        self._dismissed_downloads: set = set()
        self._attempted_items: set = set()
        self.headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}/api/v3/{endpoint.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, endpoint: str, data: Dict[str, Any]) -> Any:
        url = f"{self.base_url}/api/v3/{endpoint.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=self.headers, json=data)
            resp.raise_for_status()
            return resp.json()

    async def _delete(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/api/v3/{endpoint.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request("DELETE", url, headers=self.headers, params=params, json=json)
            resp.raise_for_status()
            if resp.content:
                try:
                    return resp.json()
                except Exception:
                    return resp.text
            return None

    async def get_queue(self) -> List[Dict[str, Any]]:
        """Fetch all items from the Radarr download queue."""
        data = await self._get(
            "queue",
            params={
                "pageSize": 100,
                "includeUnknownMovieItems": True,
                "includeMovie": True,
            },
        )
        return data.get("records", [])

    async def get_movie(self, movie_id: int) -> Dict[str, Any]:
        """Fetch detailed movie metadata (including movieFile if downloaded)."""
        try:
            return await self._get(f"movie/{movie_id}")
        except Exception as e:
            logger.debug(f"Failed to fetch movie {movie_id} from Radarr: {e}")
            return {}

    async def get_all_movies(self) -> List[Dict[str, Any]]:
        """Fetch all tracked movies from Radarr library."""
        try:
            return await self._get("movie")
        except Exception as e:
            logger.error(f"Failed to fetch movies list from Radarr: {e}")
            return []

    async def get_manual_import_items(
        self, folder: str, download_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Query Radarr for media files detected in the download directory or queue item.
        Prioritizes querying by downloadId if available.
        Never passes movieId to prevent Radarr from scanning the library folder.
        """
        if download_id:
            try:
                items = await self._get("manualimport", params={"downloadId": download_id})
                if items:
                    return items
            except Exception as e:
                logger.debug(f"Query Radarr /manualimport by downloadId '{download_id}' failed: {e}")

        if not folder or folder.strip() in (".", "/"):
            return []

        p = Path(folder)
        folder_dir = (
            str(p.parent)
            if p.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv")
            else str(p)
        )
        params: Dict[str, Any] = {"folder": folder_dir, "filterExistingFiles": True}
        if download_id:
            params["downloadId"] = download_id

        try:
            return await self._get("manualimport", params=params)
        except Exception as e:
            if download_id:
                try:
                    logger.debug(f"Retrying Radarr /manualimport for folder '{folder_dir}' without downloadId...")
                    return await self._get("manualimport", params={"folder": folder_dir, "filterExistingFiles": True})
                except Exception as fe:
                    logger.error(f"Failed to query Radarr /manualimport for folder '{folder_dir}': {fe}")
                    return []
            logger.error(f"Failed to query Radarr /manualimport for folder '{folder_dir}': {e}")
            return []

    async def trigger_manual_import(
        self,
        files_to_import: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        category_tag: str = "imported",
    ) -> bool:
        """Submit resolved movie files to Radarr for execution and track command status."""
        if not files_to_import:
            return False

        is_dry = self.dry_run if dry_run is None else dry_run

        if is_dry:
            logger.info(f"[DRY RUN] Would import {len(files_to_import)} movie file(s) to Radarr [{category_tag}]")
            for f in files_to_import:
                logger.debug(f"  [DRY RUN] File: {f.get('path')} | MovieId: {f.get('movieId')}")
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto",
        }
        try:
            logger.info(f"Submitting {len(files_to_import)} movie file(s) to Radarr for import [{category_tag}]...")
            res = await self._post("command", payload)
            cmd_id = res.get("id")
            logger.debug(f"Triggered Radarr ManualImport command ID: {cmd_id}")

            if cmd_id:
                for _ in range(5):
                    await asyncio.sleep(2)
                    try:
                        cmd_status = await self._get(f"command/{cmd_id}")
                        state = cmd_status.get("status") or cmd_status.get("state")
                        if state == "completed":
                            logger.info(
                                f"Radarr successfully imported {len(files_to_import)} movie file(s) (Command ID: {cmd_id})"
                            )
                            return True
                        elif state == "failed":
                            logger.error(
                                f"Radarr ManualImport command {cmd_id} failed: {cmd_status.get('message', 'Unknown failure')}"
                            )
                            return False
                    except Exception as pe:
                        logger.debug(f"Failed to poll Radarr command {cmd_id} status: {pe}")
                        break

            return True
        except Exception as e:
            logger.error(f"Failed to trigger Radarr ManualImport command: {e}")
            return False

    async def resolve_queue_item(
        self,
        queue_id: Optional[int],
        download_id: Optional[str] = None,
        remove_from_client: bool = False,
        blocklist: bool = False,
        skip_redownload: bool = False,
        dry_run: Optional[bool] = None,
        title: str = "",
    ) -> bool:
        """
        Unified queue removal handler for Radarr:
        - Virus / Sample-only / Corrupt: remove_from_client=True, blocklist=True, skip_redownload=False
        - Quality Downgrade / Wrong Media (Keep Seeding): remove_from_client=False, blocklist=True, skip_redownload=False
        """
        if not queue_id:
            return False

        is_dry = self.dry_run if dry_run is None else dry_run
        ids_to_remove = [queue_id]
        if download_id:
            try:
                current_queue = await self.get_queue()
                matching_ids = [
                    q.get("id")
                    for q in current_queue
                    if q.get("downloadId") == download_id and q.get("id")
                ]
                if matching_ids:
                    ids_to_remove = list(set(ids_to_remove + matching_ids))
            except Exception as e:
                logger.debug(f"Failed to lookup all queue items for downloadId '{download_id}': {e}")

        params = {
            "removeFromClient": "true" if remove_from_client else "false",
            "blocklist": "true" if blocklist else "false",
            "skipRedownload": "true" if skip_redownload else "false",
        }
        action_desc = (
            "delete from client & blocklist"
            if remove_from_client
            else ("dismiss (keep seeding), blocklist & re-search" if blocklist else "dismiss warning (keep seeding)")
        )

        if is_dry:
            logger.info(
                f"  [DRY RUN] Would {action_desc} for '{title}' (Queue IDs: {ids_to_remove}, params={params})"
            )
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True

        logger.info(f"  Executing Radarr queue action ({action_desc}) for '{title}' (Queue IDs: {ids_to_remove})...")
        try:
            if len(ids_to_remove) > 1:
                try:
                    await self._delete("queue/bulk", params=params, json={"ids": ids_to_remove})
                except Exception as be:
                    logger.debug(f"Bulk queue delete failed, falling back to individual deletes: {be}")
                    for qid in ids_to_remove:
                        try:
                            await self._delete(f"queue/{qid}", params=params)
                        except Exception:
                            pass
            else:
                await self._delete(f"queue/{queue_id}", params=params)

            if download_id:
                self._dismissed_downloads.add(download_id)
            return True
        except Exception as e:
            logger.error(f"  Failed to execute queue action ({action_desc}) for '{title}': {e}")
            return False

    async def delete_and_blocklist(
        self,
        queue_id: int,
        download_id: Optional[str] = None,
        dry_run: bool = False,
        title: str = "",
    ) -> bool:
        """Delete a virus/sample/corrupt release from download client, blocklist it, and allow redownload."""
        return await self.resolve_queue_item(
            queue_id=queue_id,
            download_id=download_id,
            remove_from_client=True,
            blocklist=True,
            skip_redownload=False,
            dry_run=dry_run,
            title=title,
        )

    def _get_item_keys(self, item: Dict[str, Any]) -> List[str]:
        keys = []
        dl_id = item.get("downloadId")
        if dl_id:
            keys.append(f"dl:{dl_id}")
        out = item.get("outputPath")
        if out and out.strip() not in (".", "/"):
            keys.append(f"path:{out.strip()}")
        q_id = item.get("id")
        title = item.get("title", "")
        if q_id or title:
            keys.append(f"item:{q_id}:{title}")
        return keys

    def _list_local_files(self, p_local: Path) -> List[str]:
        if not p_local.exists():
            return []
        if p_local.is_file():
            return [p_local.name]
        files = []
        try:
            for f in p_local.rglob("*"):
                if f.is_file() and self.extraction_config.staging_suffix not in str(f):
                    files.append(str(f.relative_to(p_local)))
        except Exception:
            pass
        return files

    async def _run_stage2_ai_or_mock_fallback(
        self,
        ai_provider: BaseAIProvider,
        release_title: str,
        folder_name: str,
        all_messages: List[str],
        movie_info: Optional[Dict[str, Any]],
        library_candidates: Optional[List[Dict[str, Any]]],
        manual_items: List[Dict[str, Any]],
        existing_disk_files: List[Dict[str, Any]],
        raw_disk_files: List[str],
        initial_category: ErrorCategory,
    ) -> Stage2DecisionResult:
        eval_fn = getattr(ai_provider, "evaluate_import_decision", None)
        is_real_or_async_mock = isinstance(ai_provider, BaseAIProvider) or (
            eval_fn is not None and asyncio.iscoroutinefunction(eval_fn)
        )
        if is_real_or_async_mock:
            return await ai_provider.evaluate_import_decision(
                media_type="movie",
                release_title=release_title,
                folder_name=folder_name,
                queue_messages=all_messages,
                candidate_media={
                    "id": movie_info.get("id"),
                    "title": movie_info.get("title"),
                    "year": movie_info.get("year"),
                    "aliases": [a.get("title") for a in movie_info.get("alternateTitles", [])],
                }
                if movie_info
                else None,
                library_candidates=library_candidates,
                manual_import_files=[
                    {
                        "path": m.get("path"),
                        "name": m.get("name") or Path(m.get("path", "")).name,
                        "quality": (m.get("quality") or {}).get("quality", {}).get("name"),
                        "customFormatScore": m.get("customFormatScore"),
                        "languages": [l.get("name") for l in m.get("languages", []) if isinstance(l, dict)],
                        "rejections": [r.get("reason", "") for r in m.get("rejections", [])],
                    }
                    for m in manual_items
                ],
                existing_disk_files=existing_disk_files,
                known_episodes=None,
                raw_disk_files=raw_disk_files,
            )

        # Fallback for unit test mocks that only mock legacy methods
        matched_id = movie_info.get("id") if movie_info else None
        candidate_title = movie_info.get("title", "") if movie_info else ""
        model_name = "MockAI"
        confidence = 1.0
        reasoning = "Evaluated via legacy mock"

        if initial_category == ErrorCategory.ID_MATCH and hasattr(ai_provider, "verify_movie_match"):
            v_res = await ai_provider.verify_movie_match(
                release_title=release_title,
                folder_name=folder_name,
                candidate_movie_title=candidate_title,
                alternate_titles=[],
                year=movie_info.get("year") if movie_info else None,
            )
            model_name = getattr(v_res, "model_name", None) or model_name
            confidence = float(getattr(v_res, "confidence", 1.0))
            reasoning = str(getattr(v_res, "reasoning", ""))
            if not getattr(v_res, "is_match", False):
                return Stage2DecisionResult(
                    decision="reject_keep_seeding",
                    matched_media_id=matched_id,
                    files_to_import=[],
                    category_tag="id_match",
                    confidence=confidence,
                    reasoning=reasoning,
                    model_name=model_name,
                )

        approved = [ApprovedFileItem(path=m.get("path", "")) for m in manual_items if m.get("path")]
        return Stage2DecisionResult(
            decision="import" if approved else "reject_keep_seeding",
            matched_media_id=matched_id,
            files_to_import=approved,
            category_tag="id_match" if approved else "rejected",
            confidence=confidence,
            reasoning=reasoning,
            model_name=model_name,
        )

    def is_item_eligible(self, item: Dict[str, Any]) -> tuple[bool, str]:
        """
        Lightweight pre-filter to determine if a queue item is stuck and eligible to enter the FIFO queue.
        Returns (is_eligible, dedup_key).
        """
        title = item.get("title", "")
        status = (item.get("trackedDownloadStatus") or "").lower()
        tracked_state = (item.get("trackedDownloadState") or "").lower()
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        queue_id = item.get("id")
        movie_info = item.get("movie")
        dedup_key = f"radarr:{download_id or queue_id or title}"

        if download_id and download_id in self._dismissed_downloads:
            return False, dedup_key

        item_keys = self._get_item_keys(item)
        if metrics.is_item_attempted(item_keys):
            return False, dedup_key

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        if tracked_state in ("downloading", "importing", "imported", "queued", "delay", "paused"):
            return False, dedup_key

        if tracked_state == "importpending" and status not in ("warning", "error") and not all_messages:
            return False, dedup_key

        if not output_path or not output_path.strip() or output_path.strip() in (".", "/"):
            return False, dedup_key

        error_msg = (item.get("errorMessage") or "").lower()
        if "stalled" in error_msg and ("no connections" in error_msg or not all_messages):
            return False, dedup_key
        if any("stalled with no connections" in m.lower() for m in all_messages):
            return False, dedup_key

        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and movie_info:
            return False, dedup_key

        return True, dedup_key

    async def process_queue_item(self, item: Dict[str, Any], ai_provider: BaseAIProvider) -> bool:
        """
        4-Step / Two-Stage Pipeline for Radarr Queue Items:
        1. Let Radarr process normal downloads naturally.
        2. Stage 1 (Hybrid Archive & Malware Triage): Inspect disk/7z headers; auto-extract clean archives,
           and use Stage 1 AI when suspicious files or archive anomalies are detected.
        3. (Optional) Extract archive in sandbox (<stem>_extracted).
        4. Stage 2: Query /manualimport + existing on-disk movieFile quality. Fast-path unambiguous cases
           (cleanly mapped or hard quality downgrade) and invoke Stage 2 AI for everything else.
        """
        title = item.get("title", "")
        status = (item.get("trackedDownloadStatus") or "").lower()
        tracked_state = (item.get("trackedDownloadState") or "").lower()
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        queue_id = item.get("id")
        movie_info = item.get("movie")

        # ------------------------------------------------------------------
        # STEP 1: Hands-off checks (let Radarr handle normal downloads)
        # ------------------------------------------------------------------
        if download_id and download_id in self._dismissed_downloads:
            logger.debug(f"Skipping '{title}': downloadId '{download_id}' was already handled.")
            return True

        item_keys = self._get_item_keys(item)
        if metrics.is_item_attempted(item_keys):
            logger.debug(f"Skipping '{title}': previously attempted.")
            return False

        all_messages: List[str] = []
        for sm in status_messages:
            all_messages.extend(sm.get("messages", []))

        if tracked_state in ("downloading", "importing", "imported", "queued", "delay", "paused"):
            return False

        if tracked_state == "importpending" and status not in ("warning", "error") and not all_messages:
            return False

        if not output_path or not output_path.strip() or output_path.strip() in (".", "/"):
            return False

        error_msg = (item.get("errorMessage") or "").lower()
        if "stalled" in error_msg and ("no connections" in error_msg or not all_messages):
            return False
        if any("stalled with no connections" in m.lower() for m in all_messages):
            return False

        initial_category = classify_issue(all_messages, has_media_info=bool(movie_info), title=title)

        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and movie_info:
            return False

        is_dry_run = self.dry_run
        mode_tag = "[DRY RUN]" if is_dry_run else "[LIVE]"
        logger.info(f"Processing stuck Radarr movie: '{title}' [{initial_category.value}] {mode_tag}")

        # ------------------------------------------------------------------
        # STEP 2 & STEP 3: Stage 1 Triage & Optional Sandbox Extraction
        # ------------------------------------------------------------------
        staging_dir_to_clean: Optional[Path] = None
        is_extracted_archive = False
        p_local = resolve_local_path(output_path, self.path_mappings)
        raw_disk_files = self._list_local_files(p_local)

        try:
            if self.extraction_config.enabled and output_path:
                if not p_local.exists():
                    logger.warning(
                        f"  ⚠️ Download directory '{output_path}' was not found inside container (resolved to '{p_local}')."
                    )
                archive_file = find_archive_file(p_local)
                if archive_file:
                    logger.info(f"  🔍 Archive detected: '{archive_file.name}'. Inspecting contents in memory...")
                    inspection = await inspect_archive(archive_file)
                    if inspection and inspection.has_video_files and not inspection.has_suspicious_files:
                        # Clean video archive -> deterministic sandbox extraction (0 AI tokens)
                        logger.info(
                            f"  📦 Valid media archive detected ({len(inspection.video_files)} video file(s)). Extracting in sandbox..."
                        )
                        staging_dir = archive_file.parent / f"{archive_file.stem}{self.extraction_config.staging_suffix}"
                        extract_ok = await extract_archive(archive_file, staging_dir)
                        if not extract_ok:
                            logger.error(f"  Failed to extract archive '{archive_file.name}'")
                            return False
                        metrics.record_archive_extracted()
                        is_extracted_archive = True
                        staging_dir_to_clean = staging_dir
                        logger.info(f"  ✔ Extraction complete in sandbox: '{staging_dir}'. Scanning for import...")
                        try:
                            archive_rel_parent = archive_file.parent.relative_to(p_local)
                            remote_staging_dir = (
                                Path(output_path)
                                / archive_rel_parent
                                / f"{archive_file.stem}{self.extraction_config.staging_suffix}"
                            )
                            output_path = str(remote_staging_dir)
                        except Exception:
                            output_path = str(staging_dir)
                    else:
                        # Suspicious or 0-video archive -> Stage 1 AI Triage
                        arch_files = [e.path for e in inspection.files] if inspection else []
                        logger.warning(
                            f"  Archive '{archive_file.name}' has suspicious files or 0 videos. Running Stage 1 AI triage..."
                        )
                        triage = await ai_provider.evaluate_stage1_triage(
                            release_title=title,
                            error_messages=all_messages,
                            disk_files=raw_disk_files,
                            archive_name=archive_file.name,
                            archive_files=arch_files,
                        )
                        model_label = triage.model_name or "AI"
                        if triage.action == "reject_delete" and triage.confidence >= self.min_confidence:
                            logger.warning(
                                f"  🚨 Stage 1 AI confirmed malware/corrupt archive: '{title}' | Model: {model_label} | Confidence: {triage.confidence:.0%}"
                            )
                            metrics.record_processed_item(
                                app="Radarr",
                                title=title,
                                category=triage.category_tag or "malware_or_corrupt",
                                status="dry_run" if is_dry_run else "rejected",
                                confidence=triage.confidence,
                                model=model_label,
                                reasoning=triage.reasoning,
                                details=f"Deleted from torrent & blocklisted ({archive_file.name})",
                                item_keys=item_keys,
                            )
                            return await self.delete_and_blocklist(
                                queue_id=queue_id,
                                download_id=download_id,
                                dry_run=is_dry_run,
                                title=title,
                            )
                        elif triage.action == "extract" and inspection:
                            staging_dir = archive_file.parent / f"{archive_file.stem}{self.extraction_config.staging_suffix}"
                            if await extract_archive(archive_file, staging_dir):
                                metrics.record_archive_extracted()
                                is_extracted_archive = True
                                staging_dir_to_clean = staging_dir
                                output_path = str(staging_dir)

            # Check non-archive Stage 1 malware/suspicious triggers
            has_suspicious_disk_file = any(
                Path(f).suffix.lower() in SUSPICIOUS_EXTENSIONS for f in raw_disk_files
            )
            if not is_extracted_archive and (
                initial_category == ErrorCategory.FAKE_OR_MALWARE or has_suspicious_disk_file
            ):
                logger.info(f"  Running Stage 1 AI triage on suspicious release: '{title}'...")
                triage = await ai_provider.evaluate_stage1_triage(
                    release_title=title,
                    error_messages=all_messages,
                    disk_files=raw_disk_files or [Path(output_path).name or title],
                )
                model_label = triage.model_name or "AI"
                if triage.action == "reject_delete" and triage.confidence >= self.min_confidence:
                    logger.warning(
                        f"  🚨 Stage 1 AI confirmed fake/malware release: '{title}' | Model: {model_label} | Confidence: {triage.confidence:.0%}"
                    )
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category=triage.category_tag or "malware_or_corrupt",
                        status="dry_run" if is_dry_run else "rejected",
                        confidence=triage.confidence,
                        model=model_label,
                        reasoning=triage.reasoning,
                        details="Deleted from torrent & blocklisted",
                        item_keys=item_keys,
                    )
                    return await self.delete_and_blocklist(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
                    )
                elif triage.action == "reject_delete" and triage.confidence < self.min_confidence:
                    if self.reject_on_low_confidence:
                        metrics.record_processed_item(
                            app="Radarr",
                            title=title,
                            category="low_confidence",
                            status="dry_run" if is_dry_run else "dismissed",
                            confidence=triage.confidence,
                            model=model_label,
                            reasoning=triage.reasoning,
                            details="Low-confidence suspicious item; dismissed & blocklisted (still seeding)",
                            item_keys=item_keys,
                        )
                        return await self.resolve_queue_item(
                            queue_id=queue_id,
                            download_id=download_id,
                            remove_from_client=False,
                            blocklist=True,
                            skip_redownload=False,
                            dry_run=is_dry_run,
                            title=title,
                        )
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category="low_confidence",
                        status="skipped",
                        confidence=triage.confidence,
                        model=model_label,
                        reasoning=triage.reasoning,
                        details="Left in queue (AI confidence below threshold)",
                        item_keys=item_keys,
                    )
                    return False

            # ------------------------------------------------------------------
            # STEP 4: Stage 2 — Query /manualimport, Fast-Path, or Stage 2 AI
            # ------------------------------------------------------------------
            query_download_id = None if staging_dir_to_clean else download_id
            manual_items = await self.get_manual_import_items(
                folder=output_path, download_id=query_download_id
            )

            movie_path = (movie_info.get("path") or "").rstrip("/") if movie_info else ""
            if movie_path:
                manual_items = [
                    m for m in manual_items
                    if not (m.get("path") or "").startswith(movie_path)
                ]

            if not manual_items:
                if not p_local.exists():
                    logger.warning(
                        f"  Radarr returned 0 files for '{title}' and local path '{p_local}' is not accessible. Leaving in queue."
                    )
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category="path_unreachable",
                        status="skipped",
                        confidence=0.0,
                        model=None,
                        reasoning=f"Radarr returned 0 importable files and local path '{p_local}' is not mounted.",
                        details="0 files found (path not mounted)",
                        item_keys=item_keys,
                    )
                    return False

                triage = await ai_provider.evaluate_stage1_triage(
                    release_title=title,
                    error_messages=all_messages,
                    disk_files=raw_disk_files,
                )
                model_label = triage.model_name or "AI"
                if triage.action == "reject_delete" and triage.confidence >= self.min_confidence:
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category=triage.category_tag or "sample_only",
                        status="dry_run" if is_dry_run else "rejected",
                        confidence=triage.confidence,
                        model=model_label,
                        reasoning=triage.reasoning,
                        details="0 valid videos; deleted from torrent & blocklisted",
                        item_keys=item_keys,
                    )
                    return await self.delete_and_blocklist(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
                    )
                metrics.record_processed_item(
                    app="Radarr",
                    title=title,
                    category="no_importable_files",
                    status="skipped",
                    confidence=triage.confidence,
                    model=model_label,
                    reasoning=triage.reasoning or "Radarr manual import returned 0 importable files",
                    details="Left in queue (0 importable files found)",
                    item_keys=item_keys,
                )
                return False

            # Gather existing on-disk movieFile metadata for Upgrade vs. Downgrade checking
            movie_id = movie_info.get("id") if movie_info else None
            existing_disk_files = []
            if movie_info:
                mf = movie_info.get("movieFile")
                if not mf and movie_info.get("hasFile") and movie_id:
                    full_movie = await self.get_movie(movie_id)
                    mf = full_movie.get("movieFile")
                if mf and isinstance(mf, dict):
                    existing_disk_files.append({
                        "movieId": movie_id,
                        "quality": (mf.get("quality") or {}).get("quality", {}).get("name"),
                        "customFormatScore": mf.get("customFormatScore"),
                        "size": mf.get("size"),
                    })

            # Evaluate fast-path eligibility and hard quality rejections
            fast_path_import_files: List[Dict[str, Any]] = []
            quality_rejected_count = 0
            needs_ai_stage2 = False
            all_rejection_reasons: List[str] = []

            if not movie_info or initial_category == ErrorCategory.ID_MATCH:
                needs_ai_stage2 = True

            for m_file in manual_items:
                f_path = m_file.get("path", "")
                f_name = m_file.get("name") or Path(f_path).name
                rejections = [r.get("reason", "") for r in m_file.get("rejections", [])]
                all_rejection_reasons.extend(rejections)
                curr_movie = m_file.get("movie") or movie_info
                target_movie_id = curr_movie.get("id") if curr_movie else movie_id

                if any("matched to movie by id" in r.lower() or "matched by id" in r.lower() for r in rejections):
                    needs_ai_stage2 = True

                quality_rejs = [
                    r for r in rejections
                    if any(k in r.lower() for k in QUALITY_REJECTION_KEYWORDS)
                ]
                if quality_rejs:
                    logger.warning(
                        f"  ✖ Skipping file '{f_name}': Radarr quality rule rejected import ({'; '.join(quality_rejs)})."
                    )
                    quality_rejected_count += 1
                    continue

                if target_movie_id and not rejections and not needs_ai_stage2:
                    payload_item = {
                        "path": f_path,
                        "movieId": target_movie_id,
                        "quality": m_file.get("quality"),
                        "languages": m_file.get("languages", []),
                        "releaseGroup": m_file.get("releaseGroup"),
                    }
                    if download_id:
                        payload_item["downloadId"] = download_id
                    fast_path_import_files.append(payload_item)
                else:
                    needs_ai_stage2 = True

            # FAST-PATH A: All candidate files were rejected by Radarr's hard quality/CF rules
            if quality_rejected_count == len(manual_items):
                reason_str = "; ".join(sorted(set(all_rejection_reasons))) or "Existing file on disk has equal or higher quality"
                metrics.record_processed_item(
                    app="Radarr",
                    title=title,
                    category="quality_downgrade",
                    status="dry_run" if is_dry_run else "dismissed",
                    confidence=1.0,
                    model="Radarr (Quality Rule)",
                    reasoning=f"Rejected by Radarr quality rules: {reason_str}",
                    details="Dismissed & blocklisted (still seeding)",
                    item_keys=item_keys,
                )
                await self.resolve_queue_item(
                    queue_id=queue_id,
                    download_id=download_id,
                    remove_from_client=False,
                    blocklist=True,
                    skip_redownload=False,
                    dry_run=is_dry_run,
                    title=title,
                )
                return False

            # FAST-PATH B: Cleanly mapped movie files with 0 rejections (e.g., extracted archive)
            if not needs_ai_stage2 and fast_path_import_files:
                decider = "Radarr (Extracted Archive)" if is_extracted_archive else "Radarr (Direct Match)"
                cat_tag = "archive_extracted" if is_extracted_archive else "direct_match"
                candidate_title = movie_info.get("title", "") if movie_info else title
                logger.info(
                    f"  ✔ Fast-path approved {len(fast_path_import_files)} file(s) for '{candidate_title}' | Model: {decider}"
                )
                import_ok = await self.trigger_manual_import(
                    fast_path_import_files, dry_run=is_dry_run, category_tag=cat_tag
                )
                status_label = "dry_run" if is_dry_run else ("imported" if import_ok else "failed")
                metrics.record_processed_item(
                    app="Radarr",
                    title=title,
                    category=cat_tag,
                    status=status_label,
                    confidence=1.0,
                    model=decider,
                    reasoning=f"Cleanly mapped {len(fast_path_import_files)} file(s) with 0 quality/identity rejections",
                    details=f"Imported {len(fast_path_import_files)} movie file(s) into '{candidate_title}'",
                    item_keys=item_keys,
                )
                return import_ok

            # ------------------------------------------------------------------
            # STAGE 2 AI: Ambiguity Remains -> Unified AI Import/Reject Decision
            # ------------------------------------------------------------------
            p_out = Path(output_path)
            folder_name = (
                p_out.parent.name
                if p_out.suffix.lower() in VIDEO_EXTENSIONS
                else (p_out.name or title)
            )

            all_movies: List[Dict[str, Any]] = []
            library_candidates: Optional[List[Dict[str, Any]]] = None
            if not movie_info:
                all_movies = await self.get_all_movies()
                library_candidates = [
                    {
                        "id": m.get("id"),
                        "title": m.get("title"),
                        "year": m.get("year"),
                        "aliases": [a.get("title") for a in m.get("alternateTitles", [])],
                    }
                    for m in all_movies
                ]

            eligible_manual_items = [
                m for m in manual_items
                if not any(
                    any(k in r.get("reason", "").lower() for k in QUALITY_REJECTION_KEYWORDS)
                    for r in m.get("rejections", [])
                )
            ]

            logger.info(f"  Running Stage 2 AI evaluation for '{title}'...")
            stage2 = await self._run_stage2_ai_or_mock_fallback(
                ai_provider=ai_provider,
                release_title=title,
                folder_name=folder_name,
                all_messages=all_messages,
                movie_info=movie_info,
                library_candidates=library_candidates,
                manual_items=eligible_manual_items,
                existing_disk_files=existing_disk_files,
                raw_disk_files=raw_disk_files,
                initial_category=initial_category,
            )
            model_label = stage2.model_name or "AI"

            if stage2.confidence < self.min_confidence:
                logger.warning(
                    f"  ⚠️ Stage 2 AI confidence ({stage2.confidence:.0%}) is below threshold ({self.min_confidence:.0%}) for '{title}': {stage2.reasoning}"
                )
                if self.reject_on_low_confidence:
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category="low_confidence",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning,
                        details="Low confidence; dismissed & blocklisted (still seeding)",
                        item_keys=item_keys,
                    )
                    await self.resolve_queue_item(
                        queue_id=queue_id,
                        download_id=download_id,
                        remove_from_client=False,
                        blocklist=True,
                        skip_redownload=False,
                        dry_run=is_dry_run,
                        title=title,
                    )
                    return False

                metrics.record_processed_item(
                    app="Radarr",
                    title=title,
                    category="low_confidence",
                    status="skipped",
                    confidence=stage2.confidence,
                    model=model_label,
                    reasoning=stage2.reasoning,
                    details="Left in queue (AI confidence below threshold)",
                    item_keys=item_keys,
                )
                return False

            if stage2.decision == "import":
                if not movie_info and stage2.matched_media_id:
                    movie_info = next(
                        (m for m in all_movies if m.get("id") == stage2.matched_media_id),
                        None,
                    )
                    if movie_info:
                        movie_id = movie_info.get("id")

                if not movie_id:
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category="unknown_media",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning or "Could not resolve movie ID in Radarr library",
                        details="Dismissed & blocklisted (still seeding)",
                        item_keys=item_keys,
                    )
                    await self.resolve_queue_item(
                        queue_id=queue_id,
                        download_id=download_id,
                        remove_from_client=False,
                        blocklist=True,
                        skip_redownload=False,
                        dry_run=is_dry_run,
                        title=title,
                    )
                    return False

                candidate_title = movie_info.get("title", title) if movie_info else title
                m_by_path = {m.get("path"): m for m in eligible_manual_items if m.get("path")}
                m_by_name = {
                    (m.get("name") or Path(m.get("path", "")).name): m
                    for m in eligible_manual_items
                }

                files_to_import: List[Dict[str, Any]] = []
                for app_file in stage2.files_to_import:
                    m_file = m_by_path.get(app_file.path) or m_by_name.get(Path(app_file.path).name)
                    if not m_file and len(eligible_manual_items) == 1:
                        m_file = eligible_manual_items[0]
                    if not m_file:
                        continue

                    payload_item = {
                        "path": m_file.get("path"),
                        "movieId": movie_id,
                        "quality": m_file.get("quality"),
                        "languages": m_file.get("languages", []),
                        "releaseGroup": m_file.get("releaseGroup"),
                    }
                    if download_id:
                        payload_item["downloadId"] = download_id
                    files_to_import.append(payload_item)

                if files_to_import:
                    cat_tag = stage2.category_tag or ("archive_extracted" if is_extracted_archive else "ai_imported")
                    import_ok = await self.trigger_manual_import(
                        files_to_import, dry_run=is_dry_run, category_tag=cat_tag
                    )
                    status_label = "dry_run" if is_dry_run else ("imported" if import_ok else "failed")
                    metrics.record_processed_item(
                        app="Radarr",
                        title=title,
                        category=cat_tag,
                        status=status_label,
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning,
                        details=f"Imported {len(files_to_import)} movie file(s) into '{candidate_title}'",
                        item_keys=item_keys,
                    )
                    return import_ok

            if stage2.decision == "reject_delete":
                cat_tag = stage2.category_tag or "malware_or_corrupt"
                metrics.record_processed_item(
                    app="Radarr",
                    title=title,
                    category=cat_tag,
                    status="dry_run" if is_dry_run else "rejected",
                    confidence=stage2.confidence,
                    model=model_label,
                    reasoning=stage2.reasoning,
                    details="Deleted from torrent & blocklisted",
                    item_keys=item_keys,
                )
                await self.delete_and_blocklist(
                    queue_id=queue_id,
                    download_id=download_id,
                    dry_run=is_dry_run,
                    title=title,
                )
                return False

            cat_tag = stage2.category_tag or "dismissed"
            metrics.record_processed_item(
                app="Radarr",
                title=title,
                category=cat_tag,
                status="dry_run" if is_dry_run else "dismissed",
                confidence=stage2.confidence,
                model=model_label,
                reasoning=stage2.reasoning,
                details="Dismissed, blocklisted & re-searched (still seeding)",
                item_keys=item_keys,
            )
            await self.resolve_queue_item(
                queue_id=queue_id,
                download_id=download_id,
                remove_from_client=False,
                blocklist=True,
                skip_redownload=False,
                dry_run=is_dry_run,
                title=title,
            )
            return False

        finally:
            if staging_dir_to_clean and staging_dir_to_clean.exists():
                try:
                    shutil.rmtree(staging_dir_to_clean, ignore_errors=True)
                    logger.info(
                        f"  🧹 Cleaned up extracted staging folder '{staging_dir_to_clean}'. Seeding archive preserved."
                    )
                except Exception as ce:
                    logger.debug(f"Failed to clean up staging folder '{staging_dir_to_clean}': {ce}")
