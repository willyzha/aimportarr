import asyncio
from datetime import datetime, timedelta, timezone
import logging
import shutil
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Set
import httpx
from .ai.base import (
    BaseAIProvider,
    Stage1TriageResult,
    Stage2DecisionResult,
    ApprovedFileItem,
)
from .classifier import (
    classify_issue,
    ErrorCategory,
    QUALITY_REJECTION_KEYWORDS,
    ALREADY_IMPORTED_KEYWORDS,
)
from .config import ExtractionConfig, SeasonPackBackfillConfig, MissingEpisodeRetryConfig
from .extractor import (
    find_archive_file,
    inspect_archive,
    extract_archive,
    resolve_local_path,
    VIDEO_EXTENSIONS,
    SUSPICIOUS_EXTENSIONS,
)
from .metrics import metrics

logger = logging.getLogger("aimportarr.sonarr")

EXISTING_FILE_QUALITY_REJECTION_PATTERNS = (
    "existing file on disk",
    "not an upgrade",
    "not a custom format upgrade",
    "cutoff already met",
    "existing file meets cutoff",
)

HARD_EPISODE_RELEASE_DISQUALIFIER_PATTERNS = (
    "not wanted",
    "below minimum",
    "not enough seeders",
    "blocklist",
    "blacklist",
    "ignored terms",
    "required terms",
    "maximum size",
    "minimum size",
    "greater than maximum",
    "smaller than minimum",
    "full season",
)

HARD_RELEASE_DISQUALIFIER_PATTERNS = (
    "not wanted",
    "below minimum",
    "not enough seeders",
    "blocklist",
    "blacklist",
    "ignored terms",
    "required terms",
    "maximum size",
    "minimum size",
    "greater than maximum",
    "smaller than minimum",
    "single episode",
    "not a full season",
)


class SonarrClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        min_confidence: float = 0.85,
        dry_run: bool = False,
        reject_on_low_confidence: bool = False,
        extraction_config: Optional[ExtractionConfig] = None,
        backfill_config: Optional[SeasonPackBackfillConfig] = None,
        episode_retry_config: Optional[MissingEpisodeRetryConfig] = None,
        path_mappings: Optional[List[Any]] = None,
        categories: Optional[Any] = None,  # Ignored legacy parameter if passed by external callers
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.dry_run = bool(dry_run)
        self.reject_on_low_confidence = bool(reject_on_low_confidence)
        self.extraction_config = extraction_config or ExtractionConfig()
        self.backfill_config = backfill_config or SeasonPackBackfillConfig()
        self.episode_retry_config = episode_retry_config or MissingEpisodeRetryConfig()
        self.path_mappings = path_mappings or []
        self._dismissed_downloads: set = set()
        self._attempted_items: set = set()
        self._attempted_backfill_keys: set = set()
        self._attempted_episode_retry_keys: set = set()
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
        """Fetch all items from the Sonarr download queue."""
        data = await self._get(
            "queue",
            params={
                "pageSize": 100,
                "includeUnknownSeriesItems": True,
                "includeSeries": True,
                "includeEpisode": True,
            },
        )
        return data.get("records", [])

    async def get_series(self, series_id: int) -> Dict[str, Any]:
        """Fetch detailed series metadata."""
        return await self._get(f"series/{series_id}")

    async def get_all_series(self) -> List[Dict[str, Any]]:
        """Fetch all tracked series from Sonarr library."""
        try:
            return await self._get("series")
        except Exception as e:
            logger.error(f"Failed to fetch series list from Sonarr: {e}")
            return []

    async def get_series_episodes(self, series_id: int) -> List[Dict[str, Any]]:
        """Fetch all episodes for a series."""
        try:
            return await self._get("episode", params={"seriesId": series_id})
        except Exception as e:
            logger.debug(f"Failed to fetch episodes for series {series_id}: {e}")
            return []

    async def get_episode_files(self, series_id: int) -> List[Dict[str, Any]]:
        """Fetch all existing on-disk episode files for a series to evaluate quality upgrades/downgrades."""
        try:
            return await self._get("episodefile", params={"seriesId": series_id})
        except Exception as e:
            logger.debug(f"Failed to fetch episode files for series {series_id}: {e}")
            return []

    async def get_manual_import_items(
        self, folder: str, download_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Query Sonarr for media files detected in the download directory or queue item.
        Prioritizes querying by downloadId if available to accurately link to the queue download.
        Never passes seriesId to prevent Sonarr from returning files already inside the library.
        """
        if download_id:
            try:
                items = await self._get("manualimport", params={"downloadId": download_id})
                if items:
                    return items
            except Exception as e:
                logger.debug(f"Query /manualimport by downloadId '{download_id}' failed: {e}")

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
                    logger.debug(f"Retrying Sonarr /manualimport for folder '{folder_dir}' without downloadId...")
                    return await self._get("manualimport", params={"folder": folder_dir, "filterExistingFiles": True})
                except Exception as fe:
                    logger.error(f"Failed to query /manualimport for folder '{folder_dir}': {fe}")
                    return []
            logger.error(f"Failed to query /manualimport for folder '{folder_dir}': {e}")
            return []

    async def trigger_manual_import(
        self,
        files_to_import: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        category_tag: str = "imported",
    ) -> bool:
        """Submit the resolved files to Sonarr for execution and track command status."""
        if not files_to_import:
            return False

        is_dry = self.dry_run if dry_run is None else dry_run

        if is_dry:
            logger.info(f"[DRY RUN] Would import {len(files_to_import)} file(s) to Sonarr [{category_tag}]")
            for f in files_to_import:
                logger.debug(
                    f"  [DRY RUN] File: {f.get('path')} | SeriesId: {f.get('seriesId')} | Episodes: {f.get('episodeIds')}"
                )
            return True

        payload = {
            "name": "ManualImport",
            "files": files_to_import,
            "importMode": "auto",
        }
        try:
            logger.info(f"Submitting {len(files_to_import)} file(s) to Sonarr for import [{category_tag}]...")
            res = await self._post("command", payload)
            cmd_id = res.get("id")
            logger.debug(f"Triggered Sonarr ManualImport command ID: {cmd_id}")

            if cmd_id:
                for _ in range(5):
                    await asyncio.sleep(2)
                    try:
                        cmd_status = await self._get(f"command/{cmd_id}")
                        state = cmd_status.get("status") or cmd_status.get("state")
                        if state == "completed":
                            logger.info(
                                f"Sonarr successfully imported {len(files_to_import)} file(s) (Command ID: {cmd_id})"
                            )
                            return True
                        elif state == "failed":
                            logger.error(
                                f"Sonarr ManualImport command {cmd_id} failed: {cmd_status.get('message', 'Unknown failure')}"
                            )
                            return False
                    except Exception as pe:
                        logger.debug(f"Failed to poll command {cmd_id} status: {pe}")
                        break

            return True
        except Exception as e:
            logger.error(f"Failed to trigger Sonarr ManualImport command: {e}")
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
        Unified queue removal handler for Sonarr:
        - Virus / Sample-only / Corrupt: remove_from_client=True, blocklist=True, skip_redownload=False
        - Quality Downgrade / Wrong Media (Keep in Client): remove_from_client=False, blocklist=True, skip_redownload=False
        - Season Pack Already Satisfied (Keep in Client): remove_from_client=False, blocklist=False, skip_redownload=True
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
            else ("dismiss (keep in client), blocklist & re-search" if blocklist else "dismiss warning (keep in client)")
        )

        if is_dry:
            logger.info(
                f"  [DRY RUN] Would {action_desc} for '{title}' (Queue IDs: {ids_to_remove}, params={params})"
            )
            if download_id:
                self._dismissed_downloads.add(download_id)
            return True

        logger.info(f"  Executing Sonarr queue action ({action_desc}) for '{title}' (Queue IDs: {ids_to_remove})...")
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

    async def dismiss_queue_warning(
        self,
        queue_id: int,
        download_id: Optional[str] = None,
        dry_run: bool = False,
        title: str = "",
    ) -> bool:
        """Dismiss a stuck queue warning while keeping the download in the client and without blocklisting."""
        return await self.resolve_queue_item(
            queue_id=queue_id,
            download_id=download_id,
            remove_from_client=False,
            blocklist=False,
            skip_redownload=True,
            dry_run=dry_run,
            title=title,
        )

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
        series_info = item.get("series")
        dedup_key = f"sonarr:{download_id or queue_id or title}"

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

        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and series_info:
            return False, dedup_key

        return True, dedup_key

    async def process_queue_item(self, item: Dict[str, Any], ai_provider: BaseAIProvider) -> bool:
        """
        4-Step / Two-Stage Pipeline for Sonarr Queue Items:
        1. Let Sonarr process normal downloads naturally.
        2. Stage 1 (Hybrid Archive & Malware Triage): Inspect disk/7z headers; auto-extract clean archives,
           and use Stage 1 AI when suspicious files or archive anomalies are detected.
        3. (Optional) Extract archive into temporary staging directory (<stem>_extracted).
        4. Stage 2: Query /manualimport + existing on-disk file qualities. Fast-path unambiguous cases
           (cleanly mapped or hard quality downgrade) and invoke Stage 2 AI for everything else.
        """
        title = item.get("title", "")
        status = (item.get("trackedDownloadStatus") or "").lower()
        tracked_state = (item.get("trackedDownloadState") or "").lower()
        status_messages = item.get("statusMessages", [])
        output_path = item.get("outputPath", "")
        download_id = item.get("downloadId", "")
        queue_id = item.get("id")
        series_info = item.get("series")

        # ------------------------------------------------------------------
        # STEP 1: Hands-off checks (let Sonarr handle normal downloads)
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

        initial_category = classify_issue(all_messages, has_media_info=bool(series_info), title=title)

        if status not in ("warning", "error") and tracked_state != "importblocked" and not all_messages and series_info:
            return False

        is_dry_run = self.dry_run
        mode_tag = "[DRY RUN]" if is_dry_run else "[LIVE]"
        logger.info(f"Processing stuck Sonarr item: '{title}' [{initial_category.value}] {mode_tag}")

        # ------------------------------------------------------------------
        # STEP 2 & STEP 3: Stage 1 Triage & Optional Archive Extraction
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
                    logger.info(f"  🔍 Archive detected: '{archive_file.name}'. Inspecting contents...")
                    inspection = await inspect_archive(archive_file)
                    if inspection and inspection.has_video_files and not inspection.has_suspicious_files:
                        # Clean video archive -> deterministic staging extraction (0 AI tokens)
                        logger.info(
                            f"  📦 Valid media archive detected ({len(inspection.video_files)} video file(s)). Extracting to staging folder..."
                        )
                        staging_dir = archive_file.parent / f"{archive_file.stem}{self.extraction_config.staging_suffix}"
                        extract_ok = await extract_archive(archive_file, staging_dir)
                        if not extract_ok:
                            logger.error(f"  Failed to extract archive '{archive_file.name}'")
                            return False
                        metrics.record_archive_extracted()
                        is_extracted_archive = True
                        staging_dir_to_clean = staging_dir
                        logger.info(f"  ✔ Extraction complete: '{staging_dir}'. Scanning for import...")
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
                                app="Sonarr",
                                title=title,
                                category=triage.category_tag or "malware_or_corrupt",
                                status="dry_run" if is_dry_run else "rejected",
                                confidence=triage.confidence,
                                model=model_label,
                                reasoning=triage.reasoning,
                                details=f"Deleted from client & blocklisted ({archive_file.name})",
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
                        app="Sonarr",
                        title=title,
                        category=triage.category_tag or "malware_or_corrupt",
                        status="dry_run" if is_dry_run else "rejected",
                        confidence=triage.confidence,
                        model=model_label,
                        reasoning=triage.reasoning,
                        details="Deleted from client & blocklisted",
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
                            app="Sonarr",
                            title=title,
                            category="low_confidence",
                            status="dry_run" if is_dry_run else "dismissed",
                            confidence=triage.confidence,
                            model=model_label,
                            reasoning=triage.reasoning,
                            details="Low-confidence suspicious item; dismissed & blocklisted (kept in client)",
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
                        app="Sonarr",
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

            # Filter out files already inside the destination series library folder
            series_path = (series_info.get("path") or "").rstrip("/") if series_info else ""
            if series_path:
                manual_items = [
                    m for m in manual_items
                    if not (m.get("path") or "").startswith(series_path)
                ]

            # Handle 0 importable files from /manualimport
            if not manual_items:
                if initial_category == ErrorCategory.MISSING_EPISODES:
                    logger.info(
                        f"  Sonarr returned 0 remaining importable files for season pack '{title}'. Dismissing queue warning..."
                    )
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="season_pack",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=1.0,
                        model="Sonarr (Season Pack)",
                        reasoning="All needed episodes accounted for; queue warning dismissed while keeping files in client.",
                        details="Dismissed season pack warning",
                        item_keys=item_keys,
                    )
                    return await self.dismiss_queue_warning(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
                    )

                if not p_local.exists():
                    logger.warning(
                        f"  Sonarr returned 0 files for '{title}' and local path '{p_local}' is not accessible. Leaving in queue."
                    )
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="path_unreachable",
                        status="skipped",
                        confidence=0.0,
                        model=None,
                        reasoning=f"Sonarr returned 0 importable files and local path '{p_local}' is not mounted.",
                        details="0 files found (path not mounted)",
                        item_keys=item_keys,
                    )
                    return False

                # Local folder exists on disk, but Sonarr sees 0 video files -> inspect with Stage 1 AI
                triage = await ai_provider.evaluate_stage1_triage(
                    release_title=title,
                    error_messages=all_messages,
                    disk_files=raw_disk_files,
                )
                model_label = triage.model_name or "AI"
                if triage.action == "reject_delete" and triage.confidence >= self.min_confidence:
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category=triage.category_tag or "sample_only",
                        status="dry_run" if is_dry_run else "rejected",
                        confidence=triage.confidence,
                        model=model_label,
                        reasoning=triage.reasoning,
                        details="0 valid videos; deleted from client & blocklisted",
                        item_keys=item_keys,
                    )
                    return await self.delete_and_blocklist(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
                    )
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="no_importable_files",
                    status="skipped",
                    confidence=triage.confidence,
                    model=model_label,
                    reasoning=triage.reasoning or "Sonarr manual import returned 0 importable files",
                    details="Left in queue (0 importable files found)",
                    item_keys=item_keys,
                )
                return False

            # Gather series episodes and existing on-disk episode files
            series_id = series_info.get("id") if series_info else None
            all_episodes = await self.get_series_episodes(series_id) if series_id else []
            raw_ep_files = await self.get_episode_files(series_id) if series_id else []
            ep_file_map = {ef.get("id"): ef for ef in raw_ep_files if ef.get("id")}
            existing_disk_files = []
            for ep in all_episodes:
                ef_id = ep.get("episodeFileId")
                if ep.get("hasFile") and ef_id in ep_file_map:
                    ef = ep_file_map[ef_id]
                    existing_disk_files.append({
                        "season": ep.get("seasonNumber"),
                        "episode": ep.get("episodeNumber"),
                        "quality": (ef.get("quality") or {}).get("quality", {}).get("name"),
                        "customFormatScore": ef.get("customFormatScore"),
                        "size": ef.get("size"),
                    })

            # Evaluate fast-path eligibility and hard quality rejections
            fast_path_import_files: List[Dict[str, Any]] = []
            quality_rejected_count = 0
            already_imported_count = 0
            needs_ai_stage2 = False
            all_rejection_reasons: List[str] = []

            if not series_info or initial_category == ErrorCategory.ID_MATCH:
                needs_ai_stage2 = True

            for m_file in manual_items:
                f_path = m_file.get("path", "")
                f_name = m_file.get("name") or Path(f_path).name
                rejections = [r.get("reason", "") for r in m_file.get("rejections", [])]
                all_rejection_reasons.extend(rejections)
                curr_episodes = m_file.get("episodes", [])
                curr_series = m_file.get("series") or series_info
                target_series_id = curr_series.get("id") if curr_series else series_id

                if any("matched to series by id" in r.lower() or "matched by id" in r.lower() for r in rejections):
                    needs_ai_stage2 = True

                already_imported_rejs = [
                    r for r in rejections
                    if any(k in r.lower() for k in ALREADY_IMPORTED_KEYWORDS)
                ]
                if already_imported_rejs:
                    logger.info(
                        f"  ✔ Skipping file '{f_name}': episode already imported ({'; '.join(already_imported_rejs)})."
                    )
                    already_imported_count += 1
                    continue

                quality_rejs = [
                    r for r in rejections
                    if any(k in r.lower() for k in QUALITY_REJECTION_KEYWORDS)
                ]
                if quality_rejs:
                    logger.warning(
                        f"  ✖ Skipping file '{f_name}': Sonarr quality rule rejected import ({'; '.join(quality_rejs)})."
                    )
                    quality_rejected_count += 1
                    continue

                if curr_episodes and not rejections and not needs_ai_stage2:
                    ep_ids = [ep.get("id") for ep in curr_episodes]
                    payload_item = {
                        "path": f_path,
                        "seriesId": target_series_id,
                        "episodeIds": ep_ids,
                        "quality": m_file.get("quality"),
                        "languages": m_file.get("languages", []),
                        "releaseGroup": m_file.get("releaseGroup"),
                    }
                    if download_id:
                        payload_item["downloadId"] = download_id
                    fast_path_import_files.append(payload_item)
                else:
                    needs_ai_stage2 = True

            # FAST-PATH A: All candidate files were either already imported or rejected by Sonarr's hard quality/CF rules
            if (quality_rejected_count + already_imported_count) == len(manual_items):
                reason_str = "; ".join(sorted(set(all_rejection_reasons))) or "Existing file on disk has equal or higher quality"
                # Check if this is a multi-episode season pack where all monitored episodes in the season are now on disk
                pack_seasons = {
                    ep.get("seasonNumber")
                    for m in manual_items
                    for ep in (m.get("episodes") or [])
                    if ep.get("seasonNumber") is not None
                }
                is_satisfied_season_pack = False
                if (len(manual_items) > 1 or bool(item.get("fullSeason"))) and pack_seasons and all_episodes:
                    season_monitored_eps = [
                        e for e in all_episodes
                        if e.get("seasonNumber") in pack_seasons and e.get("monitored", True)
                    ]
                    if season_monitored_eps and all(e.get("hasFile", False) for e in season_monitored_eps):
                        is_satisfied_season_pack = True

                if (
                    initial_category == ErrorCategory.MISSING_EPISODES
                    or already_imported_count > 0
                    or is_satisfied_season_pack
                ):
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="season_pack" if len(manual_items) > 1 else "already_imported",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=1.0,
                        model="Sonarr (Quality Rule)",
                        reasoning=f"Remaining season pack files already on disk at equal/higher quality ({reason_str})",
                        details="Dismissed season pack warning",
                        item_keys=item_keys,
                    )
                    await self.dismiss_queue_warning(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
                    )
                    return False

                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="quality_downgrade",
                    status="dry_run" if is_dry_run else "dismissed",
                    confidence=1.0,
                    model="Sonarr (Quality Rule)",
                    reasoning=f"Rejected by Sonarr quality rules: {reason_str}",
                    details="Dismissed & blocklisted (kept in client)",
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

            # FAST-PATH B: Cleanly mapped files with 0 rejections (e.g., extracted archive or partial season pack)
            if not needs_ai_stage2 and fast_path_import_files:
                decider = "Sonarr (Extracted Archive)" if is_extracted_archive else "Sonarr (Direct Match)"
                cat_tag = "archive_extracted" if is_extracted_archive else (
                    "season_pack"
                    if (initial_category == ErrorCategory.MISSING_EPISODES or already_imported_count > 0 or quality_rejected_count > 0)
                    else "direct_match"
                )
                candidate_title = series_info.get("title", "") if series_info else title
                logger.info(
                    f"  ✔ Fast-path approved {len(fast_path_import_files)} file(s) for '{candidate_title}' | Model: {decider}"
                )
                import_ok = await self.trigger_manual_import(
                    fast_path_import_files, dry_run=is_dry_run, category_tag=cat_tag
                )
                status_label = "dry_run" if is_dry_run else ("imported" if import_ok else "failed")
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category=cat_tag,
                    status=status_label,
                    confidence=1.0,
                    model=decider,
                    reasoning=f"Cleanly mapped {len(fast_path_import_files)} file(s) with 0 quality/identity rejections",
                    details=f"Imported {len(fast_path_import_files)} episode file(s) into '{candidate_title}'",
                    item_keys=item_keys,
                )
                if import_ok and (
                    initial_category == ErrorCategory.MISSING_EPISODES
                    or quality_rejected_count > 0
                    or already_imported_count > 0
                ):
                    await self.dismiss_queue_warning(
                        queue_id=queue_id,
                        download_id=download_id,
                        dry_run=is_dry_run,
                        title=title,
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

            all_series: List[Dict[str, Any]] = []
            library_candidates: Optional[List[Dict[str, Any]]] = None
            if not series_info:
                all_series = await self.get_all_series()
                library_candidates = [
                    {
                        "id": s.get("id"),
                        "title": s.get("title"),
                        "year": s.get("year"),
                        "aliases": [a.get("title") for a in s.get("alternateTitles", [])],
                    }
                    for s in all_series
                ]

            # Filter out hard quality-rejected and already-imported files before asking Stage 2 AI
            skip_rejection_keywords = QUALITY_REJECTION_KEYWORDS + ALREADY_IMPORTED_KEYWORDS
            eligible_manual_items = [
                m for m in manual_items
                if not any(
                    any(k in r.get("reason", "").lower() for k in skip_rejection_keywords)
                    for r in m.get("rejections", [])
                )
            ]

            logger.info(f"  Running Stage 2 AI evaluation for '{title}'...")
            stage2 = await ai_provider.evaluate_import_decision(
                media_type="series",
                release_title=title,
                folder_name=folder_name,
                queue_messages=all_messages,
                candidate_media={
                    "id": series_info.get("id"),
                    "title": series_info.get("title"),
                    "year": series_info.get("year"),
                    "aliases": [a.get("title") for a in series_info.get("alternateTitles", [])],
                }
                if series_info
                else None,
                library_candidates=library_candidates,
                manual_import_files=[
                    {
                        "path": m.get("path"),
                        "name": m.get("name") or Path(m.get("path", "")).name,
                        "quality": (m.get("quality") or {}).get("quality", {}).get("name"),
                        "customFormatScore": m.get("customFormatScore"),
                        "languages": [l.get("name") for l in m.get("languages", []) if isinstance(l, dict)],
                        "episodes": [
                            {"id": e.get("id"), "season": e.get("seasonNumber"), "episode": e.get("episodeNumber")}
                            for e in m.get("episodes", [])
                        ],
                        "rejections": [r.get("reason", "") for r in m.get("rejections", [])],
                    }
                    for m in eligible_manual_items
                ],
                existing_disk_files=existing_disk_files,
                known_episodes=[
                    {
                        "id": e.get("id"),
                        "season": e.get("seasonNumber"),
                        "episode": e.get("episodeNumber"),
                        "title": e.get("title"),
                        "hasFile": e.get("hasFile", False),
                    }
                    for e in all_episodes
                ],
                raw_disk_files=raw_disk_files,
            )
            model_label = stage2.model_name or "AI"

            # Check AI confidence threshold
            if stage2.confidence < self.min_confidence:
                logger.warning(
                    f"  ⚠️ Stage 2 AI confidence ({stage2.confidence:.0%}) is below threshold ({self.min_confidence:.0%}) for '{title}': {stage2.reasoning}"
                )
                if self.reject_on_low_confidence:
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="low_confidence",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning,
                        details="Low confidence; dismissed & blocklisted (kept in client)",
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
                    app="Sonarr",
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

            # Handle Stage 2 'import' decision
            if stage2.decision == "import":
                if not series_info and stage2.matched_media_id:
                    series_info = next(
                        (s for s in all_series if s.get("id") == stage2.matched_media_id),
                        None,
                    )
                    if series_info:
                        series_id = series_info.get("id")
                        all_episodes = await self.get_series_episodes(series_id)

                if not series_id:
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=title,
                        category="unknown_media",
                        status="dry_run" if is_dry_run else "dismissed",
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning or "Could not resolve series ID in Sonarr library",
                        details="Dismissed & blocklisted (kept in client)",
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

                candidate_title = series_info.get("title", title) if series_info else title
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

                    curr_eps = m_file.get("episodes", [])
                    if curr_eps:
                        ep_ids = [ep.get("id") for ep in curr_eps if ep.get("id")]
                    else:
                        s_num = app_file.season_number if app_file.season_number is not None else 1
                        ep_ids = [
                            e.get("id")
                            for e in all_episodes
                            if e.get("seasonNumber") == s_num and e.get("episodeNumber") in app_file.episode_numbers
                        ]

                    if not ep_ids:
                        continue

                    payload_item = {
                        "path": m_file.get("path"),
                        "seriesId": series_id,
                        "episodeIds": ep_ids,
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
                        app="Sonarr",
                        title=title,
                        category=cat_tag,
                        status=status_label,
                        confidence=stage2.confidence,
                        model=model_label,
                        reasoning=stage2.reasoning,
                        details=f"Imported {len(files_to_import)} episode file(s) into '{candidate_title}'",
                        item_keys=item_keys,
                    )
                    if import_ok and (
                        initial_category == ErrorCategory.MISSING_EPISODES
                        or len(files_to_import) < len(manual_items)
                    ):
                        await self.dismiss_queue_warning(
                            queue_id=queue_id,
                            download_id=download_id,
                            dry_run=is_dry_run,
                            title=title,
                        )
                    return import_ok

            # Handle Stage 2 'reject_delete' (virus, sample-only, or corrupt)
            if stage2.decision == "reject_delete":
                cat_tag = stage2.category_tag or "malware_or_corrupt"
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category=cat_tag,
                    status="dry_run" if is_dry_run else "rejected",
                    confidence=stage2.confidence,
                    model=model_label,
                    reasoning=stage2.reasoning,
                    details="Deleted from client & blocklisted",
                    item_keys=item_keys,
                )
                await self.delete_and_blocklist(
                    queue_id=queue_id,
                    download_id=download_id,
                    dry_run=is_dry_run,
                    title=title,
                )
                return False

            # Handle Stage 2 'dismiss_pack' (already satisfied season pack)
            if stage2.decision == "dismiss_pack":
                metrics.record_processed_item(
                    app="Sonarr",
                    title=title,
                    category="season_pack",
                    status="dry_run" if is_dry_run else "dismissed",
                    confidence=stage2.confidence,
                    model=model_label,
                    reasoning=stage2.reasoning,
                    details="Dismissed season pack warning (kept in client)",
                    item_keys=item_keys,
                )
                await self.dismiss_queue_warning(
                    queue_id=queue_id,
                    download_id=download_id,
                    dry_run=is_dry_run,
                    title=title,
                )
                return False

            # Default Stage 2 rejection: 'reject_keep_in_client' (quality downgrade or wrong media)
            cat_tag = stage2.category_tag or "dismissed"
            metrics.record_processed_item(
                app="Sonarr",
                title=title,
                category=cat_tag,
                status="dry_run" if is_dry_run else "dismissed",
                confidence=stage2.confidence,
                model=model_label,
                reasoning=stage2.reasoning,
                details="Dismissed, blocklisted & re-searched (kept in client)",
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
            if self.extraction_config.delete_after_import and staging_dir_to_clean and staging_dir_to_clean.exists():
                try:
                    shutil.rmtree(staging_dir_to_clean, ignore_errors=True)
                    logger.info(
                        f"  🧹 Cleaned up extracted staging folder '{staging_dir_to_clean}'. Original archive preserved."
                    )
                except Exception as ce:
                    logger.debug(f"Failed to clean up staging folder '{staging_dir_to_clean}': {ce}")

    # =========================================================================
    # Season Pack Backfill Engine
    # =========================================================================

    async def get_season_releases(self, series_id: int, season_number: int) -> List[Dict[str, Any]]:
        """Query Sonarr's Interactive Search endpoint for all releases of a specific series season."""
        url = f"{self.base_url}/api/v3/release"
        params = {"seriesId": series_id, "seasonNumber": season_number}
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []

    async def grab_release(
        self,
        guid: str,
        indexer_id: int,
        dry_run: Optional[bool] = None,
        title: str = "",
    ) -> bool:
        """Force-grab a specific release via POST /api/v3/release (overriding existing-file rejections)."""
        if not guid or indexer_id is None:
            return False

        is_dry = self.dry_run if dry_run is None else dry_run
        if is_dry:
            logger.info(
                f"  [DRY RUN] Would force-grab season pack '{title}' (GUID: {guid}, Indexer: {indexer_id})"
            )
            return True

        try:
            await self._post("release", {"guid": guid, "indexerId": int(indexer_id)})
            logger.info(f"  ✔ Force-grabbed season pack '{title}' (Indexer: {indexer_id})")
            return True
        except Exception as e:
            logger.error(f"  ✖ Failed to force-grab season pack '{title}': {e}")
            return False

    @staticmethod
    def classify_season_pack_release(release: Dict[str, Any], season_number: int) -> str:
        """
        Classify a release from GET /api/v3/release into:
        - 'tier1_eligible': Valid fullSeason pack for `season_number` with 0 rejections OR rejected
          solely because existing episode files on disk have equal/higher quality/Custom Format score.
        - 'tier2_unknown_series': Valid fullSeason pack whose only non-quality rejection is 'Unknown Series'.
        - 'disqualified': Not a full season pack, 0 seeders, or has hard profile/language/size/blocklist rejections.
        """
        if not release.get("fullSeason"):
            return "disqualified"

        rel_season = release.get("seasonNumber")
        protocol = (release.get("protocol") or "").lower()
        if protocol == "torrent" and (release.get("seeders") or 0) < 1:
            return "disqualified"

        raw_rejections = release.get("rejections") or []
        rejections = [
            (r.get("reason", "") if isinstance(r, dict) else str(r)).strip()
            for r in raw_rejections
            if r
        ]

        for r in rejections:
            r_low = r.lower()
            if any(h in r_low for h in HARD_RELEASE_DISQUALIFIER_PATTERNS):
                return "disqualified"

        if not release.get("rejected") or not rejections:
            if rel_season == season_number:
                return "tier1_eligible"
            return "disqualified"

        has_unknown_series = False
        for r in rejections:
            r_low = r.lower()
            is_existing_quality = any(p in r_low for p in EXISTING_FILE_QUALITY_REJECTION_PATTERNS)
            is_unknown_series = any(
                u in r_low for u in ("unknown series", "no series", "not matched to any series")
            )
            if is_unknown_series:
                has_unknown_series = True
            elif not is_existing_quality:
                return "disqualified"

        if has_unknown_series:
            if rel_season in (season_number, None, 0):
                return "tier2_unknown_series"
            return "disqualified"

        if rel_season == season_number:
            return "tier1_eligible"
        return "disqualified"

    @staticmethod
    def is_season_eligible_for_backfill(
        season_episodes: List[Dict[str, Any]],
        min_missing_days: int = 3,
        is_latest_season: bool = False,
        series_status: str = "ended",
        now_utc: Optional[datetime] = None,
    ) -> Tuple[bool, int, int]:
        """
        Verify if a season is eligible for Season Pack Backfill:
        1. Has >= 1 monitored episode.
        2. Not the latest season of an actively continuing/upcoming series.
        3. ALL monitored episodes in the season have aired at least `min_missing_days` ago (season is finished airing).
        4. At least 1 monitored episode is missing (`hasFile == False`).
        Returns (is_eligible, missing_count, existing_count).
        """
        if is_latest_season and str(series_status).strip().lower() in ("continuing", "upcoming"):
            return False, 0, 0

        monitored_eps = [e for e in season_episodes if e.get("monitored", True)]
        if not monitored_eps:
            return False, 0, 0

        now_dt = now_utc or datetime.now(timezone.utc)
        cutoff_dt = now_dt - timedelta(days=max(0, int(min_missing_days)))

        missing_count = 0
        existing_count = 0

        for ep in monitored_eps:
            if ep.get("hasFile", False):
                existing_count += 1
            else:
                missing_count += 1

            air_utc_str = ep.get("airDateUtc")
            air_date_str = ep.get("airDate")
            if not air_utc_str and not air_date_str:
                # Unannounced / TBA episode -> season has not finished airing
                return False, 0, 0

            try:
                if air_utc_str:
                    ep_dt = datetime.fromisoformat(air_utc_str.replace("Z", "+00:00"))
                    if ep_dt.tzinfo is None:
                        ep_dt = ep_dt.replace(tzinfo=timezone.utc)
                else:
                    ep_dt = datetime.fromisoformat(f"{air_date_str}T00:00:00+00:00")
            except ValueError:
                return False, 0, 0

            if ep_dt > cutoff_dt:
                # Episode hasn't aired yet or is within the post-finale grace period
                return False, 0, 0

        if missing_count < 1:
            return False, 0, existing_count

        return True, missing_count, existing_count

    @staticmethod
    def _rank_season_pack(release: Dict[str, Any]) -> Tuple[int, int, int, int]:
        """Sort key for ranking qualifying season packs (highest first)."""
        not_rejected = 0 if release.get("rejected") else 1
        cf_score = int(release.get("customFormatScore") or 0)
        q_weight = int(release.get("qualityWeight") or 0)
        seeders = int(release.get("seeders") or 0)
        return (not_rejected, cf_score, q_weight, seeders)

    async def run_season_pack_backfill(
        self,
        ai_provider: Optional[BaseAIProvider] = None,
        dry_run: Optional[bool] = None,
    ) -> int:
        """
        Scan monitored Sonarr series for completed seasons with missing episodes,
        search for qualifying Season Packs via GET /api/v3/release, and force-grab the best pack.
        Returns the number of season packs grabbed (or simulated in Dry Run).
        """
        is_dry_run = self.dry_run if dry_run is None else dry_run
        max_seasons = max(1, int(self.backfill_config.max_seasons_per_run))
        min_missing_days = max(0, int(self.backfill_config.min_missing_days))
        cooldown_days = max(1, int(self.backfill_config.season_cooldown_days))

        logger.info(
            f"📦 Running Sonarr Season Pack Backfill check (Max seasons: {max_seasons}, Grace period: {min_missing_days}d, Cooldown: {cooldown_days}d)..."
        )

        all_series = await self.get_all_series()
        if not all_series:
            return 0

        # Identify seasons already active in the Sonarr download queue
        queue_items = await self.get_queue()
        queued_series_seasons = set()
        queued_episode_ids = set()
        for q in queue_items or []:
            q_ep_id = q.get("episodeId") or (q.get("episode") or {}).get("id")
            if q_ep_id:
                queued_episode_ids.add(int(q_ep_id))
            q_series_id = q.get("seriesId") or (q.get("series") or {}).get("id")
            q_season = q.get("seasonNumber") or (q.get("episode") or {}).get("seasonNumber")
            if q_series_id and q_season is not None:
                queued_series_seasons.add((int(q_series_id), int(q_season)))
            elif q_series_id and q.get("fullSeason"):
                queued_series_seasons.add((int(q_series_id), -1))

        seasons_searched = 0
        packs_grabbed = 0

        for series in all_series:
            if seasons_searched >= max_seasons:
                break
            if not series.get("monitored", True):
                continue

            series_id = series.get("id")
            series_title = series.get("title") or f"Series #{series_id}"
            if not series_id:
                continue

            seasons_meta = {
                int(s.get("seasonNumber")): s
                for s in (series.get("seasons") or [])
                if s.get("seasonNumber") is not None
            }

            episodes = await self.get_series_episodes(series_id)
            if not episodes:
                continue

            eps_by_season: Dict[int, List[Dict[str, Any]]] = {}
            for ep in episodes:
                s_num = ep.get("seasonNumber")
                if s_num is None or int(s_num) <= 0:
                    continue
                eps_by_season.setdefault(int(s_num), []).append(ep)

            sorted_seasons = sorted(eps_by_season.keys())
            max_season_num = max(sorted_seasons) if sorted_seasons else 0
            series_status = series.get("status") or ""

            for season_num in sorted_seasons:
                if seasons_searched >= max_seasons:
                    break

                # Ensure season itself is monitored in Sonarr
                s_meta = seasons_meta.get(season_num)
                if s_meta and not s_meta.get("monitored", True):
                    continue

                # Skip if already downloading in queue
                if (int(series_id), season_num) in queued_series_seasons or (int(series_id), -1) in queued_series_seasons:
                    continue

                season_eps = eps_by_season[season_num]
                is_latest_season = (season_num == max_season_num)
                eligible, missing_cnt, existing_cnt = self.is_season_eligible_for_backfill(
                    season_eps,
                    min_missing_days=min_missing_days,
                    is_latest_season=is_latest_season,
                    series_status=series_status,
                )
                if not eligible:
                    continue

                backfill_key = f"backfill:sonarr:{series_id}:s{season_num:02d}"
                if metrics.is_item_attempted_within_days([backfill_key], days=cooldown_days):
                    continue
                if not metrics.db and backfill_key in self._attempted_backfill_keys:
                    continue

                if seasons_searched > 0:
                    await asyncio.sleep(5.0)  # Polite pacing between indexer searches

                seasons_searched += 1
                season_label = f"{series_title} - Season {season_num:02d}"
                logger.info(
                    f"  🔎 Searching Season Packs for '{season_label}' ({missing_cnt} missing, {existing_cnt} on disk)..."
                )

                try:
                    releases = await self.get_season_releases(series_id, season_num)
                except Exception as e:
                    logger.warning(f"  ⚠️ Interactive search failed for '{season_label}': {e}")
                    continue

                tier1_packs: List[Dict[str, Any]] = []
                tier2_packs: List[Dict[str, Any]] = []

                for rel in releases:
                    tier = self.classify_season_pack_release(rel, season_num)
                    if tier == "tier1_eligible":
                        tier1_packs.append(rel)
                    elif tier == "tier2_unknown_series":
                        tier2_packs.append(rel)

                winning_pack: Optional[Dict[str, Any]] = None
                decider_model = "Sonarr (Season Backfill)"
                match_confidence = 1.0
                match_reasoning = ""

                if tier1_packs:
                    tier1_packs.sort(key=self._rank_season_pack, reverse=True)
                    winning_pack = tier1_packs[0]
                    rejs = [
                        (r.get("reason", "") if isinstance(r, dict) else str(r))
                        for r in (winning_pack.get("rejections") or [])
                    ]
                    rej_note = f" (Overrode existing-file rejection: {'; '.join(rejs)})" if rejs else ""
                    match_reasoning = (
                        f"Selected best qualifying season pack '{winning_pack.get('title')}' "
                        f"to backfill {missing_cnt} missing episode(s) ({existing_cnt} already on disk){rej_note}."
                    )
                elif tier2_packs and ai_provider:
                    tier2_packs.sort(key=self._rank_season_pack, reverse=True)
                    logger.info(
                        f"  🤖 No direct pack matched for '{season_label}', evaluating {len(tier2_packs)} 'Unknown Series' pack(s) with AI..."
                    )
                    ai_res = await ai_provider.evaluate_backfill_season_pack(
                        series_info={
                            "id": series_id,
                            "title": series_title,
                            "year": series.get("year"),
                            "aliases": [a.get("title") for a in (series.get("alternateTitles") or [])],
                        },
                        season_number=season_num,
                        candidate_releases=[
                            {
                                "guid": r.get("guid"),
                                "title": r.get("title"),
                                "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                                "customFormatScore": r.get("customFormatScore", 0),
                                "qualityWeight": r.get("qualityWeight", 0),
                                "seeders": r.get("seeders"),
                            }
                            for r in tier2_packs[:25]
                        ],
                    )
                    decider_model = ai_res.model_name or ai_provider.name
                    match_confidence = ai_res.confidence
                    if ai_res.matched_guid and ai_res.confidence >= self.min_confidence:
                        winning_pack = next(
                            (r for r in tier2_packs if str(r.get("guid")) == str(ai_res.matched_guid)),
                            None,
                        )
                        match_reasoning = ai_res.reasoning or (
                            f"AI matched 'Unknown Series' season pack '{winning_pack.get('title') if winning_pack else ''}' "
                            f"for {season_label}."
                        )
                    else:
                        match_reasoning = ai_res.reasoning or "AI did not confirm any 'Unknown Series' candidate season pack."

                self._attempted_backfill_keys.add(backfill_key)

                if winning_pack:
                    pack_title = winning_pack.get("title") or season_label
                    grab_ok = await self.grab_release(
                        guid=str(winning_pack.get("guid")),
                        indexer_id=int(winning_pack.get("indexerId", 0)),
                        dry_run=is_dry_run,
                        title=pack_title,
                    )
                    status_str = "dry_run" if is_dry_run else ("imported" if grab_ok else "failed")
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=season_label,
                        category="season_backfill",
                        status=status_str,
                        confidence=match_confidence,
                        model=decider_model,
                        reasoning=match_reasoning,
                        details=f"{'Would grab' if is_dry_run else 'Grabbed'} season pack: {pack_title} ({missing_cnt} missing ep(s))",
                        item_keys=[backfill_key],
                    )
                    if grab_ok:
                        packs_grabbed += 1
                else:
                    no_pack_reason = match_reasoning or (
                        f"Searched {len(releases)} release(s) for {season_label} ({missing_cnt} missing, {existing_cnt} on disk); "
                        "no qualifying full-season pack found. Falling back inline to individual episode search."
                    )
                    logger.info(f"  ℹ️ {no_pack_reason}")
                    metrics.record_processed_item(
                        app="Sonarr",
                        title=season_label,
                        category="season_backfill",
                        status="skipped",
                        confidence=match_confidence if tier2_packs else None,
                        model=decider_model if tier2_packs else None,
                        reasoning=no_pack_reason,
                        details=f"No qualifying season pack found ({missing_cnt} missing ep(s)); falling back inline to individual episode search ({cooldown_days}d cooldown)",
                        item_keys=[backfill_key],
                    )

                    # Immediate inline fallback: search up to max_episodes_per_run missing episodes with 30s delay
                    season_missing_eps = [
                        e for e in season_eps
                        if e.get("monitored", True) and not e.get("hasFile", False)
                    ]
                    fallback_max = max(1, int(self.episode_retry_config.max_episodes_per_run))
                    logger.info(
                        f"  ↪ Falling back inline to search individual episodes for {season_label} (up to {fallback_max} eps, 30s delay)..."
                    )
                    await self._retry_missing_episodes_batch(
                        series=series,
                        season_num=season_num,
                        missing_eps=season_missing_eps,
                        queued_episode_ids=queued_episode_ids,
                        ai_provider=ai_provider,
                        dry_run=is_dry_run,
                        max_episodes=fallback_max,
                        delay_seconds=30.0,
                    )

        return packs_grabbed

    # =========================================================================
    # Ongoing Season Missing Episode Retry Engine
    # =========================================================================

    async def get_episode_releases(self, episode_id: int) -> List[Dict[str, Any]]:
        """Query Sonarr's Interactive Search endpoint for all releases of a specific episode."""
        url = f"{self.base_url}/api/v3/release"
        params = {"episodeId": episode_id}
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []

    @staticmethod
    def classify_episode_release(
        release: Dict[str, Any],
        season_number: int,
        episode_number: int,
    ) -> str:
        """
        Classify a single/multi-episode release from GET /api/v3/release?episodeId=X into:
        - 'tier1_eligible': Valid episode release for S{season_number}E{episode_number} with 0 rejections
          (or only existing-file quality rejections on companion episodes).
        - 'tier2_unknown_series': Valid episode release whose only non-quality rejection is 'Unknown Series'.
        - 'disqualified': Full-season pack, 0 seeders, wrong episode, or hard profile/language/size/blocklist rejections.
        """
        if release.get("fullSeason"):
            return "disqualified"

        protocol = (release.get("protocol") or "").lower()
        if protocol == "torrent" and (release.get("seeders") or 0) < 1:
            return "disqualified"

        raw_rejections = release.get("rejections") or []
        rejections = [
            (r.get("reason", "") if isinstance(r, dict) else str(r)).strip()
            for r in raw_rejections
            if r
        ]

        for r in rejections:
            r_low = r.lower()
            if any(h in r_low for h in HARD_EPISODE_RELEASE_DISQUALIFIER_PATTERNS):
                return "disqualified"

        rel_season = release.get("seasonNumber")
        rel_eps = release.get("episodeNumbers") or []
        matches_target_ep = rel_season == season_number and episode_number in rel_eps

        if not release.get("rejected") or not rejections:
            return "tier1_eligible" if matches_target_ep else "disqualified"

        has_unknown_series = False
        for r in rejections:
            r_low = r.lower()
            is_existing_quality = any(p in r_low for p in EXISTING_FILE_QUALITY_REJECTION_PATTERNS)
            is_unknown_series = any(
                u in r_low for u in ("unknown series", "no series", "not matched to any series")
            )
            if is_unknown_series:
                has_unknown_series = True
            elif not is_existing_quality:
                return "disqualified"

        if has_unknown_series:
            if rel_season in (season_number, None, 0) and (not rel_eps or episode_number in rel_eps):
                return "tier2_unknown_series"
            return "disqualified"

        return "tier1_eligible" if matches_target_ep else "disqualified"

    @staticmethod
    def find_eligible_missing_episodes_for_ongoing_season(
        season_episodes: List[Dict[str, Any]],
        min_air_delay_hours: int = 6,
        season_finale_grace_days: int = 3,
        is_latest_season: bool = False,
        series_status: str = "ended",
        now_utc: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        For an ongoing/incomplete season (where at least 1 monitored episode has NOT finished airing
        past `season_finale_grace_days`, or the season is the latest season of an actively continuing/upcoming series),
        return all monitored missing episodes (`hasFile == False`) that aired at least `min_air_delay_hours` ago.
        If the season is already completely finished airing past the finale grace period, returns []
        so Season Pack Backfill handles it instead.
        """
        monitored_eps = [e for e in season_episodes if e.get("monitored", True)]
        if not monitored_eps:
            return []

        now_dt = now_utc or datetime.now(timezone.utc)
        finale_cutoff_dt = now_dt - timedelta(days=max(0, int(season_finale_grace_days)))
        air_delay_cutoff_dt = now_dt - timedelta(hours=max(0, int(min_air_delay_hours)))

        is_season_ongoing = bool(
            is_latest_season and str(series_status).strip().lower() in ("continuing", "upcoming")
        )
        eligible_missing_eps: List[Dict[str, Any]] = []

        for ep in monitored_eps:
            air_utc_str = ep.get("airDateUtc")
            air_date_str = ep.get("airDate")
            if not air_utc_str and not air_date_str:
                is_season_ongoing = True
                continue

            try:
                if air_utc_str:
                    ep_dt = datetime.fromisoformat(air_utc_str.replace("Z", "+00:00"))
                    if ep_dt.tzinfo is None:
                        ep_dt = ep_dt.replace(tzinfo=timezone.utc)
                else:
                    ep_dt = datetime.fromisoformat(f"{air_date_str}T00:00:00+00:00")
            except ValueError:
                is_season_ongoing = True
                continue

            if ep_dt > finale_cutoff_dt:
                is_season_ongoing = True

            if not ep.get("hasFile", False) and ep_dt <= air_delay_cutoff_dt:
                eligible_missing_eps.append(ep)

        if not is_season_ongoing:
            return []

        eligible_missing_eps.sort(key=lambda e: int(e.get("episodeNumber") or 0))
        return eligible_missing_eps

    async def _retry_missing_episodes_batch(
        self,
        series: Dict[str, Any],
        season_num: int,
        missing_eps: List[Dict[str, Any]],
        queued_episode_ids: Set[int],
        ai_provider: Optional[BaseAIProvider] = None,
        dry_run: Optional[bool] = None,
        max_episodes: int = 5,
        delay_seconds: float = 30.0,
    ) -> Tuple[int, int]:
        """
        Iterate through missing episodes of a season in chronological order,
        search Sonarr interactive releases (with rate-limiting delay between searches),
        rank Tier 1 vs Tier 2 (AI), and grab winning releases.
        If a winning release covers multiple episodes, satisfies and records all covered episodes.
        Returns (episodes_searched, episodes_grabbed).
        """
        is_dry_run = self.dry_run if dry_run is None else dry_run
        cooldown_hours = max(1, int(self.episode_retry_config.episode_cooldown_hours))
        series_id = series.get("id")
        series_title = series.get("title") or f"Series #{series_id}"

        sorted_eps = sorted(missing_eps, key=lambda e: int(e.get("episodeNumber") or 0))
        ep_num_to_obj = {int(e.get("episodeNumber") or 0): e for e in sorted_eps}

        episodes_searched = 0
        episodes_grabbed = 0
        satisfied_ep_nums: Set[int] = set()

        for ep in sorted_eps:
            if episodes_searched >= max_episodes:
                break

            ep_num = int(ep.get("episodeNumber") or 0)
            if ep_num in satisfied_ep_nums:
                continue

            ep_id = ep.get("id")
            if not ep_id or int(ep_id) in queued_episode_ids:
                continue

            retry_key = f"ep_retry:sonarr:{series_id}:s{season_num:02d}e{ep_num:02d}"
            if metrics.is_item_attempted_within_hours([retry_key], hours=cooldown_hours):
                continue
            if not metrics.db and retry_key in self._attempted_episode_retry_keys:
                continue

            if episodes_searched > 0 and delay_seconds > 0:
                logger.info(
                    f"  ⏳ Waiting {delay_seconds:.0f}s before next episode search to respect indexer rate limits..."
                )
                await asyncio.sleep(delay_seconds)

            episodes_searched += 1
            ep_title = ep.get("title") or f"Episode {ep_num}"
            ep_label = f"{series_title} - S{season_num:02d}E{ep_num:02d} - {ep_title}"
            logger.info(f"  🔎 Searching releases for missing episode '{ep_label}'...")

            try:
                releases = await self.get_episode_releases(int(ep_id))
            except Exception as e:
                logger.warning(f"  ⚠️ Interactive episode search failed for '{ep_label}': {e}")
                continue

            tier1_releases: List[Dict[str, Any]] = []
            tier2_releases: List[Dict[str, Any]] = []

            for rel in releases:
                tier = self.classify_episode_release(rel, season_num, ep_num)
                if tier == "tier1_eligible":
                    tier1_releases.append(rel)
                elif tier == "tier2_unknown_series":
                    tier2_releases.append(rel)

            winning_rel: Optional[Dict[str, Any]] = None
            decider_model = "Sonarr (Episode Retry)"
            match_confidence = 1.0
            match_reasoning = ""

            if tier1_releases:
                tier1_releases.sort(key=self._rank_season_pack, reverse=True)
                winning_rel = tier1_releases[0]
                match_reasoning = (
                    f"Selected best qualifying release '{winning_rel.get('title')}' "
                    f"for missing episode S{season_num:02d}E{ep_num:02d}."
                )
            elif tier2_releases and ai_provider:
                tier2_releases.sort(key=self._rank_season_pack, reverse=True)
                logger.info(
                    f"  🤖 No direct episode release matched for '{ep_label}', evaluating {len(tier2_releases)} 'Unknown Series' release(s) with AI..."
                )
                ai_res = await ai_provider.evaluate_backfill_episode(
                    series_info={
                        "id": series_id,
                        "title": series_title,
                        "year": series.get("year"),
                        "aliases": [a.get("title") for a in (series.get("alternateTitles") or [])],
                    },
                    season_number=season_num,
                    episode_number=ep_num,
                    episode_title=ep_title,
                    candidate_releases=[
                        {
                            "guid": r.get("guid"),
                            "title": r.get("title"),
                            "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                            "customFormatScore": r.get("customFormatScore", 0),
                            "qualityWeight": r.get("qualityWeight", 0),
                            "seeders": r.get("seeders"),
                        }
                        for r in tier2_releases[:25]
                    ],
                )
                decider_model = ai_res.model_name or ai_provider.name
                match_confidence = ai_res.confidence
                if ai_res.matched_guid and ai_res.confidence >= self.min_confidence:
                    winning_rel = next(
                        (r for r in tier2_releases if str(r.get("guid")) == str(ai_res.matched_guid)),
                        None,
                    )
                    match_reasoning = ai_res.reasoning or (
                        f"AI matched 'Unknown Series' release '{winning_rel.get('title') if winning_rel else ''}' "
                        f"for {ep_label}."
                    )
                else:
                    match_reasoning = (
                        ai_res.reasoning
                        or "AI did not confirm any 'Unknown Series' candidate episode release."
                    )

            if winning_rel:
                rel_title = winning_rel.get("title") or ep_label
                grab_ok = await self.grab_release(
                    guid=str(winning_rel.get("guid")),
                    indexer_id=int(winning_rel.get("indexerId", 0)),
                    dry_run=is_dry_run,
                    title=rel_title,
                )
                status_str = "dry_run" if is_dry_run else ("imported" if grab_ok else "failed")
                if grab_ok:
                    episodes_grabbed += 1

                # Check which episodes this release covers (multi-episode support)
                raw_covered_nums = winning_rel.get("episodeNumbers") or [ep_num]
                covered_ints = [
                    int(n) for n in raw_covered_nums if isinstance(n, (int, str)) and str(n).isdigit()
                ]
                if not covered_ints or ep_num not in covered_ints:
                    covered_ints = [ep_num]

                for cov_num in covered_ints:
                    cov_ep = ep_num_to_obj.get(cov_num)
                    cov_title = (cov_ep.get("title") if cov_ep else None) or f"Episode {cov_num}"
                    cov_label = f"{series_title} - S{season_num:02d}E{cov_num:02d} - {cov_title}"
                    cov_key = f"ep_retry:sonarr:{series_id}:s{season_num:02d}e{cov_num:02d}"

                    self._attempted_episode_retry_keys.add(cov_key)
                    satisfied_ep_nums.add(cov_num)
                    if cov_ep and cov_ep.get("id"):
                        queued_episode_ids.add(int(cov_ep["id"]))

                    cov_details = f"{'Would grab' if is_dry_run else 'Grabbed'} episode release: {rel_title}"
                    if len(covered_ints) > 1:
                        cov_details += f" (multi-episode covers E{cov_num:02d})"

                    metrics.record_processed_item(
                        app="Sonarr",
                        title=cov_label,
                        category="episode_retry",
                        status=status_str,
                        confidence=match_confidence,
                        model=decider_model,
                        reasoning=match_reasoning,
                        details=cov_details,
                        item_keys=[cov_key],
                    )
            else:
                self._attempted_episode_retry_keys.add(retry_key)
                no_rel_reason = match_reasoning or (
                    f"Searched {len(releases)} release(s) for {ep_label}; no qualifying episode release found."
                )
                logger.info(f"  ℹ️ {no_rel_reason}")
                metrics.record_processed_item(
                    app="Sonarr",
                    title=ep_label,
                    category="episode_retry",
                    status="skipped",
                    confidence=match_confidence if tier2_releases else None,
                    model=decider_model if tier2_releases else None,
                    reasoning=no_rel_reason,
                    details=f"No qualifying episode release found ({cooldown_hours}h cooldown)",
                    item_keys=[retry_key],
                )

        return episodes_searched, episodes_grabbed

    async def run_missing_episode_retry(
        self,
        ai_provider: Optional[BaseAIProvider] = None,
        dry_run: Optional[bool] = None,
    ) -> int:
        """
        Scan monitored Sonarr series for ongoing/incomplete seasons with missing aired episodes,
        search for qualifying individual episode releases via GET /api/v3/release?episodeId=X,
        and grab the best release (using Tier 1 0-token ranking or Tier 2 AI fallback for 'Unknown Series').
        Returns the number of episode releases grabbed (or simulated in Dry Run).
        """
        is_dry_run = self.dry_run if dry_run is None else dry_run
        max_episodes = max(1, int(self.episode_retry_config.max_episodes_per_run))
        min_air_delay_hours = max(0, int(self.episode_retry_config.min_air_delay_hours))
        cooldown_hours = max(1, int(self.episode_retry_config.episode_cooldown_hours))
        finale_grace_days = max(0, int(self.backfill_config.min_missing_days))

        logger.info(
            f"🔎 Running Sonarr Missing Episode Retry for ongoing seasons "
            f"(Max episodes: {max_episodes}, Air delay: {min_air_delay_hours}h, Cooldown: {cooldown_hours}h)..."
        )

        all_series = await self.get_all_series()
        if not all_series:
            return 0

        queue_items = await self.get_queue()
        queued_episode_ids = set()
        queued_series_seasons = set()
        for q in queue_items or []:
            q_ep_id = q.get("episodeId") or (q.get("episode") or {}).get("id")
            if q_ep_id:
                queued_episode_ids.add(int(q_ep_id))
            q_series_id = q.get("seriesId") or (q.get("series") or {}).get("id")
            q_season = q.get("seasonNumber") or (q.get("episode") or {}).get("seasonNumber")
            if q_series_id and q.get("fullSeason") and q_season is not None:
                queued_series_seasons.add((int(q_series_id), int(q_season)))

        episodes_searched = 0
        episodes_grabbed = 0

        for series in all_series:
            if episodes_searched >= max_episodes:
                break
            if not series.get("monitored", True):
                continue

            series_id = series.get("id")
            series_title = series.get("title") or f"Series #{series_id}"
            if not series_id:
                continue

            seasons_meta = {
                int(s.get("seasonNumber")): s
                for s in (series.get("seasons") or [])
                if s.get("seasonNumber") is not None
            }

            episodes = await self.get_series_episodes(series_id)
            if not episodes:
                continue

            eps_by_season: Dict[int, List[Dict[str, Any]]] = {}
            for ep in episodes:
                s_num = ep.get("seasonNumber")
                if s_num is None or int(s_num) <= 0:
                    continue
                eps_by_season.setdefault(int(s_num), []).append(ep)

            sorted_seasons = sorted(eps_by_season.keys())
            max_season_num = max(sorted_seasons) if sorted_seasons else 0
            series_status = series.get("status") or ""

            for season_num in sorted_seasons:
                if episodes_searched >= max_episodes:
                    break

                s_meta = seasons_meta.get(season_num)
                if s_meta and not s_meta.get("monitored", True):
                    continue

                if (int(series_id), season_num) in queued_series_seasons:
                    continue

                season_eps = eps_by_season[season_num]
                is_latest = (season_num == max_season_num)
                missing_eps = self.find_eligible_missing_episodes_for_ongoing_season(
                    season_eps,
                    min_air_delay_hours=min_air_delay_hours,
                    season_finale_grace_days=finale_grace_days,
                    is_latest_season=is_latest,
                    series_status=series_status,
                )
                if not missing_eps:
                    continue

                rem_episodes = max_episodes - episodes_searched
                batch_searched, batch_grabbed = await self._retry_missing_episodes_batch(
                    series=series,
                    season_num=season_num,
                    missing_eps=missing_eps,
                    queued_episode_ids=queued_episode_ids,
                    ai_provider=ai_provider,
                    dry_run=is_dry_run,
                    max_episodes=rem_episodes,
                    delay_seconds=30.0,
                )
                episodes_searched += batch_searched
                episodes_grabbed += batch_grabbed

        return episodes_grabbed
